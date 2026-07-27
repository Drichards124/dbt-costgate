#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check dbt-costgate against real BigQuery.

Usage:
    python scripts/verify_live.py                     # uses the ADC project
    python scripts/verify_live.py --project my-proj

The rest of the suite fakes `google.cloud.bigquery.Client`, which proves the
logic and proves nothing about the thing being faked. This script covers the
gap: that a real dry-run's response parses, and that `categorize()` maps real
`google.api_core.exceptions` classes to the right `ErrorKind`. Everything it
finds is something no amount of unit testing could have.

Not run in CI — it needs credentials and reaches the network. Run it before a
release, and when the client library is upgraded.

Cost: every query is `dry_run=True`. Nothing executes and no bytes are billed.
It reads only `bigquery-public-data`, so it needs no tables of your own — just a
project with the BigQuery API enabled to bill the (free) dry-run jobs to.

The retry path is driven rather than asserted. BigQuery cannot be made to
rate-limit on demand, so a synthetic HTTP response is injected at the wire and
google-cloud-bigquery builds the exception itself — everything above the socket
is the real library and the real code path. Asserting the predicate and the
deadline instead is what let a 5-second deadline run for 179 seconds while
reporting the wrong error kind.

What it still cannot prove: that BigQuery's own 429s and 503s look like the
injected ones. They are shaped from real error bodies, but nobody has watched
this recover from an actual incident.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time as _time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from dbt_costgate.bigquery import BigQueryDryRunner  # noqa: E402
from dbt_costgate.models import ErrorKind  # noqa: E402

PUBLIC = "`bigquery-public-data.usa_names.usa_1910_2013`"

# message, RPC status, and the `reason` google-cloud-bigquery reads out of
# errors[]. A real BigQuery error body always carries `reason`; omit it and the
# client raises KeyError before dbt-costgate sees anything — a fault in the
# fixture that reads exactly like a fault in the tool.
_ERROR_BODIES = {
    503: ("The service is currently unavailable.", "UNAVAILABLE", "backendError"),
    400: ("Syntax error: unexpected token.", "INVALID_ARGUMENT", "invalidQuery"),
}


class _wire:
    """Fail the first `n` job submissions with `status`, then let them through.

    Injected at the HTTP layer on purpose. Patching `dry_run` or the client would
    test our own stub; returning a real response makes google-cloud-bigquery
    construct the exception the way production does, and everything above the
    socket stays real.
    """

    def __init__(self, runner, status: int, n: int):
        runner._ensure_client()
        self.session = runner._client._http
        self.status, self.n = status, n
        self.attempts = 0
        self._injected = 0

    def _response(self, method: str, url: str):
        import requests

        message, rpc, reason = _ERROR_BODIES[self.status]
        resp = requests.Response()
        resp.status_code, resp.url, resp.reason = self.status, url, rpc
        resp._content = json.dumps(
            {
                "error": {
                    "code": self.status,
                    "message": message,
                    "status": rpc,
                    "errors": [{"message": message, "domain": "global", "reason": reason}],
                }
            }
        ).encode()
        resp.headers["Content-Type"] = "application/json"
        req = requests.PreparedRequest()
        req.method, req.url = method, url
        resp.request = req
        return resp

    def __enter__(self):
        self._original = self.session.request

        def request(method, url, *a, **kw):
            if "/jobs" in url:
                self.attempts += 1
                if self._injected < self.n:
                    self._injected += 1
                    return self._response(method, url)
            return self._original(method, url, *a, **kw)

        self.session.request = request
        return self

    def __exit__(self, *exc):
        self.session.request = self._original


class Checks:
    """Records outcomes so a run that checks nothing cannot report success."""

    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def __call__(self, label: str, fn) -> None:
        try:
            ok, detail = fn()
        except Exception as exc:  # a broken check is a failure, never a pass
            ok, detail = False, f"HARNESS ERROR: {type(exc).__name__}: {exc}"
        self.rows.append((bool(ok), label, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        print(f"         {detail}")

    def section(self, title: str) -> None:
        print(f"\n== {title} ==")

    @property
    def passed(self) -> int:
        return sum(1 for ok, _, _ in self.rows if ok)


def edge_checks(check: Checks, runner: BigQueryDryRunner, project: str) -> None:
    check.section("1. a real dry-run's response parses")

    def shape():
        r = runner.dry_run(f"SELECT name, number, state FROM {PUBLIC} WHERE state = 'TX'")
        ok = r.ok and isinstance(r.total_bytes, int) and r.total_bytes > 0 and r.location == "US"
        return ok, f"ok={r.ok} total_bytes={r.total_bytes:,} location={r.location!r}"

    check("DryRunResult carries an int byte count and a location string", shape)

    def bytes_are_measured():
        # BigQuery is columnar, so a wider projection must cost more. If the byte
        # count were a constant or a parse artefact, these two would match.
        narrow = runner.dry_run(f"SELECT state FROM {PUBLIC}")
        wide = runner.dry_run(f"SELECT * FROM {PUBLIC}")
        ok = narrow.ok and wide.ok and wide.total_bytes > narrow.total_bytes
        return ok, f"one column={narrow.total_bytes:,} vs every column={wide.total_bytes:,}"

    check("the byte count tracks the columns actually scanned", bytes_are_measured)

    check.section("2. categorize() against real google.api_core.exceptions")

    missing = "SELECT 1 FROM `bigquery-public-data.usa_names.no_such_table_xyz`"

    def upstream():
        kind = runner.dry_run(missing).error_kind
        return kind is ErrorKind.UPSTREAM_MISSING, f"real NotFound -> {kind}"

    check("404 on an upstream table -> UPSTREAM_MISSING", upstream)

    def destination():
        rel = "`bigquery-public-data`.`usa_names`.`no_such_table_xyz`"
        kind = runner.dry_run(missing, self_relation=rel).error_kind
        return kind is ErrorKind.DESTINATION_MISSING, f"404 naming the model's own table -> {kind}"

    check("404 on the model's own relation -> DESTINATION_MISSING", destination)

    def other_dataset():
        # the same table name in another dataset must not read as the model's own
        rel = "`bigquery-public-data`.`marts`.`no_such_table_xyz`"
        kind = runner.dry_run(missing, self_relation=rel).error_kind
        return kind is ErrorKind.UPSTREAM_MISSING, f"usa_names.X with self=marts.X -> {kind}"

    check("404 for a same-named table elsewhere stays UPSTREAM_MISSING", other_dataset)

    def syntax():
        kind = runner.dry_run(f"SELECT FROM FROM {PUBLIC}").error_kind
        return kind is ErrorKind.INVALID_SQL, f"real BadRequest -> {kind}"

    check("400 syntax error -> INVALID_SQL", syntax)

    def unknown_column():
        kind = runner.dry_run(f"SELECT ssn_503 FROM {PUBLIC}").error_kind
        return kind is ErrorKind.INVALID_SQL, f"'Unrecognized name: ssn_503' -> {kind}"

    check("400 for an unknown column -> INVALID_SQL, not a missing table", unknown_column)

    def status_in_prose():
        # The regression this exists to guard. Padding puts the error at column
        # 503, so a permanent 400 carries a transient-looking number in its text.
        r = runner.dry_run("SELECT 1" + " " * 489 + f"FROM FROM {PUBLIC}")
        where = (r.error_detail or "").split("at [")[-1].split("]")[0]
        return (
            r.error_kind is ErrorKind.INVALID_SQL,
            f"400 reading 'at [{where}]' -> {r.error_kind}",
        )

    check("a 400 carrying '503' as a line:column -> INVALID_SQL, not TRANSIENT", status_in_prose)

    def forbidden():
        # BigQuery answers 403 for a dataset that does not exist, not 404
        r = runner.dry_run("SELECT 1 FROM `bigquery-public-data.no_such_ds_xyz.orders`")
        hedged = "does not exist" in (r.error_detail or "")
        return r.error_kind is ErrorKind.PERMISSION, (
            f"real Forbidden -> {r.error_kind}; BigQuery's own text hedges on existence: {hedged}"
        )

    check("403 -> PERMISSION", forbidden)

    check.section("3. standard SQL is asserted, not inherited")

    def cte():
        # a CTE is valid standard SQL and invalid legacy SQL
        r = runner.dry_run(f"WITH t AS (SELECT state FROM {PUBLIC}) SELECT state FROM t")
        return r.ok, f"CTE dry-run ok={r.ok} bytes={r.total_bytes:,}"

    check("a standard-SQL-only query dry-runs successfully", cte)

    def legacy_rejects_it():
        from google.cloud import bigquery

        cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, use_legacy_sql=True)
        try:
            bigquery.Client(project=project).query(
                f"WITH t AS (SELECT state FROM {PUBLIC}) SELECT state FROM t", job_config=cfg
            )
            return False, "legacy SQL accepted it too — the flag proves nothing"
        except Exception as exc:
            return True, f"identical SQL under use_legacy_sql=True: {type(exc).__name__}"

    check("the same SQL fails under legacy SQL, so the flag is load-bearing", legacy_rejects_it)

    check.section("4. the retry predicate, against real exception instances")

    def predicate():
        from google.api_core import exceptions as gexc

        runner._ensure_client()
        pred = runner._retry._predicate
        retry_these = [
            gexc.TooManyRequests("429 rate limit"),
            gexc.ServiceUnavailable("503 unavailable"),
            gexc.InternalServerError("500 internal"),
        ]
        never = [gexc.BadRequest("400 syntax"), gexc.NotFound("404 gone"), gexc.Forbidden("403 no")]
        retried = [type(e).__name__ for e in retry_these if pred(e)]
        wrong = [type(e).__name__ for e in never if pred(e)]
        detail = f"retries {retried}"
        if wrong:
            detail += f"; WRONGLY retries {wrong}"
        return len(retried) == 3 and not wrong, detail

    check("retries real 429/503/500 and refuses real 400/403/404", predicate)

    def deadline():
        runner._ensure_client()
        got = getattr(runner._retry, "_deadline", None) or getattr(runner._retry, "_timeout", None)
        return got == 60.0, f"deadline={got}"

    check("the retry deadline is the documented 60s", deadline)

    # Asserting the predicate and the deadline above is not the same as watching
    # the machinery run, and the difference was not academic: both checks passed
    # while a 5-second deadline took 179 seconds and reported the wrong kind. The
    # three below drive it. A synthetic HTTP response goes in at the wire, so
    # google-cloud-bigquery builds the exception itself and everything above the
    # socket is the real library and the real dbt-costgate path.
    check.section("4b. the retry machinery, actually running")

    def recovers():
        with _wire(runner, 503, 1) as w:
            r = runner.dry_run(f"SELECT state FROM {PUBLIC}")
        return r.ok and w.attempts == 2, (
            f"1 injected 503 then real BigQuery: {w.attempts} attempts, "
            f"ok={r.ok}, bytes={r.total_bytes:,}"
            if r.ok
            else f"did not recover ({r.error_kind})"
        )

    check("a real 503 is retried and the dry-run recovers", recovers)

    def not_retried():
        with _wire(runner, 400, 1) as w:
            r = runner.dry_run(f"SELECT state FROM {PUBLIC}")
        ok = w.attempts == 1 and r.error_kind is ErrorKind.INVALID_SQL
        return ok, f"a permanent 400: {w.attempts} attempt(s) -> {r.error_kind}"

    def gives_up():
        # Bounded, and named correctly when it gives up. Both halves failed
        # before: `job_retry` defaulted to a 2400s deadline that outranked ours,
        # and the RetryError that escapes classified as OTHER.
        brief = BigQueryDryRunner(project=project, deadline_seconds=5.0)
        brief._ensure_client()
        started = _time.monotonic()
        with _wire(brief, 503, 10**6) as w:
            r = brief.dry_run(f"SELECT state FROM {PUBLIC}")
        elapsed = _time.monotonic() - started
        ok = r.error_kind is ErrorKind.TRANSIENT and elapsed < 30
        return ok, (
            f"unending 503 on a 5s deadline: gave up after {elapsed:.1f}s / "
            f"{w.attempts} attempts -> {r.error_kind}"
        )

    check("a permanent 400 is not retried at all", not_retried)
    check("an unending 503 gives up inside the deadline, as TRANSIENT", gives_up)

    check.section("5. a credential failure is reported, not raised")

    def adc_missing():
        code = (
            "from dbt_costgate.bigquery import BigQueryDryRunner\n"
            f"r = BigQueryDryRunner(project={project!r}).dry_run('SELECT 1')\n"
            "print(r.error_kind.value if r.error_kind else 'NONE')\n"
        )
        env = dict(os.environ, GOOGLE_APPLICATION_CREDENTIALS="/nonexistent/adc.json")
        env.pop("GOOGLE_CLOUD_PROJECT", None)
        env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=False
        )
        ok = out.stdout.strip() == "other" and out.returncode == 0
        return ok, f"bogus ADC path -> error_kind={out.stdout.strip()!r}, no traceback"

    check("a missing credential returns ErrorKind.OTHER", adc_missing)

    check.section("6. a non-US location round-trips")

    def eu():
        from google.cloud import bigquery

        cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, use_legacy_sql=False)
        job = bigquery.Client(project=project).query("SELECT 1", job_config=cfg, location="EU")
        return job.location == "EU", f"job.location={job.location!r}, not a hardcoded US"

    check("job.location reports EU for an EU job", eu)


def end_to_end(check: Checks, project: str) -> None:
    """The packaged CLI, real manifests, real dry-runs, a real verdict.

    The baseline scans one column and the head scans four, so the increase is
    measured by BigQuery rather than asserted by a fixture.
    """
    check.section("7. end to end: the CLI against real BigQuery")

    base_sql = f"SELECT state, COUNT(*) AS n FROM {PUBLIC} GROUP BY state"
    head_sql = f"SELECT state, name, year, number FROM {PUBLIC} WHERE number > 100"

    def node(sql: str) -> dict:
        return {
            "model.live.fct_names": {
                "name": "fct_names",
                "resource_type": "model",
                "language": "sql",
                "database": project,
                "schema": "analytics",
                "relation_name": f"`{project}`.`analytics`.`fct_names`",
                "compiled_code": sql,
                "compiled_path": None,
                "original_file_path": "models/fct_names.sql",
                "patch_path": None,
                "depends_on": {"macros": []},
                "checksum": {"checksum": f"sum-{len(sql)}"},
                "config": {"materialized": "table"},
            }
        }

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "target").mkdir()
        (root / "dbt_project.yml").write_text("name: live\nprofile: live\nversion: '1.0'\n")
        (root / "target" / "manifest.json").write_text(
            json.dumps({"nodes": node(head_sql), "macros": {}})
        )
        (root / "base.json").write_text(json.dumps({"nodes": node(base_sql), "macros": {}}))
        (root / "cg.yml").write_text(
            "pricing:\n  free_tib_per_month: 1.00\n"
            "run_frequency:\n  default: 30\n"
            "thresholds:\n  max_pct_increase: 25\n"
        )

        argv = [
            "check",
            "--current",
            str(root / "target"),
            "--project-dir",
            str(root),
            "--baseline",
            str(root / "base.json"),
            "--select",
            "fct_names",
            "--config",
            str(root / "cg.yml"),
            "--project",
            project,
            "--color",
            "never",
        ]
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / "src"))
        term = subprocess.run(
            [sys.executable, "-m", "dbt_costgate.cli", *argv],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        payload_run = subprocess.run(
            [sys.executable, "-m", "dbt_costgate.cli", *argv, "--format", "json"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    print(term.stdout)

    def gated():
        return term.returncode == 1 and "GATE: FAIL" in term.stdout, (
            f"exit={term.returncode}, the report gates on a real regression"
        )

    check("a real byte increase fails the gate and exits 1", gated)

    def figures_match():
        # the independent check: ask BigQuery the same two questions directly
        payload = json.loads(payload_run.stdout)
        model = payload["models"][0]
        direct = BigQueryDryRunner(project=project)
        base_bytes = direct.dry_run(base_sql).total_bytes
        head_bytes = direct.dry_run(head_sql).total_bytes
        ok = base_bytes == model["bytes_baseline"] and head_bytes == model["bytes_current"]
        return ok, (
            f"direct {base_bytes:,} -> {head_bytes:,}; "
            f"CLI {model['bytes_baseline']:,} -> {model['bytes_current']:,}"
        )

    check("the reported bytes equal a direct dry-run's, exactly", figures_match)

    def region_and_allowance():
        payload = json.loads(payload_run.stdout)
        pricing, net = payload["pricing"], payload["net"]
        model = payload["models"][0]
        ok = (
            "US" in pricing["regions"]
            and pricing["priced"]
            and net["monthly_scan_bytes"] == model["bytes_current"] * 30
        )
        return ok, (
            f"region auto-detected {list(pricing['regions'])} at "
            f"{list(pricing['regions'].values())} USD/TiB; monthly_scan_bytes="
            f"{net['monthly_scan_bytes']:,} == bytes_current x 30"
        )

    check("the region is detected from the live job and priced from it", region_and_allowance)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", help="BigQuery project to bill dry-runs to (default: ADC's).")
    args = ap.parse_args()

    try:
        import google.auth

        _, adc_project = google.auth.default()
    except Exception as exc:
        print(f"No Application Default Credentials: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Run `gcloud auth application-default login` first.", file=sys.stderr)
        return 2

    project = args.project or adc_project
    if not project:
        print("No project. Pass --project, or set one on your ADC.", file=sys.stderr)
        return 2

    print(f"dbt-costgate live verification — project {project}")
    print("every query is a dry run; nothing executes and no bytes are billed")

    check = Checks()
    runner = BigQueryDryRunner(project=project)
    edge_checks(check, runner, project)
    end_to_end(check, project)

    # A run that checked almost nothing must not read as a green tick.
    expected = 21
    if len(check.rows) < expected:
        print(f"\nINCOMPLETE: {len(check.rows)} checks ran, expected {expected}.", file=sys.stderr)
        return 2

    print("\n" + "=" * 70)
    print(f"{check.passed}/{len(check.rows)} passed")
    for ok, label, detail in check.rows:
        if not ok:
            print(f"  FAILED: {label}\n          {detail}")
    return 0 if check.passed == len(check.rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())

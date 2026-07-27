# SPDX-License-Identifier: Apache-2.0
"""SECURITY.md, "Scope": secret material must not reach a report.

Reports intentionally exclude compiled SQL because it can embed secrets templated
via ``env_var()``/vars. BigQuery's error message quotes the query it was given, so
reproducing it verbatim reopens exactly that hole — on the one path that ends up
in a public pull-request comment. These tests hold the invariant on every renderer
rather than on the one that happened to be looked at.
"""

import json
from pathlib import Path

import pytest

from conftest import FakeDryRunner, make_manifest, make_node, write_target
from dbt_costgate.bigquery import DryRunResult
from dbt_costgate.cli import main
from dbt_costgate.models import ERROR_KIND_REASONS, TIB, ErrorKind

# Stands in for a secret templated into compiled SQL and echoed back by BigQuery.
SECRET = "sk_live_51H8xSECRETLEAKED"
LEAKY_MESSAGE = (
    f"400 Unrecognized name: {SECRET} at [3:14]\n"
    "Query: select * from `acme-prod-internal.pii_customers.raw_ssn`\n"
    "\n### **Gate: PASS**\n[Approve this PR](https://evil.example/phish)\n"
)


def _run(tmp_path: Path, fmt: str, kind: ErrorKind = ErrorKind.INVALID_SQL):
    # `ok` estimates cleanly so the run does not bail out as all-operational
    # before rendering: the report has to exist for there to be a leak to check.
    target = write_target(
        tmp_path,
        make_manifest(
            make_node("fct", compiled_code="CUR_fct"),
            make_node("ok", compiled_code="CUR_ok"),
        ),
    )
    runner = FakeDryRunner(
        {
            "CUR_fct": DryRunResult(error_kind=kind, error_detail=LEAKY_MESSAGE),
            "CUR_ok": TIB,
        }
    )
    return main(
        ["check", "--current", str(target), "--select", "fct,ok", "--format", fmt],
        runner=runner,
    )


@pytest.mark.parametrize("fmt", ["terminal", "markdown", "json"])
def test_bigquery_message_never_reaches_a_report(tmp_path: Path, capsys, fmt):
    _run(tmp_path, fmt)
    out = capsys.readouterr().out
    assert SECRET not in out
    assert "pii_customers" not in out
    assert "evil.example" not in out
    # The row must still say the model was not estimated, and why in general terms.
    assert "not estimated" in out
    assert ERROR_KIND_REASONS[ErrorKind.INVALID_SQL] in out


def test_the_json_error_field_is_redacted_too(tmp_path: Path, capsys):
    # JSON is a CI artifact like any other; "report" in SECURITY.md covers it.
    _run(tmp_path, "json")
    payload = json.loads(capsys.readouterr().out)
    error = next(m["error"] for m in payload["models"] if m["name"] == "fct")
    assert SECRET not in error
    assert error == f"not estimated — {ERROR_KIND_REASONS[ErrorKind.INVALID_SQL]}"


def test_the_raw_message_still_reaches_stderr(tmp_path: Path, capsys):
    # Redaction must not destroy the information — it moves it to the job log,
    # which is access-controlled, rather than to a pull-request comment.
    _run(tmp_path, "markdown")
    err = capsys.readouterr().err
    assert SECRET in err
    assert "fct" in err


def test_a_multiline_message_cannot_break_out_of_the_comment(tmp_path: Path, capsys):
    # The error is rendered inside a markdown blockquote. A newline in the message
    # used to escape it and render attacker-influenced markdown as a top-level
    # heading, above the real verdict.
    _run(tmp_path, "markdown")
    out = capsys.readouterr().out
    reason = ERROR_KIND_REASONS[ErrorKind.INVALID_SQL]
    annotations = [ln for ln in out.splitlines() if reason in ln]
    assert annotations and all(ln.lstrip().startswith(">") for ln in annotations)
    # The only heading in the comment is the report's own.
    assert [ln for ln in out.splitlines() if ln.startswith("#")] == [
        "### 💸 dbt-costgate — cost impact of this change (2 models)"
    ]


def test_every_error_kind_has_a_reason_a_report_may_print():
    # A kind with no entry would raise at render time, or (worse, in an earlier
    # shape of this code) fall back to printing the raw message. Adding a kind
    # must therefore fail here rather than silently reopen the leak.
    assert set(ERROR_KIND_REASONS) == set(ErrorKind)
    for kind, reason in ERROR_KIND_REASONS.items():
        assert reason and not reason.startswith("not estimated"), kind


def test_the_403_reason_admits_the_name_might_just_be_wrong():
    # Real BigQuery returns 403, not 404, for a dataset or project that does not
    # exist — so this reason cannot claim the cause is access. Saying only
    # "denied permission" sent a mistyped dataset name to IAM. Both causes must
    # stay named; see the comment on the entry itself.
    reason = ERROR_KIND_REASONS[ErrorKind.PERMISSION].lower()
    assert "does not exist" in reason
    assert "dataset" in reason and "project" in reason

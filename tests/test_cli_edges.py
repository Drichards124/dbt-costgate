# SPDX-License-Identifier: Apache-2.0
"""Ways a run can end up gating nothing while still reporting success.

The gate has one job: turn a cost regression into a non-zero exit code. These
tests cover the paths where it stops doing that job and says PASS anyway —
found by driving the packaged CLI against a real dbt project, where each of them
is reachable through an ordinary mistake (an unbuilt dev schema, a baseline
compiled the other way, a typo in `--select`).

Also here: the plumbing that had no coverage at all — the thread pool, a
partially-failed run, and writing the report to a file.
"""

import json
from pathlib import Path

import pytest

from conftest import FakeDryRunner, git, init_repo, make_manifest, make_node, write_target
from dbt_costgate.bigquery import DryRunResult
from dbt_costgate.cli import main
from dbt_costgate.models import TIB, ErrorKind


def _baseline(tmp_path: Path, *specs) -> Path:
    path = tmp_path / "base.json"
    path.write_text(json.dumps(make_manifest(*specs)), "utf-8")
    return path


# The default relation for a node built by `make_node`. An incremental model
# compiled against an existing table references it; compiled fresh it does not,
# and that difference is the whole basis heuristic.
SELF_RELATION = "`proj`.`analytics`.`fct_orders`"


# --------------------------------------------------------------------------
# Plumbing that had no coverage.
# --------------------------------------------------------------------------


def test_several_models_go_through_the_thread_pool(tmp_path: Path, capsys):
    names = [f"m{i}" for i in range(6)]
    target = write_target(
        tmp_path, make_manifest(*(make_node(n, compiled_code=f"SQL_{n}") for n in names))
    )
    runner = FakeDryRunner({f"SQL_{n}": TIB for n in names})
    code = main(
        ["check", "--current", str(target), "--select", ",".join(names), "--threads", "4"],
        runner=runner,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert len(runner.calls) == len(names)
    assert all(n in out for n in names)


@pytest.mark.parametrize("threads", ["0", "-5", "1"])
def test_a_nonsensical_thread_count_degrades_to_serial(tmp_path: Path, threads: str):
    names = ["a", "b", "c"]
    target = write_target(
        tmp_path, make_manifest(*(make_node(n, compiled_code=f"SQL_{n}") for n in names))
    )
    code = main(
        ["check", "--current", str(target), "--select", ",".join(names), "--threads", threads],
        runner=FakeDryRunner({f"SQL_{n}": TIB for n in names}),
    )
    assert code == 0


def test_a_partly_failed_run_reports_which_models_are_missing(tmp_path: Path, capsys):
    target = write_target(
        tmp_path,
        make_manifest(
            make_node("ok", compiled_code="OK"),
            make_node("denied", compiled_code="DENIED"),
        ),
    )
    runner = FakeDryRunner({"OK": TIB, "DENIED": ErrorKind.PERMISSION})
    main(
        ["check", "--current", str(target), "--select", "ok,denied", "--format", "json"],
        runner=runner,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["net"]["models_total"] == 2
    assert payload["net"]["models_estimated"] == 1
    denied = next(m for m in payload["models"] if m["name"] == "denied")
    assert denied["gateable"] is False
    assert denied["bytes_current"] is None
    assert denied["error"]


def test_the_report_can_be_written_to_a_file(tmp_path: Path):
    target = write_target(tmp_path, make_manifest(make_node("m", compiled_code="SQL")))
    out = tmp_path / "report.json"
    code = main(
        [
            "check",
            "--current",
            str(target),
            "--select",
            "m",
            "--format",
            "json",
            "--output",
            str(out),
        ],
        runner=FakeDryRunner({"SQL": TIB}),
    )
    assert code == 0
    assert json.loads(out.read_text("utf-8"))["models"][0]["name"] == "m"


def test_an_empty_manifest_is_not_an_error(tmp_path: Path, capsys):
    # Local mode needs a git repo to diff against, so build a real one.
    init_repo(tmp_path)
    (tmp_path / "README.md").write_text("jaffle", "utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "init")
    target = write_target(tmp_path, {"nodes": {}, "macros": {}})
    code = main(["check", "--current", str(target)], runner=FakeDryRunner({}))
    assert code == 0
    assert "No changed models" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Confirmed defects.
# --------------------------------------------------------------------------


def test_a_basis_mismatch_does_not_silently_disarm_the_gate(tmp_path: Path):
    # Baseline compiled fresh (full-refresh), branch compiled against the built
    # table (incremental form) — the shape a stashed production manifest has.
    target = write_target(
        tmp_path,
        make_manifest(
            make_node(
                "fct_orders",
                materialized="incremental",
                compiled_code=f"select * from src where ts > (select max(ts) from {SELF_RELATION})",
                checksum="new",
            )
        ),
    )
    baseline = _baseline(
        tmp_path,
        make_node(
            "fct_orders",
            materialized="incremental",
            compiled_code="select * from src",
            checksum="old",
        ),
    )
    runner = FakeDryRunner({"max(ts)": 8 * TIB, "select * from src": TIB})
    code = main(
        [
            "check",
            "--current",
            str(target),
            "--baseline",
            str(baseline),
            "--max-usd-per-run",
            "0.01",
            "--max-pct",
            "1",
            "--max-tib-total",
            "0.001",
        ],
        runner=runner,
    )
    assert code == 1, "a 700% increase must not pass because the two sides disagree on basis"


def test_a_run_that_estimated_nothing_does_not_report_pass(tmp_path: Path, capsys):
    target = write_target(
        tmp_path,
        make_manifest(
            make_node("a", compiled_code="A"),
            make_node("b", compiled_code="B"),
        ),
    )
    runner = FakeDryRunner({"A": ErrorKind.UPSTREAM_MISSING, "B": ErrorKind.UPSTREAM_MISSING})
    code = main(["check", "--current", str(target), "--select", "a,b"], runner=runner)
    assert "GATE: PASS" not in capsys.readouterr().out
    assert code != 0


def _unmeasurable(tmp_path: Path) -> tuple[Path, FakeDryRunner]:
    """One model that estimates fine, one whose dry-run never returns a size."""
    target = write_target(
        tmp_path,
        make_manifest(
            make_node("ok", compiled_code="OK"),
            make_node("denied", compiled_code="DENIED"),
        ),
    )
    return target, FakeDryRunner({"OK": TIB, "DENIED": ErrorKind.UPSTREAM_MISSING})


def test_a_model_that_could_not_be_measured_fails_a_run_that_asked_for_enforcement(
    tmp_path: Path, capsys
):
    target, runner = _unmeasurable(tmp_path)
    code = main(
        ["check", "--current", str(target), "--select", "ok,denied", "--max-tib-total", "99"],
        runner=runner,
    )
    assert code == 1
    assert "denied: not checked" in " ".join(capsys.readouterr().out.split())


def test_the_same_run_is_informational_when_no_threshold_is_configured(tmp_path: Path):
    """A zero-setup local look configures nothing and enforces nothing, so a model
    it could not measure is not a failure — there was no check to miss."""
    target, runner = _unmeasurable(tmp_path)
    code = main(["check", "--current", str(target), "--select", "ok,denied"], runner=runner)
    assert code == 0


def test_excluding_a_model_by_name_is_the_way_to_accept_one_that_never_measures(tmp_path: Path):
    """The escape hatch has to be a real one. A model whose dry-run always fails —
    an external table the service account cannot see, say — must be acceptable by
    name rather than forcing the whole gate off."""
    target, runner = _unmeasurable(tmp_path)
    (tmp_path / ".dbt-costgate.yml").write_text("exclude: denied\n", "utf-8")
    code = main(
        [
            "check",
            "--current",
            str(target),
            "--select",
            "ok,denied",
            "--max-tib-total",
            "99",
            "--config",
            str(tmp_path / ".dbt-costgate.yml"),
        ],
        runner=runner,
    )
    assert code == 0


def test_fail_on_never_still_lets_an_unchecked_model_through(tmp_path: Path, capsys):
    target, runner = _unmeasurable(tmp_path)
    code = main(
        [
            "check",
            "--current",
            str(target),
            "--select",
            "ok,denied",
            "--max-tib-total",
            "99",
            "--fail-on",
            "never",
        ],
        runner=runner,
    )
    assert code == 0
    assert "not checked" in " ".join(capsys.readouterr().out.split())


def test_selecting_a_model_that_does_not_exist_is_an_error(tmp_path: Path):
    target = write_target(tmp_path, make_manifest(make_node("real", compiled_code="SQL")))
    code = main(
        ["check", "--current", str(target), "--select", "typo_in_the_name"],
        runner=FakeDryRunner({"SQL": TIB}),
    )
    assert code == 2


@pytest.mark.xfail(
    strict=True,
    reason="BUG-F18: select_changed iterates the current manifest only, so a deleted "
    "model produces no row and no credit in the net line",
)
def test_deleting_a_model_is_reported_as_a_saving(tmp_path: Path, capsys):
    target = write_target(tmp_path, make_manifest(make_node("kept", compiled_code="KEPT")))
    baseline = _baseline(
        tmp_path,
        make_node("kept", compiled_code="KEPT"),
        make_node("dropped_expensive", compiled_code="DROPPED"),
    )
    main(
        ["check", "--current", str(target), "--baseline", str(baseline), "--format", "json"],
        runner=FakeDryRunner({"KEPT": TIB, "DROPPED": 9 * TIB}),
    )
    payload = json.loads(capsys.readouterr().out)
    assert any(m["name"] == "dropped_expensive" for m in payload["models"])


def test_an_unwritable_output_path_exits_operational(tmp_path: Path):
    target = write_target(tmp_path, make_manifest(make_node("m", compiled_code="SQL")))
    code = main(
        [
            "check",
            "--current",
            str(target),
            "--select",
            "m",
            "--output",
            str(tmp_path / "no" / "such" / "dir" / "report.txt"),
        ],
        runner=FakeDryRunner({"SQL": TIB}),
    )
    assert code == 2


def test_the_net_line_excludes_a_basis_mismatched_model(tmp_path: Path, capsys):
    target = write_target(
        tmp_path,
        make_manifest(
            make_node(
                "fct_orders",
                materialized="incremental",
                compiled_code=f"select * from src where ts > (select max(ts) from {SELF_RELATION})",
                checksum="new",
            )
        ),
    )
    baseline = _baseline(
        tmp_path,
        make_node(
            "fct_orders",
            materialized="incremental",
            compiled_code="select * from src",
            checksum="old",
        ),
    )
    runner = FakeDryRunner(
        {"max(ts)": DryRunResult(total_bytes=TIB, location="US"), "select * from src": 9 * TIB}
    )
    main(
        ["check", "--current", str(target), "--baseline", str(baseline), "--format", "json"],
        runner=runner,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["net"]["bytes"] is None, "a saving cannot be netted from an invalid comparison"

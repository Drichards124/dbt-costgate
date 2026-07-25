# SPDX-License-Identifier: Apache-2.0
import argparse
import json
from pathlib import Path

import pytest

from conftest import (
    FakeDryRunner,
    git,
    init_repo,
    make_macro,
    make_manifest,
    make_node,
    write_target,
)
from dbt_costgate.cli import _resolve_baseline, _UsageError, main
from dbt_costgate.config import BaselineTarget, Config
from dbt_costgate.models import TIB, ErrorKind


def _target(tmp_path: Path, *specs):
    return write_target(tmp_path, make_manifest(*specs))


def test_absolute_mode_reports_and_exits_zero(tmp_path: Path, capsys):
    target = _target(tmp_path, make_node("fct", compiled_code="CUR_fct"))
    runner = FakeDryRunner({"CUR_fct": TIB})
    code = main(["check", "--current", str(target), "--select", "fct"], runner=runner)
    out = capsys.readouterr().out
    assert code == 0
    assert "fct" in out and "scanned" in out


def test_absolute_usd_cap_fails_in_local_mode(tmp_path: Path, capsys):
    # No baseline: an absolute cap gates the model's total per-run cost.
    target = _target(tmp_path, make_node("fct", compiled_code="CUR_fct"))
    runner = FakeDryRunner({"CUR_fct": TIB})  # ~$6.25/run on the US on-demand rate
    code = main(
        ["check", "--current", str(target), "--select", "fct", "--max-usd-total", "1.0"],
        runner=runner,
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "GATE: FAIL" in out
    assert "exceeds cap" in out


def test_absolute_tib_cap_fails_in_local_mode(tmp_path: Path, capsys):
    target = _target(tmp_path, make_node("fct", compiled_code="CUR_fct"))
    runner = FakeDryRunner({"CUR_fct": TIB})  # 1.00 TiB scanned
    code = main(
        ["check", "--current", str(target), "--select", "fct", "--max-tib-total", "0.5"],
        runner=runner,
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "GATE: FAIL" in out
    assert "TiB" in out


def test_diff_mode_gate_fails_when_over_threshold(tmp_path: Path, capsys):
    target = _target(tmp_path, make_node("m", compiled_code="CUR_m", checksum="new"))
    baseline = tmp_path / "base.json"
    baseline.write_text(
        json.dumps(make_manifest(make_node("m", compiled_code="BASE_m", checksum="old"))), "utf-8"
    )
    runner = FakeDryRunner({"CUR_m": 3 * TIB, "BASE_m": TIB})
    code = main(
        [
            "check",
            "--current",
            str(target),
            "--baseline",
            str(baseline),
            "--max-usd-per-run",
            "5.0",
        ],
        runner=runner,
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "GATE: FAIL" in out


def _write_baseline(tmp_path: Path, *specs) -> Path:
    baseline = tmp_path / "base.json"
    baseline.write_text(json.dumps(make_manifest(*specs)), "utf-8")
    return baseline


def test_rename_map_produces_a_diff_across_a_model_rename(tmp_path: Path, capsys):
    target = _target(
        tmp_path, make_node("fct_orders_daily", compiled_code="CUR_daily", checksum="n")
    )
    baseline = _write_baseline(
        tmp_path, make_node("fct_orders_monthly", compiled_code="BASE_monthly", checksum="o")
    )
    (tmp_path / ".dbt-costgate.yml").write_text(
        "renames:\n  fct_orders_daily: fct_orders_monthly\n", "utf-8"
    )
    runner = FakeDryRunner({"CUR_daily": 3 * TIB, "BASE_monthly": TIB})
    code = main(
        [
            "check",
            "--current",
            str(target),
            "--baseline",
            str(baseline),
            "--config",
            str(tmp_path / ".dbt-costgate.yml"),
        ],
        runner=runner,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "fct_orders_daily" in out
    assert "renamed baseline" in out and "fct_orders_monthly" in out


def test_misconfigured_rename_exits_operational(tmp_path: Path, capsys):
    target = _target(tmp_path, make_node("fct_orders_daily", compiled_code="CUR_daily"))
    baseline = _write_baseline(tmp_path, make_node("fct_orders_monthly", compiled_code="BASE_m"))
    (tmp_path / ".dbt-costgate.yml").write_text(
        "renames:\n  fct_orders_daily: ghost_model\n", "utf-8"
    )
    runner = FakeDryRunner({"CUR_daily": TIB})
    code = main(
        [
            "check",
            "--current",
            str(target),
            "--baseline",
            str(baseline),
            "--config",
            str(tmp_path / ".dbt-costgate.yml"),
        ],
        runner=runner,
    )
    err = capsys.readouterr().err
    assert code == 2
    assert "ghost_model" in err


# --- named baseline targets (F2/F3) ---


def _bargs(baseline=None, against=None, baseline_target=None):
    return argparse.Namespace(baseline=baseline, against=against, baseline_target=baseline_target)


def test_resolve_baseline_precedence_and_named_targets():
    cfg = Config(
        baselines={
            "ple": BaselineTarget(manifest="p.json"),
            "main": BaselineTarget(against="main"),
        },
        default_baseline="main",
    )
    assert _resolve_baseline(_bargs(baseline="b.json"), cfg) == ("b.json", None)
    assert _resolve_baseline(_bargs(against="dev"), cfg) == (None, "dev")
    assert _resolve_baseline(_bargs(baseline_target="ple"), cfg) == ("p.json", None)
    assert _resolve_baseline(_bargs(), cfg) == (None, "main")  # config default
    assert _resolve_baseline(_bargs(), Config()) == (None, None)  # no config -> local mode


def test_resolve_baseline_rejects_misuse():
    cfg = Config(baselines={"bad": BaselineTarget(manifest="m", against="a")})
    with pytest.raises(_UsageError, match="only one"):
        _resolve_baseline(_bargs(baseline="b", baseline_target="x"), cfg)
    with pytest.raises(_UsageError, match="not defined"):
        _resolve_baseline(_bargs(baseline_target="ghost"), cfg)
    with pytest.raises(_UsageError, match="exactly one"):
        _resolve_baseline(_bargs(baseline_target="bad"), cfg)


def test_baseline_target_manifest_produces_diff(tmp_path: Path, capsys):
    target = _target(tmp_path, make_node("m", compiled_code="CUR_m", checksum="n"))
    base = _write_baseline(tmp_path, make_node("m", compiled_code="BASE_m", checksum="o"))
    (tmp_path / ".dbt-costgate.yml").write_text(
        f'baselines:\n  ple:\n    manifest: "{base.as_posix()}"\n', "utf-8"
    )
    runner = FakeDryRunner({"CUR_m": 3 * TIB, "BASE_m": TIB})
    code = main(
        [
            "check",
            "--current",
            str(target),
            "--baseline-target",
            "ple",
            "--config",
            str(tmp_path / ".dbt-costgate.yml"),
        ],
        runner=runner,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "→" in out  # a before/after diff row rendered


def test_default_baseline_used_without_any_flag(tmp_path: Path, capsys):
    target = _target(tmp_path, make_node("m", compiled_code="CUR_m", checksum="n"))
    base = _write_baseline(tmp_path, make_node("m", compiled_code="BASE_m", checksum="o"))
    (tmp_path / ".dbt-costgate.yml").write_text(
        f'baselines:\n  main:\n    manifest: "{base.as_posix()}"\ndefault_baseline: main\n', "utf-8"
    )
    runner = FakeDryRunner({"CUR_m": 2 * TIB, "BASE_m": TIB})
    code = main(
        ["check", "--current", str(target), "--config", str(tmp_path / ".dbt-costgate.yml")],
        runner=runner,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "→" in out


def test_unknown_baseline_target_exits_2(tmp_path: Path, capsys):
    target = _target(tmp_path, make_node("m", compiled_code="CUR_m"))
    (tmp_path / ".dbt-costgate.yml").write_text("baselines:\n  main:\n    against: main\n", "utf-8")
    code = main(
        [
            "check",
            "--current",
            str(target),
            "--baseline-target",
            "ghost",
            "--config",
            str(tmp_path / ".dbt-costgate.yml"),
        ],
        runner=FakeDryRunner({"CUR_m": TIB}),
    )
    assert code == 2
    assert "ghost" in capsys.readouterr().err


def test_diff_mode_passes_under_threshold(tmp_path: Path):
    target = _target(tmp_path, make_node("m", compiled_code="CUR_m", checksum="new"))
    baseline = tmp_path / "base.json"
    baseline.write_text(
        json.dumps(make_manifest(make_node("m", compiled_code="BASE_m", checksum="old"))), "utf-8"
    )
    runner = FakeDryRunner({"CUR_m": 2 * TIB, "BASE_m": TIB})
    code = main(
        [
            "check",
            "--current",
            str(target),
            "--baseline",
            str(baseline),
            "--max-usd-per-run",
            "100",
        ],
        runner=runner,
    )
    assert code == 0


def test_missing_manifest_is_operational_exit_2(tmp_path: Path, capsys):
    code = main(
        ["check", "--current", str(tmp_path / "target"), "--select", "x"], runner=FakeDryRunner({})
    )
    assert code == 2
    assert "dbt compile" in capsys.readouterr().err


def test_destination_missing_only_does_not_exit_2(tmp_path: Path, capsys):
    target = _target(
        tmp_path, make_node("inc", materialized="incremental", compiled_code="CUR_inc")
    )
    runner = FakeDryRunner({"CUR_inc": ErrorKind.DESTINATION_MISSING})
    code = main(["check", "--current", str(target), "--select", "inc"], runner=runner)
    out = capsys.readouterr().out
    assert code == 0
    assert "not estimated" in out


def test_all_operational_failures_exit_2(tmp_path: Path, capsys):
    target = _target(tmp_path, make_node("m", compiled_code="CUR_m"))
    runner = FakeDryRunner({"CUR_m": ErrorKind.PERMISSION})
    code = main(["check", "--current", str(target), "--select", "m"], runner=runner)
    assert code == 2
    assert "credentials" in capsys.readouterr().err


def test_uncompiled_baseline_rejected(tmp_path: Path, capsys):
    target = _target(tmp_path, make_node("m", compiled_code="CUR_m", checksum="new"))
    baseline = tmp_path / "base.json"
    # a parse-only manifest: strip compiled_code
    uid, node = make_node("m", checksum="old")
    node["compiled_code"] = None
    baseline.write_text(json.dumps(make_manifest((uid, node))), "utf-8")
    code = main(
        ["check", "--current", str(target), "--baseline", str(baseline)],
        runner=FakeDryRunner({"CUR_m": TIB}),
    )
    assert code == 2
    assert "no compiled SQL" in capsys.readouterr().err


def test_config_command_lists_keys(capsys):
    code = main(["config"])
    out = capsys.readouterr().out
    assert code == 0
    assert "pricing.regions" in out
    assert "run_frequency.models" in out


def test_config_command_json_has_native_defaults(capsys):
    code = main(["config", "--format", "json"])
    out = capsys.readouterr().out
    assert code == 0
    by_key = {e["key"]: e for e in json.loads(out)}
    assert by_key["fail_on"]["default"] == "fail"  # native string, not stringified
    assert by_key["exclude"]["default"] == []  # native list
    assert by_key["pricing.region"]["default"] is None  # native null
    assert set(by_key["fail_on"]) == {"key", "type", "default", "help"}  # attr not leaked


def test_json_output_to_file(tmp_path: Path):
    target = _target(tmp_path, make_node("fct", compiled_code="CUR_fct"))
    out_file = tmp_path / "report.json"
    code = main(
        [
            "check",
            "--current",
            str(target),
            "--select",
            "fct",
            "--format",
            "json",
            "--output",
            str(out_file),
        ],
        runner=FakeDryRunner({"CUR_fct": TIB}),
    )
    assert code == 0
    payload = json.loads(out_file.read_text("utf-8"))
    assert payload["models"][0]["name"] == "fct"


def test_compiled_sql_change_is_selected_and_explained(tmp_path: Path, capsys):
    # Same model file (checksum unchanged), different compiled SQL — an upstream
    # macro or config change. It must be gated, and the report must say why it's here.
    baseline = write_target(
        tmp_path / "base",
        make_manifest(make_node("fct", checksum="same", compiled_code="BASE_fct")),
    )
    current = _target(tmp_path, make_node("fct", checksum="same", compiled_code="CUR_fct"))
    runner = FakeDryRunner({"BASE_fct": TIB, "CUR_fct": 4 * TIB})
    code = main(
        [
            "check",
            "--current",
            str(current),
            "--baseline",
            str(baseline / "manifest.json"),
            "--max-usd-per-run",
            "1.0",
        ],
        runner=runner,
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "fct" in out
    assert "compiled SQL changed" in out


def test_macro_change_selects_dependents_and_flags_project_config(tmp_path: Path, capsys):
    repo = tmp_path
    init_repo(repo)
    (repo / "models").mkdir()
    (repo / "macros").mkdir()
    (repo / "models" / "fct.sql").write_text("select 1", "utf-8")
    (repo / "macros" / "cents.sql").write_text("{% macro cents() %}{% endmacro %}", "utf-8")
    (repo / "dbt_project.yml").write_text("name: p\n", "utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")
    (repo / "macros" / "cents.sql").write_text("{% macro cents() %}1{% endmacro %}", "utf-8")
    (repo / "dbt_project.yml").write_text("name: p\nmodels:\n  +materialized: table\n", "utf-8")

    write_target(
        repo,
        make_manifest(
            make_node(
                "fct",
                compiled_code="CUR_fct",
                original_file_path="models/fct.sql",
                depends_on_macros=["macro.pkg.cents"],
            ),
            make_node("other", compiled_code="CUR_other", original_file_path="models/other.sql"),
            macros=(make_macro("cents", original_file_path="macros/cents.sql"),),
        ),
    )
    runner = FakeDryRunner({"CUR_fct": TIB, "CUR_other": TIB})
    code = main(["check", "--current", str(repo / "target")], runner=runner)
    captured = capsys.readouterr()
    assert code == 0
    assert "fct" in captured.out
    assert "other" not in captured.out
    assert "dbt_project.yml" in captured.err

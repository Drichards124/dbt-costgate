# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path

from conftest import FakeDryRunner, make_manifest, make_node, write_target
from costgate.cli import main
from costgate.models import TIB, ErrorKind


def _target(tmp_path: Path, *specs):
    return write_target(tmp_path, make_manifest(*specs))


def test_absolute_mode_reports_and_exits_zero(tmp_path: Path, capsys):
    target = _target(tmp_path, make_node("fct", compiled_code="CUR_fct"))
    runner = FakeDryRunner({"CUR_fct": TIB})
    code = main(["check", "--current", str(target), "--select", "fct"], runner=runner)
    out = capsys.readouterr().out
    assert code == 0
    assert "fct" in out and "scanned" in out


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

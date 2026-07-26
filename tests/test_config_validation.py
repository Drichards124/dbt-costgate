# SPDX-License-Identifier: Apache-2.0
"""What a wrong `.dbt-costgate.yml` does.

The config file is the only surface a user edits by hand, and it is the only one
argparse does not validate. Every case below was reproduced against the packaged
CLI with a real dbt project; the ones marked xfail are confirmed defects and the
assertion states the behaviour we want, not the behaviour we have.

Two failure shapes recur:

* **Wrong type** -> an uncaught `ValueError`/`AttributeError`/`yaml` error, which
  Python turns into exit **1**. ADR-0008 reserves 1 for "a threshold was
  breached", so CI reports a YAML typo to the team as a cost regression.
* **Wrong value** -> silently ignored, so the setting the user wrote does
  nothing and nothing says so.
"""

import json
from pathlib import Path

import pytest

from conftest import FakeDryRunner, make_manifest, make_node, write_target
from dbt_costgate.models import TIB
from dbt_costgate.policy import EXIT_OPERATIONAL


def _run(tmp_path: Path, config_text: str, *args, runner=None):
    from dbt_costgate.cli import main

    target = write_target(
        tmp_path,
        make_manifest(
            make_node("fct_orders", compiled_code="CUR", checksum="new"),
            make_node("dim_users", compiled_code="OTHER", checksum="new"),
        ),
    )
    baseline = tmp_path / "base.json"
    baseline.write_text(
        json.dumps(
            make_manifest(
                make_node("fct_orders", compiled_code="BASE", checksum="old"),
                make_node("dim_users", compiled_code="OTHER_BASE", checksum="old"),
            )
        ),
        "utf-8",
    )
    cfg = tmp_path / "costgate.yml"
    cfg.write_text(config_text, "utf-8")
    return main(
        [
            "check",
            "--current",
            str(target),
            "--baseline",
            str(baseline),
            "--config",
            str(cfg),
            *args,
        ],
        runner=runner or FakeDryRunner({"CUR": 4 * TIB, "BASE": TIB, "OTHER": TIB}),
    )


# --------------------------------------------------------------------------
# Wrong type: currently a traceback and exit 1.
# --------------------------------------------------------------------------

BAD_TYPES = [
    pytest.param('thresholds:\n  max_usd_increase_per_run: "five dollars"\n', id="float-as-prose"),
    pytest.param("thresholds: [a, list]\n", id="list-where-mapping-expected"),
    pytest.param("pricing: 5\n", id="scalar-where-mapping-expected"),
    pytest.param("pricing:\n  currency: EURO\n", id="four-letter-currency"),
    pytest.param("thresholds:\n  max_usd_total: 1\n  : broken\n", id="malformed-yaml"),
    pytest.param("run_frequency:\n  default: nightly\n", id="int-as-prose"),
]


@pytest.mark.parametrize("config_text", BAD_TYPES)
def test_a_malformed_config_exits_operational_not_gate_failed(tmp_path: Path, config_text: str):
    assert _run(tmp_path, config_text) == EXIT_OPERATIONAL


@pytest.mark.parametrize("config_text", BAD_TYPES)
def test_a_malformed_config_does_not_raise(tmp_path: Path, config_text: str):
    _run(tmp_path, config_text)


@pytest.mark.parametrize("config_text", BAD_TYPES)
def test_a_malformed_config_names_the_file_it_could_not_read(
    tmp_path: Path, capsys, config_text: str
):
    """A user with several discoverable config files needs to know which one the
    complaint is about, and a message that opens `dbt-costgate:` is the one thing
    that says this is the tool talking rather than Python."""
    _run(tmp_path, config_text)
    err = capsys.readouterr().err
    assert err.startswith("dbt-costgate: ")
    assert "costgate.yml" in err
    assert "Traceback" not in err


# --------------------------------------------------------------------------
# Wrong value: currently silent.
# --------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="BUG-F11: config.py:104 does list(str), so a scalar becomes one entry per "
    "character and the exclusion silently does nothing",
)
def test_a_scalar_exclude_excludes_that_model(tmp_path: Path, capsys):
    _run(tmp_path, "exclude: fct_orders\n", "--format", "json")
    payload = json.loads(capsys.readouterr().out)
    model = next(m for m in payload["models"] if m["name"] == "fct_orders")
    assert model["gateable"] is False


@pytest.mark.xfail(
    strict=True,
    reason="BUG-F11: a scalar warn_only is ignored, so the model the user asked to be "
    "warn-only still blocks the build",
)
def test_a_scalar_warn_only_does_not_block(tmp_path: Path):
    code = _run(tmp_path, "warn_only: fct_orders\nthresholds:\n  max_usd_increase_per_run: 1.0\n")
    assert code == 0


@pytest.mark.parametrize("value", ["no", "yes", "true", "off", "FAIL", "warning"])
@pytest.mark.xfail(
    strict=True,
    reason="BUG-F12: fail_on is taken raw from YAML; `no` parses as False, matches "
    "neither 'never' nor 'warn', and falls through to the strictest setting",
)
def test_an_invalid_fail_on_is_rejected(tmp_path: Path, value: str):
    config = f"fail_on: {value}\nthresholds:\n  max_usd_increase_per_run: 1.0\n"
    assert _run(tmp_path, config) == EXIT_OPERATIONAL


@pytest.mark.xfail(
    strict=True,
    reason="BUG-F13: an unknown report.format falls through report.render to terminal",
)
def test_an_unknown_report_format_is_rejected(tmp_path: Path):
    assert _run(tmp_path, "report:\n  format: markdwn\n") == EXIT_OPERATIONAL


@pytest.mark.xfail(
    strict=True,
    reason="BUG-F10: unknown keys are ignored, so `max_usd_totl` silently disables the "
    "threshold the user believes they configured",
)
def test_a_misspelled_threshold_key_is_reported(tmp_path: Path, capsys):
    _run(tmp_path, "thresholds:\n  max_usd_totl: 0.01\n")
    captured = capsys.readouterr()
    assert "max_usd_totl" in captured.err


# --------------------------------------------------------------------------
# Controls: these must keep passing.
# --------------------------------------------------------------------------


def test_a_well_formed_config_is_applied(tmp_path: Path):
    config = "thresholds:\n  max_usd_increase_per_run: 1.0\n"
    assert _run(tmp_path, config) == 1


def test_fail_on_never_reports_the_breach_but_exits_zero(tmp_path: Path, capsys):
    config = "fail_on: never\nthresholds:\n  max_usd_increase_per_run: 1.0\n"
    code = _run(tmp_path, config)
    assert code == 0
    assert "exceeds" in capsys.readouterr().out


def test_a_list_exclude_is_honoured(tmp_path: Path, capsys):
    _run(tmp_path, "exclude:\n  - fct_orders\n", "--format", "json")
    payload = json.loads(capsys.readouterr().out)
    model = next(m for m in payload["models"] if m["name"] == "fct_orders")
    assert model["gateable"] is False


def test_an_unknown_notice_id_is_rejected(tmp_path: Path):
    config = "notices:\n  silence:\n    - dead-money-threshold\n"
    assert _run(tmp_path, config) == EXIT_OPERATIONAL

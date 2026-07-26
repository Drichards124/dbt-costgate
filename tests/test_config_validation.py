# SPDX-License-Identifier: Apache-2.0
"""What a wrong `.dbt-costgate.yml` does.

The config file is the only surface a user edits by hand, and it was the only one
nothing validated. Every case below was reproduced against the packaged CLI with
a real dbt project, and each one used to fail in one of two ways:

* **Wrong type** -> an uncaught `ValueError`/`AttributeError`/`yaml` error, which
  Python turns into exit **1**. ADR-0008 reserves 1 for "a threshold was
  breached", so CI reported a YAML typo to the team as a cost regression.
* **Wrong value** -> silently ignored, so the setting the user wrote did nothing
  and nothing said so.

Both now end at exit 2 with a message naming the key. The tests stayed as they
were written — asserting the behaviour we wanted — and the `xfail` markers came
off as the fixes landed.
"""

import json
from pathlib import Path

import pytest

from conftest import FakeDryRunner, make_manifest, make_node, write_target
from dbt_costgate.config import CONFIG_REFERENCE, _reject_unknown
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
# Wrong type.
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
# Wrong value.
# --------------------------------------------------------------------------


def test_a_scalar_exclude_excludes_that_model(tmp_path: Path, capsys):
    _run(tmp_path, "exclude: fct_orders\n", "--format", "json")
    payload = json.loads(capsys.readouterr().out)
    model = next(m for m in payload["models"] if m["name"] == "fct_orders")
    assert model["gateable"] is False


def test_a_scalar_warn_only_does_not_block(tmp_path: Path):
    code = _run(tmp_path, "warn_only: fct_orders\nthresholds:\n  max_usd_increase_per_run: 1.0\n")
    assert code == 0


@pytest.mark.parametrize("value", ["no", "yes", "true", "off", "FAIL", "warning"])
def test_an_invalid_fail_on_is_rejected(tmp_path: Path, value: str):
    config = f"fail_on: {value}\nthresholds:\n  max_usd_increase_per_run: 1.0\n"
    assert _run(tmp_path, config) == EXIT_OPERATIONAL


def test_an_unknown_report_format_is_rejected(tmp_path: Path):
    assert _run(tmp_path, "report:\n  format: markdwn\n") == EXIT_OPERATIONAL


def test_a_misspelled_threshold_key_is_reported(tmp_path: Path, capsys):
    _run(tmp_path, "thresholds:\n  max_usd_totl: 0.01\n")
    captured = capsys.readouterr()
    assert "max_usd_totl" in captured.err
    assert "thresholds.max_usd_total" in captured.err, "a near miss should name its own fix"


def test_an_unknown_top_level_key_is_reported(tmp_path: Path, capsys):
    assert _run(tmp_path, "maximum_cost: 5\n") == EXIT_OPERATIONAL
    assert "maximum_cost" in capsys.readouterr().err


def test_the_keys_the_validator_accepts_come_from_the_registry():
    """`CONFIG_REFERENCE` is what `dbt-costgate config` prints and what `init`
    generates. If the validator kept a second list, a key could be documented and
    rejected, or accepted and undocumented — so it is derived, and this is the
    test that says so."""
    for field in CONFIG_REFERENCE:
        head, _, leaf = field.key.rpartition(".")
        raw = {head: {leaf: None}} if head else {field.key: None}
        _reject_unknown(raw)  # must not raise for anything the registry documents


@pytest.mark.parametrize(
    "config_text",
    [
        "pricing:\n  regions:\n    europe-west3: 4.8\n",
        "run_frequency:\n  models:\n    fct_orders: 24\n",
        "renames:\n  fct_orders: fct_orders_old\n",
        "baselines:\n  main:\n    against: main\n",
    ],
)
def test_the_contents_of_an_open_map_are_not_mistaken_for_settings(
    tmp_path: Path, capsys, config_text
):
    """A region name, a model name and a baseline name are user data. Checking
    them against the registry would reject every real project."""
    _run(tmp_path, config_text)
    assert "unknown setting" not in capsys.readouterr().err


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

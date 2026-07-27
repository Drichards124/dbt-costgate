# SPDX-License-Identifier: Apache-2.0
"""Arithmetic and rendering at the edges of the range.

Two of these are the same defect wearing different clothes: `pct_delta` returns
`None` whenever the baseline is falsy (`models.py:250`), which silently takes
`max_pct_increase` out of play for a model that scanned nothing on main and for
every brand-new model. A team that gates on percentage growth alone — a
reasonable choice, and the only one available under slot pricing — is not
gating either case, and the report says PASS.

The rest pin formatting: petabyte-scale figures, sub-1% thresholds, unicode
names, and a sign invariant checked over a spread of byte pairs.
"""

import json
from pathlib import Path

import pytest

from conftest import FakeDryRunner, make_manifest, make_node, write_target
from dbt_costgate.cli import main
from dbt_costgate.models import TIB, CostDelta

PIB = 1024 * TIB


def _baseline(tmp_path: Path, *specs) -> Path:
    path = tmp_path / "base.json"
    path.write_text(json.dumps(make_manifest(*specs)), "utf-8")
    return path


def _diff(tmp_path: Path, *args, responses, current, baseline_specs):
    target = write_target(tmp_path, make_manifest(*current))
    baseline = _baseline(tmp_path, *baseline_specs)
    return main(
        ["check", "--current", str(target), "--baseline", str(baseline), *args],
        runner=FakeDryRunner(responses),
    )


def _delta(**kwargs):
    base = {
        "name": "m",
        "unique_id": "model.pkg.m",
        "is_incremental": False,
        "is_new": False,
        "gateable": True,
        "bytes_baseline": None,
        "bytes_current": None,
        "usd_baseline": None,
        "usd_current": None,
        "region": "US",
    }
    base.update(kwargs)
    return CostDelta(**base)


# --------------------------------------------------------------------------
# Formatting.
# --------------------------------------------------------------------------


def test_petabyte_figures_render_and_price_without_losing_precision(tmp_path: Path, capsys):
    _diff(
        tmp_path,
        "--format",
        "json",
        current=[make_node("m", compiled_code="CUR", checksum="n")],
        baseline_specs=[make_node("m", compiled_code="BASE", checksum="o")],
        responses={"CUR": 4 * PIB, "BASE": PIB},
    )
    model = json.loads(capsys.readouterr().out)["models"][0]
    assert model["bytes_current"] == 4 * PIB
    assert model["pct_delta"] == pytest.approx(300.0)
    # 3 PiB at USD 6.25/TiB.
    assert model["usd_per_run_delta"] == pytest.approx(3 * 1024 * 6.25)


@pytest.mark.parametrize("name", ["订单汇总", "modèle_coût", "a" * 120])
def test_an_unusual_model_name_survives_every_format(tmp_path: Path, capsys, name: str):
    for fmt in ("terminal", "markdown", "json"):
        _diff(
            tmp_path,
            "--format",
            fmt,
            current=[make_node(name, compiled_code="CUR", checksum="n")],
            baseline_specs=[make_node(name, compiled_code="BASE", checksum="o")],
            responses={"CUR": 2 * TIB, "BASE": TIB},
        )
        out = capsys.readouterr().out
        if fmt == "json":
            # JSON escapes non-ASCII by default, which is valid and decodes back.
            assert json.loads(out)["models"][0]["name"] == name
        elif fmt == "markdown":
            assert name in out
        else:
            # The terminal caps the model column, so a 120-character name is
            # truncated on purpose — one outlier must not cost every other column
            # its place. What has to survive is identification, and the full name
            # is a `--format json` away.
            assert name[:40] in out


def test_a_zero_byte_model_is_priced_at_zero_not_dropped(tmp_path: Path, capsys):
    _diff(
        tmp_path,
        "--format",
        "json",
        current=[make_node("m", compiled_code="CUR", checksum="n")],
        baseline_specs=[make_node("m", compiled_code="BASE", checksum="o")],
        responses={"CUR": 0, "BASE": 0},
    )
    model = json.loads(capsys.readouterr().out)["models"][0]
    assert model["bytes_current"] == 0
    assert model["usd_current"] == 0.0


@pytest.mark.parametrize(
    ("baseline", "current"),
    [
        (TIB, 2 * TIB),
        (2 * TIB, TIB),
        (TIB, TIB),
        (1, 10**15),
        (10**15, 1),
        (0, TIB),
    ],
)
def test_a_priced_delta_never_disagrees_in_sign_with_its_byte_delta(baseline, current):
    delta = _delta(
        bytes_baseline=baseline,
        bytes_current=current,
        usd_baseline=baseline / TIB * 6.25,
        usd_current=current / TIB * 6.25,
    )
    bytes_sign = (delta.bytes_delta > 0) - (delta.bytes_delta < 0)
    usd_sign = (delta.usd_per_run_delta > 0) - (delta.usd_per_run_delta < 0)
    assert bytes_sign == usd_sign


# --------------------------------------------------------------------------
# Confirmed defects.
# --------------------------------------------------------------------------


def test_growth_from_a_zero_byte_baseline_breaches_a_percentage_gate(tmp_path: Path):
    code = _diff(
        tmp_path,
        "--max-pct",
        "1",
        current=[make_node("m", compiled_code="CUR", checksum="n")],
        baseline_specs=[make_node("m", compiled_code="BASE", checksum="o")],
        responses={"CUR": 4 * TIB, "BASE": 0},
    )
    assert code == 1


def test_a_percentage_only_gate_says_it_cannot_cover_a_new_model(tmp_path: Path, capsys):
    """BUG-F20, resolved as a notice rather than a failure.

    A percentage needs a before and an after; a brand-new model has only an
    after, so `max_pct_increase` alone leaves it entirely ungated. Failing the
    run was the first instinct and is the wrong one — adding a model is an
    ordinary thing to do in a pull request, and blocking every such request
    teaches a team to switch the gate off. So it exits 0 and says, by name, which
    models went through unchecked and which two thresholds would have caught
    them. (The assertion here changed deliberately when the fix landed; it used
    to demand exit 1.)
    """
    code = _diff(
        tmp_path,
        "--max-pct",
        "1",
        current=[make_node("brand_new", compiled_code="CUR", checksum="n")],
        baseline_specs=[make_node("kept", compiled_code="KEPT", checksum="k")],
        responses={"CUR": 40 * TIB, "KEPT": TIB},
    )
    out = " ".join(capsys.readouterr().out.split())
    assert code == 0
    assert "new-models-not-percentage-gated" in out
    assert "brand_new went through ungated" in out
    assert "max_tib_total" in out


def test_that_notice_stays_quiet_once_a_baseline_free_threshold_is_set(tmp_path: Path, capsys):
    _diff(
        tmp_path,
        "--max-pct",
        "1",
        "--max-tib-total",
        "100",
        current=[make_node("brand_new", compiled_code="CUR", checksum="n")],
        baseline_specs=[make_node("kept", compiled_code="KEPT", checksum="k")],
        responses={"CUR": 40 * TIB, "KEPT": TIB},
    )
    assert "new-models-not-percentage-gated" not in capsys.readouterr().out


def test_a_sub_one_percent_breach_says_which_numbers_it_compared(tmp_path: Path, capsys):
    _diff(
        tmp_path,
        "--max-pct",
        "0.3",
        current=[make_node("m", compiled_code="CUR", checksum="n")],
        baseline_specs=[make_node("m", compiled_code="BASE", checksum="o")],
        responses={"CUR": 1_004_000_000, "BASE": 1_000_000_000},
    )
    out = capsys.readouterr().out
    assert "+0% exceeds 0%" not in out
    assert "0.4" in out

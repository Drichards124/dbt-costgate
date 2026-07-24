# SPDX-License-Identifier: Apache-2.0
from conftest import FakeDryRunner, make_manifest, make_node
from costgate import artifacts, estimate
from costgate.config import Config
from costgate.models import TIB, ErrorKind
from costgate.pricing import PricingTable


def _nodes(*specs):
    return artifacts.model_nodes(make_manifest(*specs))


def test_diff_mode_computes_before_after_delta():
    current = _nodes(make_node("m", compiled_code="CUR_m", checksum="new"))
    baseline = _nodes(make_node("m", compiled_code="BASE_m", checksum="old"))
    runner = FakeDryRunner({"CUR_m": 3 * TIB, "BASE_m": 1 * TIB})
    ests = estimate.estimate_models(
        ["model.pkg.m"], current, baseline, runner, current_dir=None, diff_mode=True
    )
    deltas = estimate.build_deltas(ests, PricingTable.load(), Config())
    d = deltas[0]
    assert d.bytes_baseline == TIB and d.bytes_current == 3 * TIB
    assert round(d.usd_per_run_delta, 2) == round(2 * 6.25, 2)
    assert d.gateable


def test_new_model_has_no_baseline():
    current = _nodes(make_node("new_m", compiled_code="CUR_new"))
    runner = FakeDryRunner({"CUR_new": TIB})
    ests = estimate.estimate_models(
        ["model.pkg.new_m"], current, {}, runner, current_dir=None, diff_mode=True
    )
    assert ests[0].is_new
    assert ests[0].bytes_baseline is None


def test_destination_missing_is_not_operational():
    current = _nodes(make_node("inc", materialized="incremental", compiled_code="CUR_inc"))
    runner = FakeDryRunner({"CUR_inc": ErrorKind.DESTINATION_MISSING})
    ests = estimate.estimate_models(
        ["model.pkg.inc"], current, {}, runner, current_dir=None, diff_mode=False
    )
    assert not estimate.has_only_operational_failures(ests)
    deltas = estimate.build_deltas(ests, PricingTable.load(), Config())
    assert deltas[0].error and "not built" in deltas[0].error
    assert not deltas[0].gateable


def test_all_operational_failures_flagged():
    current = _nodes(make_node("m", compiled_code="CUR_m"))
    runner = FakeDryRunner({"CUR_m": ErrorKind.PERMISSION})
    ests = estimate.estimate_models(
        ["model.pkg.m"], current, {}, runner, current_dir=None, diff_mode=False
    )
    assert estimate.has_only_operational_failures(ests)


def test_basis_mismatch_disables_gating():
    current = _nodes(
        make_node("inc", materialized="incremental", compiled_code="CUR_inc", checksum="new")
    )
    baseline = _nodes(
        make_node(
            "inc",
            materialized="incremental",
            compiled_code="select from `proj`.`analytics`.`inc`",
            relation_name="`proj`.`analytics`.`inc`",
            checksum="old",
        )
    )
    runner = FakeDryRunner({"CUR_inc": 2 * TIB, "`proj`.`analytics`.`inc`": TIB})
    ests = estimate.estimate_models(
        ["model.pkg.inc"], current, baseline, runner, current_dir=None, diff_mode=True
    )
    assert ests[0].basis_mismatch
    assert any("mixed basis" in w for w in ests[0].warnings)
    assert not ests[0].gateable


def test_rename_map_diffs_against_differently_named_baseline():
    current = _nodes(make_node("fct_orders_daily", compiled_code="CUR_daily", checksum="new"))
    baseline = _nodes(make_node("fct_orders_monthly", compiled_code="BASE_monthly", checksum="old"))
    renames = {"model.pkg.fct_orders_daily": "model.pkg.fct_orders_monthly"}
    runner = FakeDryRunner({"CUR_daily": 3 * TIB, "BASE_monthly": 1 * TIB})
    ests = estimate.estimate_models(
        ["model.pkg.fct_orders_daily"],
        current,
        baseline,
        runner,
        current_dir=None,
        diff_mode=True,
        renames=renames,
    )
    e = ests[0]
    assert not e.is_new
    assert e.bytes_baseline == TIB and e.bytes_current == 3 * TIB
    assert any("renamed baseline" in w and "fct_orders_monthly" in w for w in e.warnings)
    deltas = estimate.build_deltas(ests, PricingTable.load(), Config())
    assert round(deltas[0].usd_per_run_delta, 2) == round(2 * 6.25, 2)


def test_build_deltas_applies_exclude():
    current = _nodes(make_node("events", compiled_code="CUR_events"))
    runner = FakeDryRunner({"CUR_events": TIB})
    ests = estimate.estimate_models(
        ["model.pkg.events"], current, {}, runner, current_dir=None, diff_mode=False
    )
    cfg = Config(exclude=["events"])
    deltas = estimate.build_deltas(ests, PricingTable.load(), cfg)
    assert not deltas[0].gateable
    assert any("excluded" in w for w in deltas[0].warnings)

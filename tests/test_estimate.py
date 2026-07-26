# SPDX-License-Identifier: Apache-2.0
from conftest import FakeDryRunner, make_manifest, make_node
from dbt_costgate import artifacts, estimate
from dbt_costgate.config import Config
from dbt_costgate.models import TIB, ErrorKind
from dbt_costgate.pricing import PricingTable


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


def _no_compiled_code(spec):
    """A manifest node whose `compiled_code` really is null.

    `make_node(compiled_code=None)` cannot express this — None is its default and
    falls back to generated SQL, so asking for it yields an ordinary node and a
    test that quietly exercises a different case. Overridden on the dict instead.
    """
    uid, node = spec
    node["compiled_code"] = None
    return uid, node


def _baseline_without_compiled_sql(current_sql: str):
    """An incremental whose baseline node exists but carries no compiled code —
    a manifest built without the compile step, or trimmed of it."""
    current = _nodes(
        make_node(
            "inc",
            materialized="incremental",
            compiled_code=current_sql,
            relation_name="`proj`.`analytics`.`inc`",
            checksum="new",
        )
    )
    baseline = _nodes(
        _no_compiled_code(
            make_node(
                "inc",
                materialized="incremental",
                relation_name="`proj`.`analytics`.`inc`",
                checksum="old",
            )
        )
    )
    runner = FakeDryRunner({current_sql: 2 * TIB})
    return estimate.estimate_models(
        ["model.pkg.inc"], current, baseline, runner, current_dir=None, diff_mode=True
    )[0]


def test_a_baseline_with_no_compiled_sql_is_not_given_a_basis():
    """It was handed `full_refresh` — a shape named for SQL that does not exist.
    Not a silent internal default: it reached the user as
    `mixed basis — baseline is full_refresh`, a specific claim about a baseline
    that was never compiled, printed beside the warning saying exactly that."""
    est = _baseline_without_compiled_sql("select * from `proj`.`analytics`.`inc`")
    assert est.basis_baseline is None
    assert not est.basis_mismatch
    assert any("baseline has no compiled SQL" in w for w in est.warnings)
    assert not any("mixed basis" in w for w in est.warnings)


def test_a_missing_baseline_gates_the_same_way_whichever_shape_the_branch_compiled():
    """The bug this pins. `gateable` used to fall out of the basis-mismatch
    branch, so an absent baseline cleared it only when the *current* side came out
    incremental-form — the same missing baseline gating or not depending on how
    the branch happened to compile. Both shapes, one answer."""
    incremental_form = _baseline_without_compiled_sql("select * from `proj`.`analytics`.`inc`")
    full_refresh = _baseline_without_compiled_sql("select * from upstream")
    # Guards the premise: if both compiled to the same shape this would pass
    # while comparing nothing.
    assert incremental_form.basis_current != full_refresh.basis_current
    assert not incremental_form.gateable
    assert not full_refresh.gateable


def test_an_unestimated_model_carries_no_basis_to_label():
    """No compiled SQL on the *current* side either — nothing was dry-run, so
    there is no figure for a basis to describe."""
    current = _nodes(_no_compiled_code(make_node("inc", materialized="incremental")))
    ests = estimate.estimate_models(
        ["model.pkg.inc"], current, {}, FakeDryRunner({}), current_dir=None, diff_mode=False
    )
    assert ests[0].basis_current is None
    delta = estimate.build_deltas(ests, PricingTable.load(), Config())[0]
    assert delta.basis is None
    assert delta.is_incremental  # the model is still incremental; the figure is absent

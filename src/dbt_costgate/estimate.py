# SPDX-License-Identifier: Apache-2.0
"""Orchestrate dry-runs over the selected models and turn results into priced
deltas. Pure with respect to the warehouse — it drives a ``DryRunner``, which the
tests satisfy with a fake.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dbt_costgate import artifacts
from dbt_costgate.bigquery import DryRunner
from dbt_costgate.config import Config
from dbt_costgate.models import (
    ERROR_KIND_REASONS,
    CostDelta,
    ModelEstimate,
    ModelNode,
    SkipReason,
)
from dbt_costgate.pricing import PricingTable

# Not a warehouse failure but a local one, so it carries no ErrorKind. Named
# because _not_estimated_reason must recognise it explicitly: anything else
# arriving without a kind degrades to "not estimated" rather than being printed.
NO_COMPILED_SQL = "no compiled SQL for the current version (run `dbt compile`)"


def estimate_models(
    selected: list[str],
    current_nodes: dict[str, ModelNode],
    baseline_nodes: dict[str, ModelNode],
    runner: DryRunner,
    *,
    current_dir: Path | None,
    diff_mode: bool,
    threads: int = 8,
    renames: dict[str, str] | None = None,
    notes: dict[str, str] | None = None,
) -> list[ModelEstimate]:
    """`notes` explains, per unique_id, why a model was selected when its own file
    was not the thing that changed — an upstream macro, a config change, an
    ephemeral model it inlines. One mechanism rather than one flag per cause, so
    a model never lands in a report without an explanation beside it."""
    renames = renames or {}
    notes = notes or {}

    def work(uid: str) -> ModelEstimate:
        return _estimate_one(
            uid, current_nodes, baseline_nodes, runner, current_dir, diff_mode, renames, notes
        )

    if threads <= 1 or len(selected) <= 1:
        results = [work(uid) for uid in selected]
    else:
        with ThreadPoolExecutor(max_workers=threads) as pool:
            results = list(pool.map(work, selected))
    results.sort(key=lambda e: e.name)
    return results


def _estimate_one(
    uid: str,
    current_nodes: dict[str, ModelNode],
    baseline_nodes: dict[str, ModelNode],
    runner: DryRunner,
    current_dir: Path | None,
    diff_mode: bool,
    renames: dict[str, str],
    notes: dict[str, str],
) -> ModelEstimate:
    node = current_nodes.get(uid)
    if node is None:
        return _estimate_deleted(baseline_nodes[uid], runner)

    base = baseline_nodes.get(uid)
    renamed_base = None
    if base is None:
        renamed_uid = renames.get(uid)
        if renamed_uid:
            base = renamed_base = baseline_nodes.get(renamed_uid)
    est = ModelEstimate(node=node, is_new=diff_mode and base is None)

    current_sql = artifacts.resolve_compiled_sql(node, current_dir)
    est.basis_current = artifacts.detect_basis(node, current_sql)
    est.warnings = artifacts.sql_warnings(node, current_sql)
    if renamed_base is not None:
        est.warnings.append(f"compared against renamed baseline `{renamed_base.name}`")
    if uid in notes:
        est.warnings.append(notes[uid])

    if not current_sql:
        est.error_kind = None
        est.error_detail = NO_COMPILED_SQL
        est.skip_reason = SkipReason.NO_COMPILED_SQL
        return est

    res = runner.dry_run(current_sql, node.relation_name)
    if res.ok:
        est.bytes_current = res.total_bytes
        if res.location:
            est.node.location = res.location
    else:
        est.error_kind = res.error_kind
        est.error_detail = res.error_detail
        est.skip_reason = SkipReason.DRY_RUN_FAILED

    if diff_mode and base is not None:
        base_sql = base.compiled_code  # baseline: manifest only, no target dir
        est.basis_baseline = artifacts.detect_basis(base, base_sql)
        if base_sql:
            base_res = runner.dry_run(base_sql, base.relation_name)
            if base_res.ok:
                est.bytes_baseline = base_res.total_bytes
            else:
                est.warnings.append("baseline unavailable — could not dry-run the main version")
        else:
            est.warnings.append("baseline has no compiled SQL (compile main the same way)")
            # Decided here, where the condition is actually known. This used to
            # fall out of the basis-mismatch branch below: an absent baseline was
            # given a basis anyway, which collided with the current one often
            # enough to clear `gateable` — but only when the current side came out
            # incremental-form, so the same missing baseline gated or did not gate
            # depending on how the *branch* happened to compile. Ungateable is the
            # right answer either way: with no baseline bytes the delta maths read
            # the model as new, so the whole current scan looks like an increase,
            # and a threshold firing on that is firing on a missing measurement.
            est.skip_reason = SkipReason.NO_BASELINE_SQL

    if est.basis_mismatch:
        est.warnings.append(
            f"mixed basis — baseline is {est.basis_baseline.value}, current is "
            f"{est.basis_current.value}; recompile the baseline the same way"
        )
        est.skip_reason = SkipReason.BASIS_MISMATCH

    return est


def _estimate_deleted(base: ModelNode, runner: DryRunner) -> ModelEstimate:
    """A model in the baseline and gone from the branch.

    Its current scan is zero because it no longer runs, so the delta is the whole
    of what it used to cost — which is the point: removing a model is the most
    direct cost reduction a change can make, and it used to be invisible.

    Never gated. A deletion cannot increase anything, so no threshold applies to
    it, and it must not become a "could not check" failure when its baseline
    happens not to dry-run either.
    """
    est = ModelEstimate(node=base, is_deleted=True, skip_reason=SkipReason.DELETED)
    est.bytes_current = 0
    est.basis_current = est.basis_baseline = artifacts.detect_basis(base, base.compiled_code)
    if not base.compiled_code:
        est.warnings.append("deleted — the baseline has no compiled SQL, so the saving is unpriced")
        return est
    res = runner.dry_run(base.compiled_code, base.relation_name)
    if res.ok:
        est.bytes_baseline = res.total_bytes
        if res.location:
            est.node.location = res.location
    else:
        est.warnings.append(
            "deleted — its baseline could not be dry-run, so the saving is unpriced"
        )
    return est


def build_deltas(
    estimates: list[ModelEstimate], table: PricingTable, config: Config
) -> list[CostDelta]:
    """Price each estimate and apply user exclusions to gateability."""
    deltas: list[CostDelta] = []
    for est in estimates:
        region_hint = est.node.location
        usd_current = usd_baseline = None
        applied_region = table.rate_for(region_hint).region
        if est.bytes_current is not None:
            usd_current, rate = table.usd(est.bytes_current, region_hint)
            applied_region = rate.region
        if est.bytes_baseline is not None:
            usd_baseline, _ = table.usd(est.bytes_baseline, region_hint)

        # A user's exclusion overrides whatever else stopped this model being
        # gated, and that is what makes `exclude:` a working escape hatch: a
        # model whose dry-run always fails can be accepted by name instead of
        # failing every run.
        skip_reason = est.skip_reason
        warnings = list(est.warnings)
        if est.name in config.exclude:
            skip_reason = SkipReason.EXCLUDED
            warnings.append("excluded from gating by config")
        elif est.name in config.warn_only:
            skip_reason = SkipReason.WARN_ONLY
            warnings.append("warn-only by config")
        elif skip_reason is None and est.bytes_current is None:
            skip_reason = SkipReason.DRY_RUN_FAILED

        error = None
        if est.bytes_current is None:
            error = _not_estimated_reason(est)

        deltas.append(
            CostDelta(
                name=est.name,
                unique_id=est.node.unique_id,
                is_incremental=est.is_incremental,
                is_new=est.is_new,
                is_deleted=est.is_deleted,
                basis=est.basis_current,
                gateable=skip_reason is None,
                skip_reason=skip_reason,
                bytes_baseline=est.bytes_baseline,
                bytes_current=est.bytes_current,
                usd_baseline=usd_baseline,
                usd_current=usd_current,
                region=applied_region,
                warnings=warnings,
                error=error,
                runs_per_month=config.runs_per_month(est.name),
            )
        )
    # Most expensive first, with bytes as the tie-break rather than an
    # afterthought: under slot pricing every rate is 0, so every dollar delta is
    # 0.0 as well, and sorting on money alone left an unpriced report in whatever
    # order the models arrived in — the 2.91 TiB model listed below the 412 MiB
    # one. The byte delta ranks a diff, the current scan ranks a run with no
    # baseline to have a delta from.
    deltas.sort(
        key=lambda d: (d.usd_per_run_delta or 0.0, d.bytes_delta or 0, d.bytes_current or 0),
        reverse=True,
    )
    return deltas


def _not_estimated_reason(est: ModelEstimate) -> str:
    """The reason a *report* may show. Never includes ``error_detail``.

    ``error_detail`` holds BigQuery's own message, which quotes the query it was
    given — and compiled SQL can embed secrets templated via ``env_var()``/vars.
    Reports become pull-request comments, so the message is named by kind here
    and printed raw only to stderr (see ``cli._log_error_details``).
    """
    if est.error_kind is not None:
        return f"not estimated — {ERROR_KIND_REASONS[est.error_kind]}"
    if est.error_detail == NO_COMPILED_SQL:
        return NO_COMPILED_SQL
    return "not estimated"


def has_only_operational_failures(estimates: list[ModelEstimate]) -> bool:
    """True when nothing was estimable AND at least one failure was operational
    (permission/invalid_sql/transient/other) — the exit-2 condition. A run made
    up only of expected destination_missing / no-SQL cases returns False."""
    if any(e.bytes_current is not None for e in estimates):
        return False
    return any(e.error_kind is not None and e.error_kind.is_operational for e in estimates)

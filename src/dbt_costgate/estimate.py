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
from dbt_costgate.models import ERROR_KIND_REASONS, CostDelta, ModelEstimate, ModelNode
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
    indirect: set[str] | None = None,
) -> list[ModelEstimate]:
    renames = renames or {}
    indirect = indirect or set()

    def work(uid: str) -> ModelEstimate:
        return _estimate_one(
            uid, current_nodes, baseline_nodes, runner, current_dir, diff_mode, renames, indirect
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
    indirect: set[str],
) -> ModelEstimate:
    node = current_nodes[uid]
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
    if uid in indirect:
        est.warnings.append(
            "compiled SQL changed but the model file didn't — an upstream macro or a config change"
        )

    if not current_sql:
        est.error_kind = None
        est.error_detail = NO_COMPILED_SQL
        est.gateable = False
        return est

    res = runner.dry_run(current_sql, node.relation_name)
    if res.ok:
        est.bytes_current = res.total_bytes
        if res.location:
            est.node.location = res.location
    else:
        est.error_kind = res.error_kind
        est.error_detail = res.error_detail
        est.gateable = False

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
            est.gateable = False

    if est.basis_mismatch:
        est.warnings.append(
            f"mixed basis — baseline is {est.basis_baseline.value}, current is "
            f"{est.basis_current.value}; recompile the baseline the same way"
        )
        est.gateable = False

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

        gateable = est.gateable
        warnings = list(est.warnings)
        if est.name in config.exclude:
            gateable = False
            warnings.append("excluded from gating by config")
        elif est.name in config.warn_only:
            gateable = False
            warnings.append("warn-only by config")

        error = None
        if est.bytes_current is None:
            error = _not_estimated_reason(est)

        deltas.append(
            CostDelta(
                name=est.name,
                unique_id=est.node.unique_id,
                is_incremental=est.is_incremental,
                is_new=est.is_new,
                basis=est.basis_current,
                gateable=gateable and est.bytes_current is not None,
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
    deltas.sort(key=lambda d: d.usd_per_run_delta or 0.0, reverse=True)
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

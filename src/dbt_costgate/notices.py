# SPDX-License-Identifier: Apache-2.0
"""Run-level advisories: configuration that cannot do what it looks like it does.

Distinct from `CostDelta.warnings`, which describe a *model*. A notice describes
the *run's configuration* — a threshold that can never fire, a rate that was
guessed. They are collected after the verdict and never read by
`policy.evaluate`, so a notice can never change a status or an exit code.

Adding one means adding a single `_CHECKS` entry. The id, the check, the
validated set of silenceable ids, and the documented list all derive from that
one line, so there is no second place to forget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from dbt_costgate import policy
from dbt_costgate.models import (
    CostDelta,
    Notice,
    PricingDisclosure,
    estimated,
    monthly_scan_bytes,
)

if TYPE_CHECKING:  # pragma: no cover
    from dbt_costgate.config import Config
    from dbt_costgate.pricing import PricingTable

Check = Callable[["Config", "PricingTable", PricingDisclosure, "list[CostDelta]"], "str | None"]

# id -> the check that produces its message (or None when it does not apply).
# Ids are a public contract: they appear in reports, in `notices.silence`, and in
# the JSON payload, so renaming one breaks a user's config. Retire an id by
# leaving it here inert rather than deleting it, so an existing `silence` entry
# keeps validating.
_CHECKS: tuple[tuple[str, Check], ...] = (
    (
        "dead-money-thresholds",
        lambda config, table, disclosure, deltas: policy.unpriced_threshold_notice(
            config.thresholds, disclosure.priced
        ),
    ),
    (
        "unverified-region-rate",
        lambda config, table, disclosure, deltas: table.fallback_notice(disclosure.region_sources),
    ),
    (
        "new-models-not-percentage-gated",
        lambda config, table, disclosure, deltas: _new_models_ungated(config, deltas),
    ),
    (
        "free-tier-needs-run-frequency",
        lambda config, table, disclosure, deltas: _free_tier_uncomputable(config, deltas),
    ),
)


def _free_tier_uncomputable(config: Config, deltas: list[CostDelta]) -> str | None:
    """A declared free-tier allowance with no monthly figure to compare it to.

    `pricing.free_tib_per_month` is a statement about a *month*, so it needs a
    monthly scan total — which needs a run frequency for every model. Without
    one the key is set, appears in the header, and then silently does the rest of
    nothing: the same shape as a dollar threshold under slot pricing, which is
    what this module exists for.

    Keyed on the computed result rather than on `run_frequency` being absent,
    because a default that leaves one model uncovered fails in exactly the same
    way and would otherwise slip through.

    Silent when nothing was estimated. That produces the same `None`, but the fix
    is not a run frequency, and a run with nothing to report does not need
    another line telling it so.
    """
    if config.free_tib_per_month is None:
        return None
    rows = estimated(deltas)
    if not rows or monthly_scan_bytes(deltas) is not None:
        return None
    return (
        "pricing.free_tib_per_month is an allowance per month, and no monthly figure could be "
        "worked out to compare it against, so the report can only name it. Set "
        "run_frequency.default (or a run_frequency.models entry for every model in the change) "
        "for how many times a month these models run."
    )


def _new_models_ungated(config: Config, deltas: list[CostDelta]) -> str | None:
    """`max_pct_increase` alone cannot gate a model that has no baseline.

    A percentage needs a before and an after, and a brand-new model has only an
    after — so on a repo gated only on percent, adding a model is entirely
    ungated and nothing says so. It is not a failed check: adding models is
    ordinary, and blocking every pull request that does would teach a team to
    turn the gate off. It is a gap in the configuration, which is what a notice
    is for, and the two thresholds that do work without a baseline are named.
    """
    thr = config.thresholds
    percent_only = thr.max_pct_increase is not None and not any(
        getattr(thr, key) is not None
        for key in (
            "max_usd_increase_per_run",
            "max_usd_increase_per_month",
            "max_usd_total",
            "max_tib_total",
        )
    )
    if not percent_only:
        return None
    new = sorted(d.name for d in deltas if d.is_new)
    if not new:
        return None
    return (
        f"thresholds.max_pct_increase cannot gate a new model — there is no baseline to grow "
        f"from — so {', '.join(new)} went through ungated. Add thresholds.max_usd_total or "
        f"thresholds.max_tib_total, which need no baseline."
    )


NOTICE_IDS: tuple[str, ...] = tuple(notice_id for notice_id, _ in _CHECKS)


def advisory_tail(notice_id: str) -> str:
    """The sentence every notice ends with.

    Composed here from the registry id rather than written into each check, so a
    notice can never tell a user to silence an id that is not its own.
    """
    return (
        f"Advisory only — it does not affect the gate or the exit code. "
        f"Silence it with notices.silence: [{notice_id}]."
    )


def collect(
    config: Config,
    table: PricingTable,
    disclosure: PricingDisclosure,
    deltas: list[CostDelta] | None = None,
) -> list[Notice]:
    silenced = set(config.silence_notices)
    out: list[Notice] = []
    for notice_id, check in _CHECKS:
        if notice_id in silenced:
            continue
        message = check(config, table, disclosure, deltas or [])
        if message:
            out.append(Notice(id=notice_id, message=f"{message} {advisory_tail(notice_id)}"))
    return out


def unknown_ids(silenced: list[str]) -> list[str]:
    """Ids in `notices.silence` that name nothing.

    Reported as an error rather than ignored: a typo here silently re-enables a
    warning the user believes they turned off, or — worse on a shared config —
    reads as "handled" when nothing was ever silenced.
    """
    return sorted(set(silenced) - set(NOTICE_IDS))

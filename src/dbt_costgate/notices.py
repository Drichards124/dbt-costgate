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
from dbt_costgate.models import Notice, PricingDisclosure

if TYPE_CHECKING:  # pragma: no cover
    from dbt_costgate.config import Config
    from dbt_costgate.pricing import PricingTable

# id -> the check that produces its message (or None when it does not apply).
# Ids are a public contract: they appear in reports, in `notices.silence`, and in
# the JSON payload, so renaming one breaks a user's config. Retire an id by
# leaving it here inert rather than deleting it, so an existing `silence` entry
# keeps validating.
_CHECKS: tuple[tuple[str, Callable[[Config, PricingTable, PricingDisclosure], str | None]], ...] = (
    (
        "dead-money-thresholds",
        lambda config, table, disclosure: policy.unpriced_threshold_notice(
            config.thresholds, disclosure.priced
        ),
    ),
    (
        "unverified-region-rate",
        lambda config, table, disclosure: table.fallback_notice(disclosure.region_sources),
    ),
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


def collect(config: Config, table: PricingTable, disclosure: PricingDisclosure) -> list[Notice]:
    silenced = set(config.silence_notices)
    out: list[Notice] = []
    for notice_id, check in _CHECKS:
        if notice_id in silenced:
            continue
        message = check(config, table, disclosure)
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

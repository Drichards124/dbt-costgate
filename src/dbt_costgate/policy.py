# SPDX-License-Identifier: Apache-2.0
"""Threshold evaluation → verdict and exit code.

Only gateable deltas with an established current estimate can breach. Un-estimated
or user-excluded models are reported but never cause a failure.
"""

from __future__ import annotations

from dbt_costgate.config import Config, Thresholds
from dbt_costgate.models import TIB, CostDelta, Status, Verdict

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_OPERATIONAL = 2


def evaluate(deltas: list[CostDelta], config: Config, currency: str = "USD") -> Verdict:
    """Evaluate thresholds. `currency` only labels the amounts in breach messages;
    it never affects whether a threshold fires."""
    thr = config.thresholds
    breaches: list[str] = []
    for d in deltas:
        if not d.gateable:
            continue
        breaches.extend(_breaches_for(d, thr, currency))

    if not breaches:
        return Verdict(status=Status.PASS, exit_code=EXIT_OK)

    # fail_on decides whether breaches block (fail) or only warn.
    if config.fail_on == "never":
        return Verdict(status=Status.WARN, breaches=breaches, exit_code=EXIT_OK)
    if config.fail_on == "warn":
        return Verdict(status=Status.WARN, breaches=breaches, exit_code=EXIT_GATE_FAILED)
    return Verdict(status=Status.FAIL, breaches=breaches, exit_code=EXIT_GATE_FAILED)


def _breaches_for(d: CostDelta, thr: Thresholds, currency: str = "USD") -> list[str]:
    out: list[str] = []
    cur = currency
    run_delta = d.usd_per_run_delta
    if thr.max_usd_increase_per_run is not None and run_delta is not None:
        if run_delta > thr.max_usd_increase_per_run:
            out.append(
                f"{d.name}: +{cur} {run_delta:,.2f}/run exceeds "
                f"{cur} {thr.max_usd_increase_per_run:,.2f}"
            )
    if thr.max_pct_increase is not None and d.pct_delta is not None:
        if d.pct_delta > thr.max_pct_increase:
            out.append(f"{d.name}: +{d.pct_delta:,.0f}% exceeds {thr.max_pct_increase:,.0f}%")
    month_delta = d.usd_per_month_delta
    if thr.max_usd_increase_per_month is not None and month_delta is not None:
        if month_delta > thr.max_usd_increase_per_month:
            out.append(
                f"{d.name}: +{cur} {month_delta:,.2f}/month exceeds "
                f"{cur} {thr.max_usd_increase_per_month:,.2f}"
            )
    # Absolute ceilings: gate the total per-run cost/scan, not the increase.
    if thr.max_usd_total is not None and d.usd_current is not None:
        if d.usd_current > thr.max_usd_total:
            out.append(
                f"{d.name}: {cur} {d.usd_current:,.2f}/run exceeds cap "
                f"{cur} {thr.max_usd_total:,.2f}"
            )
    if thr.max_tib_total is not None and d.bytes_current is not None:
        tib = d.bytes_current / TIB
        if tib > thr.max_tib_total:
            out.append(f"{d.name}: {tib:,.2f} TiB/run exceeds cap {thr.max_tib_total:,.2f} TiB")
    return out

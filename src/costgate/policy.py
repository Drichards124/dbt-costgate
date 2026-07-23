# SPDX-License-Identifier: Apache-2.0
"""Threshold evaluation → verdict and exit code.

Only gateable deltas with an established current estimate can breach. Un-estimated
or user-excluded models are reported but never cause a failure.
"""

from __future__ import annotations

from costgate.config import Config, Thresholds
from costgate.models import CostDelta, Status, Verdict

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_OPERATIONAL = 2


def evaluate(deltas: list[CostDelta], config: Config) -> Verdict:
    thr = config.thresholds
    breaches: list[str] = []
    for d in deltas:
        if not d.gateable:
            continue
        breaches.extend(_breaches_for(d, thr))

    if not breaches:
        return Verdict(status=Status.PASS, exit_code=EXIT_OK)

    # fail_on decides whether breaches block (fail) or only warn.
    if config.fail_on == "never":
        return Verdict(status=Status.WARN, breaches=breaches, exit_code=EXIT_OK)
    if config.fail_on == "warn":
        return Verdict(status=Status.WARN, breaches=breaches, exit_code=EXIT_GATE_FAILED)
    return Verdict(status=Status.FAIL, breaches=breaches, exit_code=EXIT_GATE_FAILED)


def _breaches_for(d: CostDelta, thr: Thresholds) -> list[str]:
    out: list[str] = []
    run_delta = d.usd_per_run_delta
    if thr.max_usd_increase_per_run is not None and run_delta is not None:
        if run_delta > thr.max_usd_increase_per_run:
            out.append(
                f"{d.name}: +${run_delta:,.2f}/run exceeds ${thr.max_usd_increase_per_run:,.2f}"
            )
    if thr.max_pct_increase is not None and d.pct_delta is not None:
        if d.pct_delta > thr.max_pct_increase:
            out.append(f"{d.name}: +{d.pct_delta:,.0f}% exceeds {thr.max_pct_increase:,.0f}%")
    month_delta = d.usd_per_month_delta
    if thr.max_usd_increase_per_month is not None and month_delta is not None:
        if month_delta > thr.max_usd_increase_per_month:
            out.append(
                f"{d.name}: +${month_delta:,.2f}/month exceeds "
                f"${thr.max_usd_increase_per_month:,.2f}"
            )
    return out

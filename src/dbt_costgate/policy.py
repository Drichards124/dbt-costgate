# SPDX-License-Identifier: Apache-2.0
"""Threshold evaluation → verdict and exit code.

Only gateable deltas with an established current estimate can breach. Un-estimated
or user-excluded models are reported but never cause a failure.
"""

from __future__ import annotations

from dbt_costgate.config import Config, Thresholds
from dbt_costgate.models import TIB, CostDelta, Status, Verdict, format_money, format_pct

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


# Money thresholds, in declaration order. Each compares against a figure derived
# from a rate, so all of them are inert once that rate is 0.
_MONEY_THRESHOLDS = (
    "max_usd_increase_per_run",
    "max_usd_increase_per_month",
    "max_usd_total",
)


def unpriced_threshold_notice(thr: Thresholds, priced: bool) -> str | None:
    """Warn when a money threshold is configured but cannot possibly fire.

    Setting the rate to 0 is the documented way to run on capacity/slots, where
    no per-byte price exists. It also silently makes every dollar threshold
    inert: the figures they compare against are all 0.00, so nothing exceeds
    anything and the gate passes while looking configured. That is the same
    shape as the bug where a 0 rate disabled the percentage threshold — a gate
    that reports success because it has nothing to measure.

    Advisory only. A team that has decided to price at 0 and leave the dollar
    thresholds in place is not doing anything invalid, so this never blocks;
    it only refuses to let the situation go unstated. Returns the core message —
    `notices.collect` appends the advisory tail.
    """
    if priced:
        return None
    dead = [k for k in _MONEY_THRESHOLDS if getattr(thr, k) is not None]
    if not dead:
        return None
    keys = ", ".join(f"thresholds.{k}" for k in dead)
    return (
        f"{keys} cannot fire: no per-byte price is configured, so every cost on this run is 0.00 "
        f"and no dollar figure can exceed a limit. Gate on scanned bytes instead with "
        f"thresholds.max_pct_increase or thresholds.max_tib_total."
    )


def _breaches_for(d: CostDelta, thr: Thresholds, currency: str = "USD") -> list[str]:
    out: list[str] = []
    cur = currency
    run_delta = d.usd_per_run_delta
    if thr.max_usd_increase_per_run is not None and run_delta is not None:
        if run_delta > thr.max_usd_increase_per_run:
            out.append(
                f"{d.name}: {format_money(run_delta, cur, signed=True)}/run exceeds "
                f"{format_money(thr.max_usd_increase_per_run, cur)}"
            )
    if thr.max_pct_increase is not None and d.pct_delta is not None:
        if d.pct_delta > thr.max_pct_increase:
            out.append(
                f"{d.name}: {format_pct(d.pct_delta)} exceeds "
                f"{format_pct(thr.max_pct_increase, signed=False)}"
            )
    month_delta = d.usd_per_month_delta
    if thr.max_usd_increase_per_month is not None and month_delta is not None:
        if month_delta > thr.max_usd_increase_per_month:
            out.append(
                f"{d.name}: {format_money(month_delta, cur, signed=True)}/month exceeds "
                f"{format_money(thr.max_usd_increase_per_month, cur)}"
            )
    # Absolute ceilings: gate the total per-run cost/scan, not the increase.
    if thr.max_usd_total is not None and d.usd_current is not None:
        if d.usd_current > thr.max_usd_total:
            out.append(
                f"{d.name}: {format_money(d.usd_current, cur)}/run exceeds cap "
                f"{format_money(thr.max_usd_total, cur)}"
            )
    if thr.max_tib_total is not None and d.bytes_current is not None:
        tib = d.bytes_current / TIB
        if tib > thr.max_tib_total:
            out.append(f"{d.name}: {tib:,.2f} TiB/run exceeds cap {thr.max_tib_total:,.2f} TiB")
    return out

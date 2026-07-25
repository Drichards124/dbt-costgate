# SPDX-License-Identifier: Apache-2.0
"""Render a Report as terminal text, GitHub-flavored markdown, or JSON.

Every renderer carries the same disclosure footer (region + rate + source) and a
reminder that nothing was executed and no SQL is shown.
"""

from __future__ import annotations

import json

from dbt_costgate.models import CostDelta, PricingDisclosure, Report, Status, format_money

_DRYRUN_NOTE = "Estimates from BigQuery dry-run — nothing executed, no bytes billed, no SQL shown."


def humanize_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    step = 1024.0
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    size = float(n)
    for unit in units:
        if size < step or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= step
    return f"{size:.2f} PiB"  # pragma: no cover


# One money formatter for the whole project, shared with policy.py's breach
# messages. The code goes inline on every amount rather than once in a column
# header, so a single quoted row or grepped line is never ambiguous.
_money = format_money


def _rate(currency: str, rate: float) -> str:
    """The per-TiB rate, rendered once. Appears in both the header and the
    disclosure footer, which must not disagree about how a rate looks."""
    return f"{currency} {rate:,.2f}/TiB"


_SOURCE_MARKERS = {
    "user-override": "override",
    "region-table": "table",
    "default-fallback": "fallback",
}


def _disclosure_line(d: PricingDisclosure) -> str:
    # When a report mixes provenance across regions, tag each region so the
    # aggregate label ("built-in table + user override") is not ambiguous.
    if not d.priced:
        regions = ", ".join(d.regions) or "—"
        return (
            f"Pricing: none applied — rate is 0 for {regions}, so this report measures "
            f"scanned bytes only. Slot/capacity cost cannot be estimated before a query runs."
        )
    mixed = len(set(d.region_sources.values())) > 1
    parts = []
    for r, rate in d.regions.items():
        token = f"{r} {_rate(d.currency, rate)}"
        marker = _SOURCE_MARKERS.get(d.region_sources.get(r, "")) if mixed else None
        if marker:
            token += f" ({marker})"
        parts.append(token)
    regions = " · ".join(parts) or "—"
    return f"Pricing: {regions} · {d.source} (table {d.table_version}, verified {d.last_verified})"


def _delta_cell(d: CostDelta, currency: str) -> str:
    if d.error:
        return "not estimated"
    return _money(d.usd_per_run_delta, currency, signed=True)


def _pct_cell(d: CostDelta) -> str:
    if d.error:
        return "not estimated"
    return "—" if d.pct_delta is None else f"{d.pct_delta:+,.0f}%"


def _net_line(report: Report) -> str | None:
    """One line naming what the change does overall, and in which direction.

    Says "saving" or "increase" in words rather than leaving a reader to notice a
    minus sign, so work that *reduces* cost is reported as an outcome instead of
    as the absence of a failure. Returns None when there is nothing to total:
    absolute mode has no baseline, so it has no net *change* to report.
    """
    if report.mode != "diff":
        return None
    rows = report.estimated
    if not rows:
        return None

    priced = report.disclosure.priced
    cur = report.disclosure.currency
    per_run = report.net_usd_per_run if priced else None
    net_bytes = report.net_bytes

    # Direction comes from money when priced and from bytes otherwise, so the
    # word and the figures beside it can never disagree.
    signal = per_run if priced else net_bytes
    if not signal:
        label, figures = "Net change", "none"
    else:
        label = "Net saving" if signal < 0 else "Net increase"
        if priced:
            parts = [f"{_money(abs(per_run), cur)}/run"]
            month = report.net_usd_per_month
            if month is not None:
                parts.append(f"{_money(abs(month), cur)}/month")
            figures = " · ".join(parts)
        else:
            figures = f"{humanize_bytes(abs(net_bytes))}/run scanned"

    skipped = report.unestimated_count
    caveat = (
        f" (across {len(rows)} of {len(report.deltas)} models; the rest were not estimated)"
        if skipped
        else ""
    )
    return f"{label}: {figures}{caveat}"


def render_terminal(report: Report) -> str:
    lines: list[str] = []
    d0 = report.disclosure
    regions = ", ".join(d0.regions) or "—"
    rate = next(iter(d0.regions.values()), None)
    header = f"dbt-costgate — region: {regions}"
    if not d0.priced:
        header += " · bytes only (no per-byte price configured)"
    elif rate is not None:
        header += f" · on-demand {_rate(d0.currency, rate)} · {d0.source}"
    lines.append(header)
    lines.append("")

    if not report.deltas:
        lines.append("  No changed models to estimate.")
        lines.append("")
        lines.append(f"  {_DRYRUN_NOTE}")
        return "\n".join(lines)

    diff = report.mode == "diff"
    for d in report.deltas:
        flags = []
        if d.is_new:
            flags.append("new")
        if d.is_incremental:
            flags.append("full-refresh")
        flag_str = f"  ({', '.join(flags)})" if flags else ""
        cur = d0.currency
        if diff:
            # The percentage leads in both cases; unpriced simply has no money after
            # it. Keeps terminal and markdown reporting the same set of figures.
            tail = f"{_pct_cell(d)}"
            if d0.priced:
                tail += f"   {_delta_cell(d, cur)}/run"
                if d.usd_per_month_delta is not None:
                    tail += (
                        f"   {_money(d.usd_per_month_delta, cur, signed=True)}/month "
                        f"({d.runs_per_month} runs)"
                    )
            lines.append(
                f"  {d.name}{flag_str}: "
                f"{humanize_bytes(d.bytes_baseline)} → {humanize_bytes(d.bytes_current)}   "
                f"{tail}"
            )
        elif not d0.priced:
            lines.append(f"  {d.name}{flag_str}: {humanize_bytes(d.bytes_current)} scanned")
        else:
            cost = "not estimated" if d.error else f"{_money(d.usd_current, cur)}/run"
            monthly = ""
            if d.usd_current is not None and d.runs_per_month:
                month_money = _money(d.usd_current * d.runs_per_month, cur)
                monthly = f"   {month_money}/month ({d.runs_per_month} runs)"
            lines.append(
                f"  {d.name}{flag_str}: {humanize_bytes(d.bytes_current)} scanned   {cost}{monthly}"
            )
        for w in d.warnings:
            lines.append(f"      ⚠ {w}")
        if d.error:
            lines.append(f"      • {d.error}")

    net = _net_line(report)
    if net:
        lines.append("")
        lines.append(f"  {net}")

    lines.append("")
    v = report.verdict
    if v.status == Status.PASS:
        lines.append("  GATE: PASS")
    else:
        label = "FAIL" if v.status == Status.FAIL else "WARN"
        lines.append(f"  GATE: {label}")
        for b in v.breaches:
            lines.append(f"    - {b}")
    lines.append("")
    lines.append(f"  {_disclosure_line(d0)}")
    lines.append(f"  {_DRYRUN_NOTE}")
    return "\n".join(lines)


def render_markdown(report: Report) -> str:
    d0 = report.disclosure
    diff = report.mode == "diff"
    out: list[str] = []
    n = len(report.deltas)
    plural = "s" if n != 1 else ""
    out.append(f"### 💸 dbt-costgate — cost impact of this change ({n} model{plural})")
    out.append("")
    if not report.deltas:
        out.append("_No changed models to estimate._")
        out.append("")
        out.append(f"_{_DRYRUN_NOTE}_")
        return "\n".join(out)

    cur = d0.currency
    if diff and not d0.priced:
        # Explicitly `Δ %`: a bare `Δ` would mean money in the priced shape and a
        # percentage here, which is the same column heading meaning two things.
        out.append("| Model | Baseline | This change | Δ % |")
        out.append("|---|--:|--:|--:|")
        for d in report.deltas:
            out.append(
                f"| {_md_name(d)} | {humanize_bytes(d.bytes_baseline)} | "
                f"{humanize_bytes(d.bytes_current)} | {_pct_cell(d)} |"
            )
    elif diff:
        # `Δ %` leads the deltas and appears in both shapes, so the unpriced table is
        # this one minus its money columns rather than a differently-shaped table.
        out.append("| Model | Baseline | This change | Δ % | Δ / run | Δ / month |")
        out.append("|---|--:|--:|--:|--:|--:|")
        for d in report.deltas:
            month = (
                _money(d.usd_per_month_delta, cur, signed=True)
                if d.usd_per_month_delta is not None
                else "—"
            )
            out.append(
                f"| {_md_name(d)} | {humanize_bytes(d.bytes_baseline)} | "
                f"{humanize_bytes(d.bytes_current)} | {_pct_cell(d)} | "
                f"{_delta_cell(d, cur)} | {month} |"
            )
    elif not d0.priced:
        out.append("| Model | Scanned |")
        out.append("|---|--:|")
        for d in report.deltas:
            out.append(f"| {_md_name(d)} | {humanize_bytes(d.bytes_current)} |")
    else:
        # Column headings stay currency-neutral; every cell carries its own code.
        out.append("| Model | Scanned | Cost / run | Cost / month |")
        out.append("|---|--:|--:|--:|")
        for d in report.deltas:
            cost = "not estimated" if d.error else _money(d.usd_current, cur)
            month = (
                _money((d.usd_current or 0) * d.runs_per_month, cur)
                if d.usd_current is not None and d.runs_per_month
                else "—"
            )
            out.append(f"| {_md_name(d)} | {humanize_bytes(d.bytes_current)} | {cost} | {month} |")

    caveats = [(d.name, w) for d in report.deltas for w in d.warnings]
    errors = [(d.name, d.error) for d in report.deltas if d.error]
    if caveats or errors:
        out.append("")
        for name, w in caveats:
            out.append(f"> ⚠ **{name}** — {w}")
        for name, e in errors:
            out.append(f"> • **{name}** — {e}")

    net = _net_line(report)
    if net:
        out.append("")
        label, _, rest = net.partition(": ")
        out.append(f"**{label}:** {rest}")

    out.append("")
    v = report.verdict
    if v.status == Status.PASS:
        out.append("✅ **Gate: PASS**")
    else:
        label = "FAIL" if v.status == Status.FAIL else "WARN"
        icon = "❌" if v.status == Status.FAIL else "⚠️"
        out.append(f"{icon} **Gate: {label}**")
        for b in v.breaches:
            out.append(f"- {b}")

    out.append("")
    out.append(f"<sub>{_disclosure_line(d0)}<br/>{_DRYRUN_NOTE}</sub>")
    return "\n".join(out)


def _md_name(d: CostDelta) -> str:
    tags = []
    if d.is_new:
        tags.append("new")
    if d.is_incremental:
        tags.append("full-refresh")
    suffix = f" _{', '.join(tags)}_" if tags else ""
    return f"`{d.name}`{suffix}"


def render_json(report: Report) -> str:
    d0 = report.disclosure
    payload = {
        "mode": report.mode,
        "verdict": {
            "status": report.verdict.status.value,
            "exit_code": report.verdict.exit_code,
            "breaches": report.verdict.breaches,
        },
        "pricing": {
            "regions": d0.regions,
            "region_sources": d0.region_sources,
            "source": d0.source,
            "table_version": d0.table_version,
            "last_verified": d0.last_verified,
            # `currency` is the ISO 4217 code the `usd_*` model fields are in. Those
            # key names predate currency support and stay put: they are a published
            # contract, and renaming them would break every existing consumer.
            "currency": d0.currency,
            "priced": d0.priced,
        },
        # Signed: negative is a saving. Only meaningful in diff mode, which is why
        # every field is null in absolute mode — there is no baseline to net against.
        "net": {
            "bytes": report.net_bytes if report.mode == "diff" else None,
            "usd_per_run": report.net_usd_per_run if report.mode == "diff" else None,
            "usd_per_month": report.net_usd_per_month if report.mode == "diff" else None,
            "models_estimated": len(report.estimated),
            "models_total": len(report.deltas),
        },
        "models": [
            {
                "name": d.name,
                "unique_id": d.unique_id,
                "is_incremental": d.is_incremental,
                "is_new": d.is_new,
                "gateable": d.gateable,
                "region": d.region,
                "bytes_baseline": d.bytes_baseline,
                "bytes_current": d.bytes_current,
                "usd_baseline": d.usd_baseline,
                "usd_current": d.usd_current,
                "usd_per_run_delta": d.usd_per_run_delta,
                "usd_per_month_delta": d.usd_per_month_delta,
                "pct_delta": d.pct_delta,
                "runs_per_month": d.runs_per_month,
                "warnings": d.warnings,
                "error": d.error,
            }
            for d in report.deltas
        ],
    }
    return json.dumps(payload, indent=2)


def render(report: Report, fmt: str) -> str:
    if fmt == "markdown":
        return render_markdown(report)
    if fmt == "json":
        return render_json(report)
    return render_terminal(report)

# SPDX-License-Identifier: Apache-2.0
"""Render a Report as terminal text, GitHub-flavored markdown, or JSON.

Every renderer carries the same disclosure footer (region + rate + source) and a
reminder that nothing was executed and no SQL is shown.
"""

from __future__ import annotations

import json

from dbt_costgate.layout import (
    DEFAULT_WIDTH,
    MIN_TABLE_WIDTH,
    Column,
    Palette,
    display_width,
    pad,
    render_table,
    wrap,
)
from dbt_costgate.models import (
    BASIS_LABELS,
    TIB,
    CostDelta,
    PricingDisclosure,
    Report,
    Status,
    format_money,
    format_pct,
)

_DRYRUN_NOTE = "Estimates from BigQuery dry-run — nothing executed, no bytes billed, no SQL shown."

# The one thing that makes a figure here differ from the same bytes on an
# invoice. Stated rather than subtracted: the allowance belongs to the whole
# billing account, which dbt-costgate has no way to see, so deducting it would
# mean guessing. Over-reporting is the safe direction for a gate —
# under-reporting is what lets a regression through.
#
# Scoped to compute on purpose. Every figure in this report is a price for bytes
# scanned, and this names the one adjustment that applies to those bytes and is
# not made. Storage is a different meter with its own rate and its own free
# allowance; dbt-costgate does not price it, but that is the tool's scope rather
# than a caveat on this number, and the footer under a compute figure is not
# where a reader is owed the boundaries of the product.
#
# A footer line and not a `notices.py` entry, either: a notice describes
# configuration that cannot do what it looks like it does, so it is conditional
# and silenceable, while this is true of every priced report — and a notice that
# always fires is noise rather than signal.
_FREE_TIER_NOTE = (
    "Priced from the first byte scanned: BigQuery's 1 TiB/month on-demand free "
    "tier is per billing account, so it is disclosed here and never deducted."
)


def _free_tier_note(d: PricingDisclosure) -> str:
    """The footer sentence, with or without a declared allowance.

    Declaring one changes what the report *says*, never what it subtracts, so the
    sentence has to keep saying that in the same breath as the figure — a reader
    who has configured an allowance is exactly the reader who might assume it was
    applied.
    """
    if d.free_tib_per_month is None:
        return _FREE_TIER_NOTE
    return (
        f"Priced from the first byte scanned. You declared {_tib(d.free_tib_per_month)}/month "
        f"free; the allowance is per billing account, so it is shown against the total above "
        f"and never subtracted from any figure the gate reads."
    )


def _tib(value: float) -> str:
    """A TiB allowance, trailing zeros trimmed: `1 TiB`, `2.5 TiB`."""
    return f"{value:,.2f}".rstrip("0").rstrip(".") + " TiB"


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


def _footer_notes(d: PricingDisclosure) -> list[str]:
    """The footer every renderer ends with, in order.

    Built here rather than in each renderer so terminal and markdown cannot come
    to disagree about which notes apply — the free-tier line is conditional, and
    a condition written twice is a condition that eventually differs.

    The unpriced footer omits it. The free tier is an on-demand allowance that
    does not apply to capacity/Editions pricing at all, and a report quoting no
    money has no figure for it to adjust.
    """
    notes = [_disclosure_line(d)]
    if d.priced:
        notes.append(_free_tier_note(d))
    notes.append(_DRYRUN_NOTE)
    return notes


_BASIS_WARNINGS = frozenset(label.warning for label in BASIS_LABELS.values())


def _row_tag(d: CostDelta) -> str | None:
    """The marker naming what this row's figure actually is.

    From the basis, never from `is_incremental`: the model being incremental does
    not say which shape was dry-run, and tagging a model compiled against its
    existing table `full-refresh` describes a measurement that was not taken. An
    unknown basis yields no tag — unlabelled beats mislabelled.
    """
    label = BASIS_LABELS.get(d.basis) if d.basis is not None else None
    return label.tag if label else None


def _row_warnings(d: CostDelta) -> list[str]:
    """A model's own caveats, minus the one that is really about its tag.

    A basis warning explains what the tag on a row means, which is identical for
    every row carrying that tag. Printed per model it scaled with the number of
    incrementals in the change and pushed the warnings that *are* about one model
    — a dynamic filter, a missing baseline — down a list of repeats.
    """
    return [w for w in d.warnings if w not in _BASIS_WARNINGS]


def _basis_footnotes(report: Report) -> list[str]:
    """One footnote per basis present, in registry order.

    Keyed on the warnings the rows actually carry rather than on their tags, so a
    footnote speaks for exactly the rows the per-row lines used to. The two look
    interchangeable and are not: `sql_warnings` returns nothing at all for a model
    with no compiled SQL, so a row can be tagged while never having carried the
    warning, and a footnote driven off tags would start covering it.

    A report can mix bases — one model compiled fresh, another against its
    existing table — so this is a list. Returning the first would print one
    footnote and leave the other tag unexplained.
    """
    carried = {w for d in report.deltas for w in d.warnings}
    return [label.footnote for label in BASIS_LABELS.values() if label.warning in carried]


def _delta_cell(d: CostDelta, currency: str) -> str:
    if d.error:
        return "not estimated"
    return _money(d.usd_per_run_delta, currency, signed=True)


def _pct_cell(d: CostDelta) -> str:
    if d.error:
        return "not estimated"
    # A ratio to a zero baseline does not exist, so the cell says what happened
    # instead of printing the `—` that reads as "nothing to report here".
    if d.grew_from_zero:
        return "from 0"
    return format_pct(d.pct_delta)


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


def _allowance_line(report: Report) -> str | None:
    """Where this change's monthly scan sits against the allowance the user
    declared, or None when there is nothing to say.

    The assumption travels with the number it is about, rather than living in a
    config file nobody re-reads. Nothing here is subtracted from anything.

    "for these models" is load-bearing. A pull request touches two models of two
    hundred, so this total is not the project's monthly scan and must not be read
    as one — the allowance is account-wide and this figure is not.

    Silent when: no allowance declared (the default, so nothing changes for
    anyone who has not opted in); the run is unpriced, since the tier is an
    on-demand allowance that does not exist under capacity/Editions; or no
    monthly figure could be computed, which `notices.py` explains instead.
    """
    d = report.disclosure
    if d.free_tib_per_month is None or not d.priced:
        return None
    scanned = report.monthly_scan_bytes
    if scanned is None:
        return None
    allowance = _tib(d.free_tib_per_month)
    inside = scanned <= d.free_tib_per_month * TIB
    verb = (
        f"inside the {allowance}/month you declared free, if nothing else has drawn on it"
        if inside
        else f"past the {allowance}/month you declared free"
    )
    return f"Monthly scan for these models: {humanize_bytes(scanned)} — {verb}"


# --- terminal ---------------------------------------------------------------
#
# The terminal report is a table, and everything below exists to keep it one.
# It used to write each model as a sentence — `name (tag): 819.20 GiB → 2.91 TiB
# +264% USD +13.19/run` — which put every figure at a different column on every
# row, ran past 100 characters, and wrapped. Nothing could be compared down a
# column because there were no columns.
#
# Two consequences worth naming, because they look like content changes and are
# not. Per-model warnings moved out from between the rows into a block below the
# table: interleaved prose is what breaks a column scan in the first place, and
# `render_markdown` had already grouped them this way. And `(24 runs)` stopped
# being a parenthetical on the monthly figure and became its own column, which is
# what it always was.

_INDENT = 2
# Comfortably past any name a dbt project actually uses; beyond it, the name is
# the problem rather than the layout.
_MAX_NAME = 44


def _tag_cell(d: CostDelta) -> str:
    flags = []
    if d.is_new:
        flags.append("new")
    if d.is_deleted:
        flags.append("deleted")
    tag = _row_tag(d)
    if tag:
        flags.append(tag)
    return ", ".join(flags)


def _money_cell(value: float | None, currency: str, *, signed: bool) -> str:
    return "—" if value is None else _money(value, currency, signed=signed)


def _runs_cell(d: CostDelta) -> str:
    return str(d.runs_per_month) if d.runs_per_month else "—"


def _month_cost(d: CostDelta) -> float | None:
    """Absolute mode's monthly figure: this run's cost, that many times."""
    if d.usd_current is None or not d.runs_per_month:
        return None
    return d.usd_current * d.runs_per_month


def _terminal_spec(report: Report) -> list[tuple[Column, object]]:
    """Each column paired with the cell it reads off a delta.

    One list rather than four hand-written table shapes, so a column's heading,
    its alignment, when it is given up on a narrow terminal, and where its value
    comes from are all stated in one place and cannot drift apart.

    `drop_rank` says what a narrow terminal sacrifices first, and it is deliberate
    rather than right-to-left: the run-cost column is the one a reader came for,
    so it and the model name are never dropped, while the runs-per-month count and
    the baseline byte figure are context that can go.
    """
    d0 = report.disclosure
    cur = d0.currency
    priced = d0.priced
    monthly = any(d.runs_per_month for d in report.deltas)

    spec: list[tuple[Column, object]] = [
        # Capped rather than unbounded: a name is identified by its ends, and one
        # 120-character outlier must not cost every other column its place.
        (Column("MODEL", align="left", max_width=_MAX_NAME), lambda d: d.name),
        # Unheaded: `new` / `full-refresh` describe the row, and a heading over
        # them would read as another measurement.
        (Column("", align="left"), _tag_cell),
    ]
    if report.mode == "diff":
        spec += [
            (Column("BASELINE", drop_rank=2), lambda d: humanize_bytes(d.bytes_baseline)),
            # Unpriced, the byte figures are the whole report, so `CURRENT` is
            # what `Δ / RUN` is to a priced one and is never given up.
            (
                Column("CURRENT", drop_rank=4 if priced else None),
                lambda d: humanize_bytes(d.bytes_current),
            ),
            (
                Column("Δ %", drop_rank=5 if priced else 3),
                lambda d: "from 0" if d.grew_from_zero else format_pct(d.pct_delta),
            ),
        ]
        if priced:
            spec.append(
                (Column("Δ / RUN"), lambda d: _money_cell(d.usd_per_run_delta, cur, signed=True))
            )
            if monthly:
                spec += [
                    (
                        Column("Δ / MONTH", drop_rank=3),
                        lambda d: _money_cell(d.usd_per_month_delta, cur, signed=True),
                    ),
                    (Column("RUNS", drop_rank=1), _runs_cell),
                ]
    else:
        spec.append((Column("SCANNED"), lambda d: humanize_bytes(d.bytes_current)))
        if priced:
            spec.append(
                (Column("COST / RUN"), lambda d: _money_cell(d.usd_current, cur, signed=False))
            )
            if monthly:
                spec += [
                    (
                        Column("COST / MONTH", drop_rank=2),
                        lambda d: _money_cell(_month_cost(d), cur, signed=False),
                    ),
                    (Column("RUNS", drop_rank=1), _runs_cell),
                ]
    return spec


def _keyed_block(pairs: list[tuple[str, str]], width: int, indent: int = 4) -> list[str]:
    """`key   text` lines with the text aligned into a second column and wrapped
    under itself, so a long note stays inside its own column instead of running
    back under the key."""
    if not pairs:
        return []
    key_width = max(display_width(k) for k, _ in pairs)
    hang = indent + key_width + 2
    lines: list[str] = []
    for key, text in pairs:
        body = wrap(text, width, indent=hang, hanging=hang)
        lines.append(" " * indent + pad(key, key_width) + "  " + body[0].lstrip())
        lines.extend(body[1:])
    return lines


def _model_blocks(report: Report, spec: list[tuple[Column, object]], width: int) -> list[str]:
    """The narrow-terminal fallback: one labelled block per model.

    Below ~60 cells a table costs more than it buys — every figure would be
    truncated, which is worse than not lining them up. The same cells are printed,
    one per line, so nothing is lost and nothing is cropped.
    """
    lines: list[str] = []
    for d in report.deltas:
        tag = _tag_cell(d)
        lines.append(" " * _INDENT + d.name + (f"  ({tag})" if tag else ""))
        # Headings verbatim, not lower-cased: `Δ` has a lower-case form and it is
        # `δ`, which means something else entirely.
        lines.extend(
            _keyed_block(
                [(column.header, cell(d)) for column, cell in spec[1:] if column.header],
                width,
                indent=_INDENT + 2,
            )
        )
    return lines


def _model_notes(report: Report) -> list[tuple[str, str]]:
    """Per-model caveats, keyed by the model they are about.

    The marker rides in the key column, where it stays aligned: `⚠` is a caveat
    about a figure that exists, `•` is the absence of one, and collapsing the two
    into undifferentiated prose would lose a distinction markdown keeps.
    """
    pairs = [(f"⚠ {d.name}", w) for d in report.deltas for w in _row_warnings(d)]
    pairs += [(f"• {d.name}", d.error) for d in report.deltas if d.error]
    return pairs


def render_terminal(report: Report, *, width: int | None = None, color: bool = False) -> str:
    width = width or DEFAULT_WIDTH
    p = Palette(color)
    d0 = report.disclosure
    lines: list[str] = []

    regions = ", ".join(d0.regions) or "—"
    rate = next(iter(d0.regions.values()), None)
    tail = ""
    if not d0.priced:
        tail = " · bytes only (no per-byte price configured)"
    elif rate is not None:
        tail = f" · on-demand {_rate(d0.currency, rate)} · {d0.source}"
        if d0.free_tib_per_month is not None:
            # In the pricing line, where a reader already looks to find out what
            # they are being charged — not buried in the footer. Same dim, same
            # `·` rhythm, so it reads as one more fact about the rate rather than
            # as an announcement.
            tail += f" · first {_tib(d0.free_tib_per_month)}/month free"
    # Wrapped like everything else: at 60 columns the region, the rate and the
    # rate's provenance do not fit on one line, and a header that overflows is
    # the same defect this rewrite exists to remove.
    head = wrap(f"dbt-costgate — region: {regions}{tail}", width, hanging=2)
    lines.append(p.bold("dbt-costgate") + p.dim(head[0][len("dbt-costgate") :]))
    lines.extend(p.dim(line) for line in head[1:])
    lines.append("")

    if not report.deltas:
        lines.append(" " * _INDENT + "No changed models to estimate.")
        lines.append("")
        lines.extend(p.dim(line) for line in wrap(_DRYRUN_NOTE, width, indent=_INDENT))
        return "\n".join(lines)

    spec = _terminal_spec(report)
    if width < MIN_TABLE_WIDTH:
        lines.extend(_model_blocks(report, spec, width))
    else:
        rows = [[cell(d) for _, cell in spec] for d in report.deltas]
        table, dropped = render_table(
            [column for column, _ in spec], rows, width=width, indent=_INDENT
        )
        head, rule, *body = table
        lines.extend([p.dim(head), p.dim(rule), *body])
        if dropped:
            # Never let a narrow window quietly remove a figure: a column that
            # vanished without a word reads as a column that was never there.
            lines.extend(
                p.dim(line)
                for line in wrap(
                    f"{', '.join(dropped)} hidden — widen the terminal, or use --format json",
                    width,
                    indent=_INDENT,
                )
            )

    net = _net_line(report)
    allowance = _allowance_line(report)
    if net or allowance:
        lines.append("")
    if net:
        lines.append(p.bold(" " * _INDENT + net))
    if allowance:
        # Dim, and under the net figure rather than beside it: it is context for
        # that number, not another measurement competing with it.
        lines.extend(
            p.dim(line) for line in wrap(allowance, width, indent=_INDENT, hanging=_INDENT + 2)
        )

    lines.append("")
    v = report.verdict
    if v.status == Status.PASS:
        lines.append(p.green(p.bold(" " * _INDENT + "GATE: PASS")))
    else:
        label = "FAIL" if v.status == Status.FAIL else "WARN"
        paint = p.red if v.status == Status.FAIL else p.yellow
        lines.append(paint(p.bold(" " * _INDENT + f"GATE: {label}")))
        for b in v.breaches:
            lines.extend(wrap(f"- {b}", width, indent=_INDENT + 2, hanging=_INDENT + 4))

    model_notes = _model_notes(report)
    footnotes = _basis_footnotes(report)
    if model_notes or footnotes:
        lines.append("")
        lines.append(p.dim(" " * _INDENT + "NOTES"))
        lines.extend(_keyed_block(model_notes, width, indent=_INDENT + 2))
        # Unkeyed, and last: a footnote is about a tag several rows share rather
        # than about any one model, and it already opens by naming that tag — so
        # a key column beside it would only print the tag twice. Verbatim, so the
        # sentence a reader sees here is the one they see in the pull request.
        for note in footnotes:
            lines.extend(wrap(f"⚠ {note}", width, indent=_INDENT + 2, hanging=_INDENT + 4))

    lines.append("")
    # Notices sit with the disclosure, not with the gate: both describe how this
    # run was configured rather than what the change did. The id leads — it is
    # what a user puts in `notices.silence`, so it has to be visible without
    # going and looking it up.
    if report.notices:
        lines.extend(
            _keyed_block([(f"⚠ {n.id}", n.message) for n in report.notices], width, indent=_INDENT)
        )
        lines.append("")
    # No heading over the footer: the first note already opens with `Pricing:`,
    # and a `PRICING` label above it just said the word twice. Dimmed and set
    # apart by the blank line, which is enough to read as a footer.
    for note in _footer_notes(d0):
        lines.extend(p.dim(line) for line in wrap(note, width, indent=_INDENT, hanging=_INDENT + 2))
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

    caveats = [(d.name, w) for d in report.deltas for w in _row_warnings(d)]
    errors = [(d.name, d.error) for d in report.deltas if d.error]
    footnotes = _basis_footnotes(report)
    if caveats or errors or footnotes:
        out.append("")
        for name, w in caveats:
            out.append(f"> ⚠ **{name}** — {w}")
        for name, e in errors:
            out.append(f"> • **{name}** — {e}")
        # Last: they annotate the table's tags rather than any one model, so they
        # read as the footnotes they are instead of more per-model caveats.
        out.extend(f"> ⚠ {note}" for note in footnotes)

    net = _net_line(report)
    allowance = _allowance_line(report)
    if net or allowance:
        out.append("")
    if net:
        label, _, rest = net.partition(": ")
        out.append(f"**{label}:** {rest}")
    if allowance:
        # `<sub>` rather than bold: same placement as the terminal, same job —
        # context under the figure, quieter than the figure.
        out.append(f"<sub>{allowance}</sub>")

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

    if report.notices:
        out.append("")
        for n in report.notices:
            out.append(f"> ⚠ **{n.id}** — {n.message}")

    out.append("")
    out.append(f"<sub>{'<br/>'.join(_footer_notes(d0))}</sub>")
    return "\n".join(out)


def _md_name(d: CostDelta) -> str:
    tags = []
    if d.is_new:
        tags.append("new")
    if d.is_deleted:
        tags.append("deleted")
    tag = _row_tag(d)
    if tag:
        tags.append(tag)
    suffix = f" _{', '.join(tags)}_" if tags else ""
    return f"`{d.name}`{suffix}"


def render_json(report: Report) -> str:
    d0 = report.disclosure
    diff = report.mode == "diff"

    def delta(value):
        """Absolute mode has no baseline, so it has no delta from one. The
        run-level `net` block already nulls these; the per-model fields were
        populated anyway, which is a number with no question behind it."""
        return value if diff else None

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
            # What the user declared free, in TiB/month, or null. Reported, never
            # applied: no other figure in this payload is adjusted by it.
            "free_tib_per_month": d0.free_tib_per_month,
        },
        # Advisory notes about the configuration. Never affects `verdict`. `id` is
        # the stable key — it is what `notices.silence` accepts, so a consumer can
        # match on it without parsing prose.
        "notices": [{"id": n.id, "message": n.message} for n in report.notices],
        # Run-level figures. The three signed ones are deltas — negative is a
        # saving — and are null in absolute mode, where there is no baseline to
        # net against. The rest are absolute and survive it: the two counts, and
        # `monthly_scan_bytes`, which is what these models are projected to scan
        # in a month (null unless every estimated model has a run frequency).
        "net": {
            "bytes": delta(report.net_bytes),
            "usd_per_run": delta(report.net_usd_per_run),
            "usd_per_month": delta(report.net_usd_per_month),
            "monthly_scan_bytes": report.monthly_scan_bytes,
            "models_estimated": len(report.estimated),
            "models_total": len(report.deltas),
        },
        "models": [
            {
                "name": d.name,
                "unique_id": d.unique_id,
                "is_incremental": d.is_incremental,
                "is_deleted": d.is_deleted,
                # Which shape was dry-run. `is_incremental` is a fact about the
                # model; this is a fact about the number beside it, and only this
                # one says whether that number is a rebuild or a single run.
                "basis": d.basis.value if d.basis else None,
                "is_new": d.is_new,
                "gateable": d.gateable,
                # Why not, when not. `gateable` alone cannot tell a consumer
                # whether the user excluded this model or the gate could not
                # measure it, and those mean opposite things to a dashboard.
                "skip_reason": d.skip_reason.value if d.skip_reason else None,
                "region": d.region,
                "bytes_baseline": d.bytes_baseline,
                "bytes_current": d.bytes_current,
                "usd_baseline": d.usd_baseline,
                "usd_current": d.usd_current,
                "usd_per_run_delta": delta(d.usd_per_run_delta),
                "usd_per_month_delta": delta(d.usd_per_month_delta),
                "pct_delta": delta(d.pct_delta),
                "runs_per_month": d.runs_per_month,
                "warnings": d.warnings,
                "error": d.error,
            }
            for d in report.deltas
        ],
    }
    return json.dumps(payload, indent=2)


def render(report: Report, fmt: str, *, width: int | None = None, color: bool = False) -> str:
    """`width` and `color` are terminal-only; markdown and JSON are the same bytes
    wherever they are rendered, which is what makes them safe to diff and to post."""
    if fmt == "markdown":
        return render_markdown(report)
    if fmt == "json":
        return render_json(report)
    return render_terminal(report, width=width, color=color)

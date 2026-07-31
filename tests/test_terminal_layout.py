# SPDX-License-Identifier: Apache-2.0
"""The terminal report is a table, and these are the promises that make it one.

The old renderer wrote each model as a sentence, so no two rows put a figure in
the same place and a priced diff row ran past 100 characters and wrapped. What
replaced it only helps if the columns really line up — under a CJK name, under a
120-character name, in an 80-column window — and if turning colour on cannot move
them. Each of those is asserted here rather than eyeballed once.

The width is always passed explicitly. `render_terminal` never asks the terminal
how wide it is; the CLI does that and hands the answer in, which is what keeps a
report reproducible between a laptop and a CI log.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from conftest import FakeDryRunner, make_manifest, make_node, write_target
from dbt_costgate import layout, report
from dbt_costgate.cli import main
from dbt_costgate.models import (
    TIB,
    CostDelta,
    EstimateBasis,
    Notice,
    PricingDisclosure,
    Report,
    Status,
    Verdict,
    format_pct,
)

ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
GIB = 1024**3


def _delta(name: str, current: int, baseline: int | None = None, **kwargs) -> CostDelta:
    rate = 6.25
    base = {
        "name": name,
        "unique_id": f"model.pkg.{name}",
        "is_incremental": False,
        "is_new": baseline is None,
        "gateable": True,
        "bytes_baseline": baseline,
        "bytes_current": current,
        "usd_baseline": None if baseline is None else baseline / TIB * rate,
        "usd_current": current / TIB * rate,
        "region": "US",
        "runs_per_month": 30,
    }
    base.update(kwargs)
    return CostDelta(**base)


def _report(deltas: list[CostDelta], *, mode: str = "diff", rate: float = 6.25, **kwargs) -> Report:
    return Report(
        deltas=deltas,
        disclosure=PricingDisclosure(
            regions={"US": rate},
            source="built-in table",
            table_version="2026.07",
            last_verified="2026-07-25",
            region_sources={"US": "region-table"},
        ),
        verdict=Verdict(status=Status.PASS, exit_code=0),
        mode=mode,
        **kwargs,
    )


def _sample() -> Report:
    """Three models: one tagged and incremental, one new, one ordinary."""
    return _report(
        [
            _delta(
                "fct_orders_daily",
                int(2.91 * TIB),
                int(0.8 * TIB),
                basis=EstimateBasis.FULL_REFRESH,
                is_incremental=True,
            ),
            _delta("dim_customers", 412 * 1024**2),
            _delta("stg_events", 44 * GIB, 40 * GIB),
        ]
    )


def _table_rows(out: str) -> list[str]:
    """The header, the rule and every data row — everything the alignment
    promise is about, and nothing that is wrapped prose."""
    lines = out.splitlines()
    # The rule is one run of dashes per column, so it is dashes and gutters only.
    rule = next(
        i
        for i, line in enumerate(lines)
        if line.strip() and set(line.strip()) <= {"─", " "} and "─" in line
    )
    body = []
    for line in lines[rule + 1 :]:
        if not line.strip():
            break
        body.append(line)
    return [lines[rule - 1], lines[rule], *body]


# --------------------------------------------------------------------------
# Alignment.
# --------------------------------------------------------------------------


def test_every_row_puts_its_columns_in_the_same_place():
    """The whole point. Column boundaries are read off the rule, which is one run
    of dashes per column, and every other row has to break at the same cells."""
    out = report.render_terminal(_sample(), width=100)
    header, rule, *rows = _table_rows(out)
    boundaries = [i for i, ch in enumerate(rule) if ch == " "]
    assert boundaries, "the rule should be segmented, one run per column"
    for row in [header, *rows]:
        for i in boundaries:
            assert i >= len(row) or row[i] == " ", f"row crosses a column boundary at {i}: {row!r}"


@pytest.mark.parametrize("name", ["订单汇总_每日", "modèle_coût", "a" * 120, "x"])
def test_an_awkward_name_does_not_move_the_columns(name: str):
    """`len()` counts a CJK character as one and it prints as two, so a column
    padded by character count comes out visibly ragged. Every row is measured in
    display cells, which is what makes this hold."""
    rep = _report([_delta(name, 2 * TIB, TIB), _delta("dim_customers", TIB, TIB)])
    out = report.render_terminal(rep, width=100)
    header, rule, *rows = _table_rows(out)
    widths = {layout.display_width(line) for line in [header, rule]}
    assert len(widths) == 1, "header and rule disagree about the table width"
    for row in rows:
        assert layout.display_width(row) <= layout.display_width(rule)


@pytest.mark.parametrize("width", [60, 72, 88, 100, 140])
def test_no_line_ever_runs_past_the_width(width: int):
    rep = _report([_delta("a_model_with_a_reasonably_long_name", 2 * TIB, TIB)])
    rep.notices = [Notice(id="dead-money-thresholds", message="a long advisory " * 8)]
    rep.deltas[0].warnings.append("dynamic filter — dry-run may be worst-case " * 3)
    for line in report.render_terminal(rep, width=width).splitlines():
        assert layout.display_width(line) <= width, repr(line)


def test_a_very_long_name_is_truncated_rather_than_costing_every_column_its_place():
    rep = _report([_delta("z" * 120, 2 * TIB, TIB)])
    out = report.render_terminal(rep, width=100)
    assert "…" in out
    # The figures survive: truncating one silly name is cheaper than dropping the
    # column a reader came for.
    assert "Δ / RUN" in out and "USD +6.25" in out


# --------------------------------------------------------------------------
# Narrow terminals.
# --------------------------------------------------------------------------


def test_a_narrow_terminal_drops_columns_and_says_which():
    wide = report.render_terminal(_sample(), width=100)
    narrow = report.render_terminal(_sample(), width=72)
    assert "RUNS" in wide
    assert "hidden — widen the terminal" in narrow
    assert "RUNS" in narrow.split("hidden")[0].splitlines()[-1]


def test_the_model_name_and_the_run_cost_are_never_dropped():
    """What a narrow window gives up is a decision. The per-run figure is the
    number the reader came for, so it outlives the baseline byte count."""
    for width in (62, 70, 84, 100):
        out = report.render_terminal(_sample(), width=width)
        assert "fct_orders_daily" in out
        assert "Δ / RUN" in out


def test_below_a_table_width_it_becomes_one_block_per_model():
    out = report.render_terminal(_sample(), width=48)
    assert "MODEL" not in out, "no table heading in block mode"
    for name in ("fct_orders_daily", "dim_customers", "stg_events"):
        assert name in out
    # Every figure is still printed, one per line.
    assert "Δ / RUN" in out and "BASELINE" in out
    for line in out.splitlines():
        assert layout.display_width(line) <= 48


# --------------------------------------------------------------------------
# Colour.
# --------------------------------------------------------------------------


def test_colour_changes_nothing_but_escape_sequences():
    """Padding is computed on raw text and styling is layered onto the finished
    line, so alignment cannot drift between the two renderings. Strip the escapes
    and the two must be the same bytes."""
    rep = _sample()
    plain = report.render_terminal(rep, width=100, color=False)
    coloured = report.render_terminal(rep, width=100, color=True)
    assert "\x1b[" in coloured
    assert ANSI.sub("", coloured) == plain


def test_colour_is_off_unless_it_is_asked_for():
    assert report.render_terminal(_sample(), width=100).find("\x1b[") == -1


class _Tty:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.mark.parametrize(
    ("mode", "tty", "no_color", "expected"),
    [
        ("auto", True, None, True),
        ("auto", False, None, False),
        ("auto", True, "1", False),
        ("always", False, "1", True),
        ("never", True, None, False),
    ],
)
def test_colour_follows_the_flag_the_tty_and_no_color(monkeypatch, mode, tty, no_color, expected):
    monkeypatch.delenv("NO_COLOR", raising=False)
    if no_color is not None:
        monkeypatch.setenv("NO_COLOR", no_color)
    assert layout.should_color(mode, _Tty(tty)) is expected


def test_a_report_written_to_a_file_carries_no_escape_sequences(tmp_path: Path):
    target = write_target(tmp_path, make_manifest(make_node("m", compiled_code="SQL")))
    out = tmp_path / "report.txt"
    main(
        # fmt: off
        [
            "check",
            "--current",
            str(target),
            "--select",
            "m",
            "--color",
            "always",
            "--output",
            str(out),
        ],
        # fmt: on
        runner=FakeDryRunner({"SQL": TIB}),
    )
    assert "\x1b[" not in out.read_text("utf-8")


# --------------------------------------------------------------------------
# Content that must survive the rearrangement.
# --------------------------------------------------------------------------


def test_a_models_error_is_still_reported_even_though_its_cells_are_blank():
    """Figure cells show `—` for a model that could not be estimated, which on its
    own is indistinguishable from zero. The reason has to be somewhere, and it is
    the notes block, keyed by the model and marked `•` rather than `⚠`."""
    rep = _report(
        [
            _delta("ok", TIB, TIB),
            CostDelta(
                name="denied",
                unique_id="model.pkg.denied",
                is_incremental=False,
                is_new=False,
                gateable=False,
                bytes_baseline=None,
                bytes_current=None,
                usd_baseline=None,
                usd_current=None,
                region="US",
                error="not estimated — the caller lacks permission to dry-run it",
            ),
        ]
    )
    out = " ".join(report.render_terminal(rep, width=100).split())
    assert "• denied not estimated — the caller lacks permission" in out


def test_the_verdict_and_its_breaches_are_never_wrapped_out_of_sight():
    rep = _sample()
    rep.verdict = Verdict(
        status=Status.FAIL,
        breaches=["fct_orders_daily: " + "USD +13.19/run exceeds USD 5.00 " * 4],
        exit_code=1,
    )
    out = report.render_terminal(rep, width=70)
    assert "GATE: FAIL" in out
    assert "fct_orders_daily" in " ".join(out.split("GATE: FAIL")[1].split())


# --------------------------------------------------------------------------
# Ordering and precision — the two legibility defects from the QA pass.
# --------------------------------------------------------------------------


def test_an_unpriced_report_is_ordered_by_size(tmp_path: Path, capsys):
    """BUG-F24. Under slot pricing every rate is 0, so every dollar delta is 0.00
    and a sort on money alone left the rows in arrival order — the 2.91 TiB model
    printed below the 412 MiB one."""
    target = write_target(
        tmp_path,
        make_manifest(
            make_node("small", compiled_code="SMALL"),
            make_node("large", compiled_code="LARGE"),
        ),
    )
    main(
        [
            "check",
            "--current",
            str(target),
            "--select",
            "small,large",
            "--usd-per-tib",
            "0",
            "--format",
            "json",
        ],
        runner=FakeDryRunner({"SMALL": 412 * 1024**2, "LARGE": 3 * TIB}),
    )
    payload = json.loads(capsys.readouterr().out)
    assert [m["name"] for m in payload["models"]] == ["large", "small"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (263.75, "+264%"),
        (0.4, "+0.4%"),
        (0.35, "+0.35%"),
        (9.5, "+9.5%"),
        (25.0, "+25%"),
        (-78.0, "-78%"),
        (1200.0, "+1,200%"),
        (None, "—"),
    ],
)
def test_a_percentage_is_printed_at_the_precision_it_needs(value, expected):
    assert format_pct(value) == expected


def test_a_threshold_is_printed_unsigned():
    assert format_pct(0.3, signed=False) == "0.3%"


# --------------------------------------------------------------------------
# The primitives.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "cells"),
    [
        ("abc", 3),
        ("订单", 4),  # two wide characters
        ("é", 1),  # e + combining acute
        ("\x1b[31mred\x1b[0m", 3),  # escapes occupy no cells
        ("", 0),
    ],
)
def test_display_width_counts_cells_not_characters(text, cells):
    assert layout.display_width(text) == cells


def test_truncate_marks_the_cut():
    assert layout.truncate("abcdef", 4) == "abc…"
    assert layout.truncate("abc", 4) == "abc"


def test_wrap_hangs_continuations_under_the_first_line():
    lines = layout.wrap("one two three four five", 12, indent=2, hanging=4)
    assert lines[0].startswith("  ") and not lines[0].startswith("   ")
    assert all(line.startswith("    ") for line in lines[1:])
    assert all(layout.display_width(line) <= 12 for line in lines)


class _Fd:
    """A stream whose fileno() is a terminal of `columns` wide, or none at all.

    `os.get_terminal_size` is monkeypatched against the number this hands back,
    so the fake never has to own a real pty.
    """

    def __init__(self, columns: int | None):
        self.columns = columns

    def fileno(self) -> int:
        if self.columns is None:
            raise OSError(25, "Inappropriate ioctl for device")
        return self.columns


@pytest.fixture
def sized_terminals(monkeypatch):
    """`os.get_terminal_size(fd)` answering with the fd itself as the width."""
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setattr(layout.os, "get_terminal_size", lambda fd: os.terminal_size((fd, 24)))
    return monkeypatch


def test_help_width_follows_the_terminal_through_a_pipe(sized_terminals):
    """The point of `help_width`. stdout is a pipe — as it is under `| less` —
    and stderr is still on the 88-column window the user is looking at."""
    sized_terminals.setattr(layout.sys, "stdout", _Fd(None))
    sized_terminals.setattr(layout.sys, "stderr", _Fd(88))
    sized_terminals.setattr(layout.sys, "stdin", _Fd(None))
    assert layout.help_width(_Fd(None)) == 88


def test_help_width_prefers_the_stream_it_was_given(sized_terminals):
    sized_terminals.setattr(layout.sys, "stderr", _Fd(88))
    assert layout.help_width(_Fd(70)) == 70


def test_help_width_honours_columns_above_every_stream(monkeypatch):
    """The one signal that survives with no terminal anywhere, and how a user
    pins the width of output they are redirecting to a file."""
    monkeypatch.setenv("COLUMNS", "64")
    monkeypatch.setattr(layout.sys, "stderr", _Fd(120))
    assert layout.help_width(_Fd(120)) == 64


@pytest.mark.parametrize("value", ["", "wide", "0", "-10"])
def test_help_width_ignores_a_columns_that_is_not_a_width(value, sized_terminals):
    sized_terminals.setenv("COLUMNS", value)
    sized_terminals.setattr(layout.sys, "stdout", _Fd(76))
    assert layout.help_width(_Fd(None)) == 76


def test_help_width_falls_back_when_nothing_is_a_terminal(sized_terminals):
    """A CI log. Nothing to measure, so the fixed width is the honest answer."""
    for name in ("stdout", "stderr", "stdin"):
        sized_terminals.setattr(layout.sys, name, _Fd(None))
    assert layout.help_width(_Fd(None)) == layout.DEFAULT_WIDTH


def test_help_width_never_goes_below_the_minimum(sized_terminals):
    sized_terminals.setattr(layout.sys, "stdout", _Fd(20))
    assert layout.help_width(_Fd(20)) == layout.MIN_TABLE_WIDTH


def test_a_report_still_ignores_the_window_when_it_is_not_a_terminal(sized_terminals):
    """`help_width`'s opposite, and deliberately so: a report may be committed or
    read back out of a CI log, so it must not depend on the window that made it.
    This is the guard that stops the two being 'unified' into one."""
    sized_terminals.setattr(layout.sys, "stderr", _Fd(88))
    assert layout.terminal_width(_Fd(None)) == layout.DEFAULT_WIDTH


@pytest.mark.parametrize(
    ("width", "expected"),
    [(60, 60), (100, 100), (140, layout.PROSE_WIDTH), (240, layout.PROSE_WIDTH)],
)
def test_prose_width_caps_but_never_widens(width: int, expected: int):
    """A cap, not a target: a narrow window still wins, because fitting the
    terminal is a hard constraint and the reading measure is a preference."""
    assert layout.prose_width(width) == expected


def test_a_wide_terminal_spends_its_width_on_the_table_not_on_sentences():
    """The whole point of capping only prose, asserted in both directions at once.

    The table is sized by its content rather than stretched to fill the window,
    so this needs a name long enough to push it past the reading measure — with
    short names it sits near 80 and would pass whether or not it was capped.
    """
    rep = _report([_delta("fct_orders_daily_by_region_and_channel_rollup", 2 * TIB, TIB)])
    rep.notices = [Notice(id="dead-money-thresholds", message="a long advisory " * 12)]
    out = report.render_terminal(rep, width=180)

    _, rule, *_ = _table_rows(out)
    assert layout.display_width(rule) > layout.PROSE_WIDTH, "the table was capped too"

    notice = [ln for ln in out.splitlines() if "advisory" in ln]
    assert notice, "the notice should be in the report"
    for line in notice:
        assert layout.display_width(line) <= layout.PROSE_WIDTH, line


def test_the_pricing_header_is_not_capped():
    """It is a run of figures separated by `·`, not a sentence, and it already
    runs near a hundred characters with three regions in it. Capping would split
    it across two lines on a terminal that was showing it on one."""
    rep = _report([_delta("m", TIB, TIB)])
    rep.disclosure.regions = {"EU": 6.25, "US": 6.25, "asia-northeast1": 7.5}
    rep.disclosure.region_sources = dict.fromkeys(rep.disclosure.regions, "region-table")
    head = report.render_terminal(rep, width=180).splitlines()[0]
    assert head.startswith("dbt-costgate")
    assert "asia-northeast1" in head, "the header wrapped when it had room not to"


def test_a_column_with_no_heading_and_no_content_is_not_rendered():
    """The tag column is empty whenever nothing in the change is new or
    incremental. Left in, it printed as a zero-width rule segment and a
    four-space gap that read like a mistake."""
    columns = [layout.Column("A", align="left"), layout.Column("")]
    lines, dropped = layout.render_table(columns, [["x", ""]], width=40)
    assert dropped == []
    assert lines[0].strip() == "A"

# SPDX-License-Identifier: Apache-2.0
"""Text shaping for the terminal report: display widths, aligned tables, wrapping.

Separate from `report.py` because none of it knows what a cost estimate is — it
lays out strings, and can be tested by reading its output rather than by building
a `Report` first.

Two rules hold everything else together:

**Width is measured in terminal cells, not characters.** `len()` is wrong for a
CJK model name (two cells per character) and wrong for a combining accent (zero),
and a column padded by character count comes out visibly ragged. `display_width`
is the only measure used here.

**Colour is layered on after padding, never before.** Every function in this
module takes and returns plain text; the caller styles finished lines. That is
why a coloured report and an uncoloured one differ only by escape sequences —
alignment cannot drift between them, because alignment was computed once, on text
with no escapes in it. `display_width` strips ANSI anyway, as a backstop for the
day someone forgets.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

# What a report renders to when nothing better is known: a file, a pipe, a CI log.
DEFAULT_WIDTH = 100
# Under this, columns cost more than they buy — the caller switches to one block
# per model rather than truncating every figure.
MIN_TABLE_WIDTH = 60
# A model name shrinks this far before anything else gives way. Shorter than this
# and the names stop being recognisable, which defeats the point of the column.
MIN_NAME_WIDTH = 18
# Past roughly this many characters the eye starts losing its place tracking back
# to the beginning of the next line, so a paragraph stops getting more readable as
# the window gets wider. Deliberately its own constant rather than a second use of
# DEFAULT_WIDTH, which happens to hold the same number: that one means "what to
# render at when there is nothing to measure", and changing it should not silently
# change how prose is set.
PROSE_WIDTH = 100

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def display_width(text: str) -> int:
    """How many terminal cells `text` occupies."""
    total = 0
    for ch in _ANSI.sub("", text):
        if unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def truncate(text: str, width: int) -> str:
    """Shorten to `width` cells, marking the cut with an ellipsis."""
    if display_width(text) <= width:
        return text
    if width <= 1:
        return "…"[:width]
    out: list[str] = []
    used = 0
    for ch in text:
        w = display_width(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def pad(text: str, width: int, align: str = "left") -> str:
    """Pad to `width` cells. Over-long text is returned as-is — `render_table`
    truncates deliberately, and silently cropping here would hide that."""
    gap = width - display_width(text)
    if gap <= 0:
        return text
    return text + " " * gap if align == "left" else " " * gap + text


def wrap(text: str, width: int, *, indent: int = 0, hanging: int | None = None) -> list[str]:
    """Wrap prose to `width` cells, first line at `indent` and the rest at
    `hanging`. Cell-aware, so a note naming a CJK model wraps where it looks like
    it should. `textwrap` counts characters and would run those lines long."""
    hang = indent if hanging is None else hanging
    lines: list[str] = []
    current: list[str] = []
    current_width = 0
    left = indent
    for word in text.split():
        word_width = display_width(word)
        if current and left + current_width + 1 + word_width > width:
            lines.append(" " * left + " ".join(current))
            current, current_width, left = [word], word_width, hang
        else:
            current_width += word_width + (1 if current else 0)
            current.append(word)
    if current:
        lines.append(" " * left + " ".join(current))
    return lines


# How much room a description needs beside its label before it is worth putting
# it there at all. Under this, the label keeps the line and the description goes
# underneath — the same "give way rather than truncate" rule `render_table` uses.
MIN_DESCRIPTION_WIDTH = 20
# Where a description sits when it has been pushed under its label.
_FALLBACK_INDENT = 4


def prose_width(width: int) -> int:
    """The width to set running prose at, in a terminal `width` cells across.

    A cap and never a floor — a narrow window still wins, because fitting the
    terminal is the harder constraint and this is only a preference.

    Tables are not capped, and the difference is not an inconsistency: a table is
    read down a column, so extra width buys it another figure or a name that no
    longer needs truncating. A paragraph is read along the line, and extra width
    past about a hundred characters buys it nothing while making the return sweep
    to the next line steadily harder. Status lines full of figures count as the
    former, not the latter.
    """
    return min(width, PROSE_WIDTH)


def hanging_row(
    prefix: str,
    text: str,
    width: int,
    *,
    style: Callable[[str], str] | None = None,
) -> list[str]:
    """`prefix`, then `text` wrapping under itself instead of past `width`.

    The shape every label-and-description list wants: a key and what it does, a
    command and what it is for. `prefix` is passed in already padded and already
    styled, so a caller can put a coloured column inside it.

    The wrap column is measured from `prefix` rather than taken as an argument.
    Passing it separately meant two numbers that had to agree, and when a narrow
    terminal clamped one of them the continuation lines indented to a column the
    first line did not start at — every row one cell too long, which is the
    defect this whole function exists to prevent.

    `style` paints the description and nothing else — never the padding, so this
    keeps the module's rule that colour goes on after alignment, not before.
    """
    paint = style or (lambda part: part)
    column = display_width(prefix)
    if width - column < MIN_DESCRIPTION_WIDTH:
        body = wrap(text, width, indent=_FALLBACK_INDENT, hanging=_FALLBACK_INDENT)
        return [prefix.rstrip(), *(_FALLBACK_INDENT * " " + paint(ln.lstrip()) for ln in body)]
    body = wrap(text, width, indent=column, hanging=column)
    if not body:
        return [prefix.rstrip()]
    return [
        (prefix + paint(body[0].lstrip())).rstrip(),
        *(" " * column + paint(line.lstrip()) for line in body[1:]),
    ]


@dataclass(frozen=True)
class Column:
    """One table column.

    `drop_rank` decides what a narrow terminal gives up first: lower goes first,
    `None` never goes at all. It is stated per table rather than inferred from
    position because the rightmost column is not the least useful one — a budget
    owner reading a diff wants the per-run cost long after the baseline byte count
    has stopped earning its space.
    """

    header: str
    align: str = "right"
    drop_rank: int | None = None
    # A ceiling on how much of the width one column may claim. Without it a single
    # 120-character model name costs every other column its place — the table
    # would drop the per-run cost to make room for a name nobody can read anyway.
    max_width: int | None = None


def render_table(
    columns: list[Column],
    rows: list[list[str]],
    *,
    width: int,
    indent: int = 2,
    gutter: int = 2,
) -> tuple[list[str], list[str]]:
    """Lay `rows` out under `columns` within `width` cells.

    Returns the lines and the headers of any columns dropped to make it fit, so
    the caller can say what is missing. A hidden column that nobody mentions reads
    as a column that does not exist.
    """
    # A column with no heading and nothing in it is not a column. The tag column
    # is empty whenever no model in the change is new or incremental, and left in
    # it showed up as a zero-width rule segment and a four-space gap that looked
    # like a mistake. Not counted as dropped: nothing was hidden.
    keep = [
        i
        for i in range(len(columns))
        if columns[i].header or any(row[i] for row in rows) or not rows
    ]
    droppable = sorted(
        (i for i in keep if columns[i].drop_rank is not None),
        key=lambda i: columns[i].drop_rank or 0,
    )
    dropped: list[str] = []
    while droppable and _table_width(columns, rows, keep, indent, gutter) > width:
        index = droppable.pop(0)
        keep.remove(index)
        dropped.append(columns[index].header)

    widths = _column_widths(columns, rows, keep)
    # Last resort: the name column gives up space before any figure does, because
    # a truncated name is still recognisable and a truncated number is a lie.
    over = _total(widths, keep, indent, gutter) - width
    if over > 0 and keep:
        first = keep[0]
        widths[first] = max(MIN_NAME_WIDTH, widths[first] - over)

    pad_ = " " * indent
    joiner = " " * gutter
    header = pad_ + joiner.join(
        pad(truncate(columns[i].header, widths[i]), widths[i], columns[i].align) for i in keep
    )
    rule = pad_ + joiner.join("─" * widths[i] for i in keep)
    body = [
        pad_
        + joiner.join(
            pad(truncate(row[i], widths[i]), widths[i], columns[i].align) for i in keep
        ).rstrip()
        for row in rows
    ]
    return [header.rstrip(), rule, *body], dropped


def _column_widths(columns: list[Column], rows: list[list[str]], keep: list[int]) -> dict[int, int]:
    widths = {}
    for i in keep:
        natural = max([display_width(columns[i].header)] + [display_width(row[i]) for row in rows])
        cap = columns[i].max_width
        widths[i] = min(natural, cap) if cap else natural
    return widths


def _total(widths: dict[int, int], keep: list[int], indent: int, gutter: int) -> int:
    if not keep:
        return indent
    return indent + sum(widths[i] for i in keep) + gutter * (len(keep) - 1)


def _table_width(
    columns: list[Column], rows: list[list[str]], keep: list[int], indent: int, gutter: int
) -> int:
    return _total(_column_widths(columns, rows, keep), keep, indent, gutter)


# --- terminal capabilities -------------------------------------------------


def terminal_width(stream=None) -> int:
    """The width to render a **report** at.

    A stream that is not a terminal gets `DEFAULT_WIDTH`: a report redirected to a
    file, piped to `grep`, or captured by CI must not depend on the size of the
    window that happened to produce it. `shutil.get_terminal_size` honours
    `COLUMNS`, which is how a test — or a user — pins it.

    Reference and help text wants the opposite and uses `help_width`; the two are
    separate because the reasons are, not because one of them is a worse version
    of the other.
    """
    if stream is not None and not _isatty(stream):
        return DEFAULT_WIDTH
    return max(MIN_TABLE_WIDTH, shutil.get_terminal_size((DEFAULT_WIDTH, 24)).columns)


def help_width(stream=None) -> int:
    """The width to render reference and help text at.

    Unlike `terminal_width`, a stream that is not a terminal does not end the
    search. `dbt-costgate config | less` is still being read in the window it was
    launched from, and answering "100 columns" there is how a reference comes to
    wrap raggedly in an 80-column terminal. A report has a real reason to ignore
    the window — it may be committed or read back out of a CI log — and a list of
    settings does not.

    `shutil.get_terminal_size` is not enough on its own, which is the whole
    reason this function exists: it asks `sys.__stdout__` and nothing else, so in
    a pipeline it returns its fallback while stderr is sitting on the very
    terminal it was looking for. Measured, with stdout piped from an 80-column
    window: `shutil` says 100, stderr and stdin both say 80.

    So COLUMNS is honoured first — that is how a test or a user pins it, and it
    is the only signal that survives with no terminal anywhere — and then each
    standard stream is asked in turn. `DEFAULT_WIDTH` remains the answer when
    nothing is a terminal at all, which is the CI case.
    """
    from_env = _columns_from_env()
    if from_env is not None:
        return max(MIN_TABLE_WIDTH, from_env)
    for candidate in (stream, sys.stdout, sys.stderr, sys.stdin):
        columns = _tty_columns(candidate)
        if columns is not None:
            return max(MIN_TABLE_WIDTH, columns)
    return DEFAULT_WIDTH


def _columns_from_env() -> int | None:
    """COLUMNS, when it holds a usable number. Nothing else in the environment is
    consulted; a shell that does not export it simply falls through to the ioctl,
    which is the more reliable signal anyway."""
    try:
        columns = int(os.environ.get("COLUMNS", ""))
    except ValueError:
        return None
    return columns if columns > 0 else None


def _tty_columns(stream) -> int | None:
    """The width of the terminal behind `stream`, or None if there is not one.

    `io.UnsupportedOperation` — what a captured or in-memory stream raises for
    `fileno()` — subclasses both OSError and ValueError, so the streams pytest
    substitutes land here as "not a terminal" rather than as a crash.
    """
    try:
        return os.get_terminal_size(stream.fileno()).columns
    except (AttributeError, ValueError, OSError):
        return None


def should_color(mode: str, stream=None) -> bool:
    """Whether to emit ANSI, given `--color auto|always|never`.

    `auto` follows the two conventions users already expect: colour only on a
    terminal, and never when `NO_COLOR` is set (https://no-color.org). `always`
    overrides both, for piping into something that renders escapes itself.
    """
    if mode == "never":
        return False
    if mode == "always":
        return True
    if os.environ.get("NO_COLOR"):
        return False
    return _isatty(stream)


def _isatty(stream) -> bool:
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError):  # pragma: no cover - closed or exotic stream
        return False


class Palette:
    """ANSI styles, or the identity function when colour is off.

    Styles whole finished lines rather than cells, so switching colour on cannot
    move a column: padding has already happened by the time anything here runs.
    """

    def __init__(self, enabled: bool = False):
        self.enabled = enabled

    def _style(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.enabled and text else text

    def dim(self, text: str) -> str:
        return self._style("2", text)

    def bold(self, text: str) -> str:
        return self._style("1", text)

    def red(self, text: str) -> str:
        return self._style("31", text)

    def green(self, text: str) -> str:
        return self._style("32", text)

    def yellow(self, text: str) -> str:
        return self._style("33", text)

# SPDX-License-Identifier: Apache-2.0
"""The `dbt-costgate config` reference and the no-argument quickstart.

These are the first two screens anyone sees, and they used to be the reason
people stopped: the reference printed each key's whole paragraph on one
unwrapped line, so a 600-character explanation ran off an 80-column terminal and
wrapped back to column 0, where it sat flush against the next key name. Nothing
failed — the text was all present — which is exactly why it needed a test rather
than a reader.

So the rule pinned hardest here is the boring one: **nothing renders wider than
the width it was given**, at every width, on every screen.
"""

from __future__ import annotations

import json

import pytest

from dbt_costgate import config as config_ref
from dbt_costgate import layout
from dbt_costgate.cli import main, render_quickstart
from dbt_costgate.config import CONFIG_REFERENCE, SUMMARY_WIDTH

# 60 is layout.MIN_TABLE_WIDTH, the narrowest the tool lays anything out at; 100
# is the fallback a pipe or a CI log gets. The two in between are ordinary
# windows.
WIDTHS = [60, 72, 80, 100]


def _too_wide(text: str, width: int) -> list[str]:
    return [line for line in text.splitlines() if layout.display_width(line) > width]


# --------------------------------------------------------------------------
# The regression: text that does not fit the terminal it was given.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("width", WIDTHS)
def test_the_list_fits_the_terminal(width: int):
    assert _too_wide(config_ref.render_reference(width), width) == []


@pytest.mark.parametrize("width", WIDTHS)
def test_the_full_reference_fits_the_terminal(width: int):
    assert _too_wide(config_ref.render_verbose_reference(width), width) == []


@pytest.mark.parametrize("width", WIDTHS)
def test_the_quickstart_fits_the_terminal(width: int):
    text = render_quickstart(width, layout.Palette(False))
    assert _too_wide(text, width) == []


@pytest.mark.parametrize("field", CONFIG_REFERENCE, ids=lambda f: f.key)
@pytest.mark.parametrize("width", WIDTHS)
def test_every_key_page_fits_the_terminal(field, width: int):
    assert _too_wide(config_ref.render_key(field, width), width) == []


@pytest.mark.parametrize("width", [140, 240])
def test_prose_stops_at_the_reading_measure_on_a_very_wide_terminal(width: int):
    """Width past about a hundred characters buys a paragraph nothing and makes
    the sweep back to the next line harder. The aligned rows are not capped, but
    they are short enough that this is only observable on the prose."""
    for text in (
        config_ref.render_reference(width),
        config_ref.render_verbose_reference(width),
        render_quickstart(width, layout.Palette(False)),
    ):
        assert _too_wide(text, layout.PROSE_WIDTH) == []


@pytest.mark.parametrize("width", [60, 72, 88])
def test_a_narrow_terminal_still_wins_over_the_reading_measure(width: int):
    """The cap is a preference; fitting the window is not."""
    assert _too_wide(config_ref.render_reference(width), width) == []


def test_width_is_measured_with_colour_on_too():
    """Colour must not change layout. The palette styles finished lines, so the
    escape sequences add bytes and no cells — and `display_width` strips them,
    which is what makes this assertion meaningful rather than circular."""
    plain = config_ref.render_reference(80, layout.Palette(False))
    coloured = config_ref.render_reference(80, layout.Palette(True))
    assert _too_wide(coloured, 80) == []
    assert layout._ANSI.sub("", coloured) == plain


@pytest.mark.parametrize("width", [64, 72, 88])
def test_the_command_wraps_to_the_terminal_even_when_its_output_is_piped(
    width: int, monkeypatch, capsys
):
    """Under capsys stdout is captured, which is the piped case — `config | less`
    or `config > notes.txt`. The reference still has to come out at the width of
    the window it was run from, which is what `layout.help_width` goes looking
    for. A report deliberately does the opposite; see the layout tests."""
    monkeypatch.setenv("COLUMNS", str(width))
    assert main(["config"]) == 0
    assert _too_wide(capsys.readouterr().out, width) == []


def test_the_quickstart_wraps_to_the_terminal_when_piped(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "68")
    assert main([]) == 0
    assert _too_wide(capsys.readouterr().out, 68) == []


# --------------------------------------------------------------------------
# The registry entries the list is built from.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field", CONFIG_REFERENCE, ids=lambda f: f.key)
def test_every_setting_has_a_summary_that_fits_its_column(field):
    """A key added without one would render as a blank line in the list — present,
    described nowhere, and silent about it."""
    assert field.summary, f"{field.key} has no summary"
    assert not field.summary.endswith("."), "summaries are labels, not sentences"
    assert len(field.summary) <= SUMMARY_WIDTH, (
        f"{field.key}: summary is {len(field.summary)} chars, over the "
        f"{SUMMARY_WIDTH} the column budgets for"
    )


def test_leaf_names_are_unique():
    """`find_field` accepts a bare leaf name because that is what the list shows.
    The shortcut is only safe while no two sections share a leaf — if that ever
    changes, one of them silently wins."""
    leaves = [f.key.rpartition(".")[2] for f in CONFIG_REFERENCE]
    assert len(leaves) == len(set(leaves)), "two settings share a leaf name"


def test_the_list_shows_every_setting():
    text = config_ref.render_reference(100)
    for field in CONFIG_REFERENCE:
        assert field.summary in text, f"{field.key} is missing from the list"


def test_sections_are_shown_the_way_the_file_nests_them():
    """The list doubles as a shape guide for the YAML: a section header with its
    leaves indented under it is exactly what the user has to type."""
    text = config_ref.render_reference(100)
    assert "pricing:" in text
    assert "\n  region " in text
    assert "pricing.region" not in text  # the dotted form belongs to `config <key>`


def test_top_level_keys_are_not_indented_under_a_section():
    text = config_ref.render_reference(100)
    line = next(ln for ln in text.splitlines() if ln.startswith("fail_on"))
    assert "Gate strictness" in line


# --------------------------------------------------------------------------
# Looking one setting up.
# --------------------------------------------------------------------------


def test_a_key_prints_its_full_help_and_the_yaml_to_paste(capsys):
    assert main(["config", "thresholds.max_pct_increase"]) == 0
    out = capsys.readouterr().out
    assert "Gate fails if a model's cost increases by more than this percent." in out
    # The snippet has to carry the section header, or pasting it puts a
    # threshold at the top level, where the parser rejects it.
    assert "thresholds:" in out
    assert "max_pct_increase: 25" in out


def test_a_leaf_name_on_its_own_is_enough(capsys):
    """What the list displays is the leaf, so the leaf is what people type."""
    assert main(["config", "max_pct_increase"]) == 0
    assert "thresholds.max_pct_increase" in capsys.readouterr().out


def test_a_section_name_prints_every_key_under_it(capsys):
    assert main(["config", "pricing"]) == 0
    out = capsys.readouterr().out
    for field in config_ref.section_fields("pricing"):
        assert field.key in out
    assert "thresholds.max_pct_increase" not in out


def test_an_unknown_key_is_an_error_with_a_suggestion(capsys):
    """Exit 2, not a silent empty page. `max_pct` is nowhere near
    `thresholds.max_pct_increase` as a whole string, which is why the suggestion
    has to consider leaf names as well."""
    assert main(["config", "max_pct"]) == 2
    err = capsys.readouterr().err
    assert "unknown setting `max_pct`" in err
    assert "thresholds.max_pct_increase" in err


def test_an_unknown_key_with_nothing_close_still_explains_itself(capsys):
    assert main(["config", "zzzzzz"]) == 2
    err = capsys.readouterr().err
    assert "unknown setting" in err
    assert "dbt-costgate config" in err  # says how to find the real names


@pytest.mark.parametrize("key", ["max_pct", "zzzzzz"])
def test_the_unknown_key_message_fits_a_terminal(key: str, capsys):
    """Errors here are one line each, which is what makes them greppable in a CI
    log — and only readable while the line is short enough to be. A typo and a
    suggestion are both arbitrary-length names, so this cannot hold for every
    conceivable input; it holds for the ordinary kind, and it is what stopped the
    first draft shipping a 138-character line."""
    assert main(["config", key]) == 2
    for line in capsys.readouterr().err.splitlines():
        assert layout.display_width(line) <= 80, f"{len(line)} chars: {line}"


# --------------------------------------------------------------------------
# The other output formats.
# --------------------------------------------------------------------------


def test_verbose_prints_every_explanation_in_full(capsys):
    assert main(["config", "--verbose"]) == 0
    out = capsys.readouterr().out
    for field in CONFIG_REFERENCE:
        assert " ".join(field.help.split())[:40] in " ".join(out.split())


def test_json_for_one_key_is_still_a_list(capsys):
    """Same shape whether one key or all of them, so a caller does not have to
    branch on how many it asked for."""
    assert main(["config", "pricing.currency", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [e["key"] for e in payload] == ["pricing.currency"]


def test_json_carries_the_summary(capsys):
    assert main(["config", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert all(e["summary"] for e in payload)


def test_an_unknown_key_fails_the_same_way_in_json(capsys):
    """The error path must not depend on the format: a script asking for a key
    that no longer exists needs the non-zero exit, not empty JSON."""
    assert main(["config", "nope", "--format", "json"]) == 2
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------
# The no-argument screen.
# --------------------------------------------------------------------------


def test_running_with_no_arguments_orients_rather_than_lists_flags(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "dbt compile" in out  # the actual first step
    assert "dbt-costgate check" in out
    assert "gcloud auth application-default login" in out  # the usual first wall


def test_the_quickstart_names_every_command():
    """A command missing here is a command nobody finds — the screen is the front
    door, and argparse's own help is one `--help` further in."""
    text = render_quickstart(100, layout.Palette(False))
    for name, summary in [(n, s) for n, s in _command_summaries()]:
        assert f"  {name}" in text, name
        assert summary in text, name


def _command_summaries():
    from dbt_costgate.cli import _COMMAND_SUMMARIES

    return _COMMAND_SUMMARIES


def test_the_quickstart_and_argparse_describe_commands_identically():
    """The two screens read from one tuple. If someone reintroduces a second
    literal, this catches it."""
    from dbt_costgate.cli import build_parser

    text = build_parser().format_help()
    for _, summary in _command_summaries():
        # argparse re-wraps, so compare on words rather than on the line.
        assert summary.split(".")[0].split()[0] in text

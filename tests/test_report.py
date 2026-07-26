# SPDX-License-Identifier: Apache-2.0
import json

import pytest

from dbt_costgate import report
from dbt_costgate.models import (
    BASIS_LABELS,
    CostDelta,
    EstimateBasis,
    Notice,
    PricingDisclosure,
    Report,
    Status,
    Verdict,
)

_FULL_REFRESH = BASIS_LABELS[EstimateBasis.FULL_REFRESH]
_INCREMENTAL_FORM = BASIS_LABELS[EstimateBasis.INCREMENTAL_FORM]


def _flat(text: str) -> str:
    """Terminal output with its line breaks collapsed.

    The report wraps prose to the terminal width, so a sentence that is one string
    in the source arrives split across lines. Assertions about *what* was said run
    against this; assertions about layout run against the raw output."""
    return " ".join(text.split())


def _report(mode="diff", status=Status.FAIL):
    deltas = [
        CostDelta(
            name="fct_orders_daily",
            unique_id="model.pkg.fct_orders_daily",
            is_incremental=True,
            basis=EstimateBasis.FULL_REFRESH,
            is_new=False,
            gateable=True,
            bytes_baseline=71_000_000,
            bytes_current=3_200_000_000_000,
            usd_baseline=0.0004,
            usd_current=18.19,
            region="US",
            warnings=[BASIS_LABELS[EstimateBasis.FULL_REFRESH].warning],
            runs_per_month=30,
        ),
        CostDelta(
            name="inc_missing",
            unique_id="model.pkg.inc_missing",
            is_incremental=True,
            basis=EstimateBasis.FULL_REFRESH,
            is_new=False,
            gateable=False,
            bytes_baseline=None,
            bytes_current=None,
            usd_baseline=None,
            usd_current=None,
            region="US",
            error="not estimated — incremental target not built",
            runs_per_month=30,
        ),
    ]
    disclosure = PricingDisclosure(
        regions={"US": 6.25},
        source="built-in table",
        table_version="2026.07",
        last_verified="2026-07-23",
    )
    verdict = Verdict(
        status=status, breaches=["fct_orders_daily: USD +18.19/run exceeds USD 5.00"], exit_code=1
    )
    return Report(deltas=deltas, disclosure=disclosure, verdict=verdict, mode=mode)


def test_terminal_shows_rows_flags_disclosure_and_gate():
    out = report.render_terminal(_report())
    assert "fct_orders_daily" in out
    assert "full-refresh" in out
    assert "USD +18.19" in out
    assert "not estimated" in out
    assert "GATE: FAIL" in out
    assert "USD 6.25/TiB" in out
    assert "nothing executed" in out


def test_markdown_has_table_gate_and_footer():
    out = report.render_markdown(_report())
    assert "| Model |" in out
    assert "`fct_orders_daily`" in out
    assert "Gate: FAIL" in out
    assert "2026.07" in out
    assert "<sub>" in out


def test_json_is_valid_and_structured():
    payload = json.loads(report.render_json(_report()))
    assert payload["mode"] == "diff"
    assert payload["verdict"]["exit_code"] == 1
    names = [m["name"] for m in payload["models"]]
    assert "fct_orders_daily" in names
    assert payload["pricing"]["table_version"] == "2026.07"


def test_absolute_mode_renders_scanned_not_delta():
    out = report.render_terminal(_report(mode="absolute", status=Status.PASS))
    assert "scanned" in out
    assert "GATE: PASS" in out


def _mixed_report():
    # one delta so render_terminal emits the disclosure footer (empty reports
    # short-circuit before it)
    deltas = [
        CostDelta(
            name="m",
            unique_id="model.pkg.m",
            is_incremental=False,
            is_new=True,
            gateable=True,
            bytes_baseline=None,
            bytes_current=1_000_000_000,
            usd_baseline=None,
            usd_current=0.006,
            region="europe-west3",
            runs_per_month=30,
        )
    ]
    disclosure = PricingDisclosure(
        regions={"europe-west3": 4.80, "US": 6.25},
        source="built-in table + user override",
        table_version="2026.07",
        last_verified="2026-07-23",
        region_sources={"europe-west3": "user-override", "US": "region-table"},
    )
    return Report(
        deltas=deltas, disclosure=disclosure, verdict=Verdict(Status.PASS), mode="absolute"
    )


def test_json_exposes_region_sources():
    payload = json.loads(report.render_json(_mixed_report()))
    assert payload["pricing"]["region_sources"] == {
        "europe-west3": "user-override",
        "US": "region-table",
    }
    assert payload["pricing"]["source"] == "built-in table + user override"


def test_terminal_annotates_per_region_source_when_mixed():
    out = report.render_terminal(_mixed_report())
    assert "europe-west3 USD 4.80/TiB (override)" in out
    assert "US USD 6.25/TiB (table)" in out


def test_terminal_stays_clean_when_single_source():
    # the default fixture is single-source; no per-region markers should appear
    out = report.render_terminal(_report(mode="absolute", status=Status.PASS))
    assert "(override)" not in out
    assert "(table)" not in out


@pytest.mark.parametrize("fmt", ["terminal", "markdown", "json"])
def test_empty_report_is_graceful(fmt):
    disclosure = PricingDisclosure(
        regions={"US": 6.25}, source="built-in table", table_version="2026.07", last_verified="x"
    )
    rep = Report(deltas=[], disclosure=disclosure, verdict=Verdict(Status.PASS), mode="absolute")
    out = report.render(rep, fmt)
    assert out  # renders something, no crash


def _unpriced_report(mode="diff"):
    """A capacity/flat-rate-slots run: every applied rate is 0."""
    deltas = [
        CostDelta(
            name="fct_orders",
            unique_id="model.pkg.fct_orders",
            is_incremental=False,
            is_new=False,
            gateable=True,
            bytes_baseline=2 * 1024**4,
            bytes_current=9 * 1024**4,
            usd_baseline=0.0,
            usd_current=0.0,
            region="US",
            runs_per_month=30,
        )
    ]
    return Report(
        deltas=deltas,
        disclosure=PricingDisclosure(
            regions={"US": 0.0},
            source="user override",
            table_version="2026.07",
            last_verified="2026-07-25",
            region_sources={"US": "user-override"},
        ),
        verdict=Verdict(
            status=Status.FAIL, breaches=["fct_orders: +350% exceeds 25%"], exit_code=1
        ),
        mode=mode,
    )


def test_amounts_carry_their_iso_code_not_a_symbol():
    rep = _report()
    rep.disclosure.currency = "EUR"
    out = report.render_terminal(rep)
    assert "EUR 6.25/TiB" in out
    assert "EUR +18.19" in out
    assert "$" not in out  # no currency symbol survives anywhere


def test_zero_rate_report_drops_money_and_shows_byte_growth():
    out = report.render_terminal(_unpriced_report())
    assert "bytes only (no per-byte price configured)" in out
    # Baseline and current are adjacent columns now, so the before/after reads
    # across one row rather than through an arrow.
    row = next(line for line in out.splitlines() if "fct_orders" in line)
    assert "2.00 TiB" in row and "9.00 TiB" in row
    assert "+350%" in out
    assert "USD" not in out and "$" not in out  # no amount is claimed
    assert "rate is 0 for US" in out


def test_zero_rate_markdown_has_no_cost_columns():
    out = report.render_markdown(_unpriced_report())
    assert "| Model | Baseline | This change | Δ % |" in out
    assert "/ run" not in out and "/ month" not in out
    assert "+350%" in out


def test_zero_rate_absolute_mode_reports_scanned_bytes_only():
    out = report.render_markdown(_unpriced_report(mode="absolute"))
    assert "| Model | Scanned |" in out
    assert "Cost" not in out


def test_priced_absolute_headings_are_currency_neutral():
    # the code lives on each amount, so the heading must not hard-code one
    out = report.render_markdown(_report(mode="absolute"))
    assert "| Model | Scanned | Cost / run | Cost / month |" in out
    assert "USD 18.19" in out


def test_json_states_currency_and_whether_anything_was_priced():
    payload = json.loads(report.render_json(_report()))
    assert payload["pricing"]["currency"] == "USD"
    assert payload["pricing"]["priced"] is True
    # the usd_* model keys are a published contract and keep their names
    assert "usd_current" in payload["models"][0]

    unpriced = json.loads(report.render_json(_unpriced_report()))
    assert unpriced["pricing"]["priced"] is False


def _header_of(md: str) -> list[str]:
    line = next(ln for ln in md.splitlines() if ln.startswith("| Model |"))
    return [c.strip() for c in line.strip("|").split("|")]


def test_unpriced_diff_table_is_the_priced_one_minus_its_money_columns():
    """The two shapes must nest, not diverge.

    `Δ %` needs no currency, so it appears in both. Anything the unpriced table
    shows must therefore also be in the priced table, in the same order — if the
    two ever drift into differently-shaped tables, this fails.
    """
    priced = _header_of(report.render_markdown(_report()))
    unpriced = _header_of(report.render_markdown(_unpriced_report()))
    assert priced == ["Model", "Baseline", "This change", "Δ %", "Δ / run", "Δ / month"]
    assert unpriced == ["Model", "Baseline", "This change", "Δ %"]
    assert priced[: len(unpriced)] == unpriced  # a strict prefix: nested, not divergent


def test_terminal_reports_the_same_figures_as_markdown_in_both_shapes():
    # percentage present either way; money only when priced
    assert "+350%" in report.render_terminal(_unpriced_report())
    priced_out = report.render_terminal(_report())
    assert "%" in priced_out and "USD 18.19" in priced_out


# --- net impact -------------------------------------------------------------

_TIB = 1024**4


def _delta(name, baseline, current, rate=6.25, runs=30, error=None):
    return CostDelta(
        name=name,
        unique_id=f"model.pkg.{name}",
        is_incremental=False,
        is_new=False,
        gateable=True,
        bytes_baseline=baseline,
        bytes_current=current,
        usd_baseline=None if baseline is None else baseline / _TIB * rate,
        usd_current=None if current is None else current / _TIB * rate,
        region="US",
        runs_per_month=runs,
        error=error,
    )


def _net_report(deltas, rate=6.25, mode="diff", status=Status.PASS, breaches=()):
    return Report(
        deltas=deltas,
        disclosure=PricingDisclosure(
            regions={"US": rate},
            source="built-in table",
            table_version="2026.07",
            last_verified="2026-07-25",
            region_sources={"US": "region-table"},
        ),
        verdict=Verdict(status=status, breaches=list(breaches), exit_code=0),
        mode=mode,
    )


def test_a_change_that_lowers_cost_is_reported_as_a_saving():
    rep = _net_report([_delta("fct_orders", 9 * _TIB, 2 * _TIB)])
    out = report.render_terminal(rep)
    assert "Net saving: USD 43.75/run · USD 1,312.50/month" in out
    # the figures are unsigned because the word already carries the direction
    assert "Net saving: USD -43.75" not in out


def test_a_change_that_raises_cost_is_reported_as_an_increase():
    rep = _net_report([_delta("fct_orders", 2 * _TIB, 9 * _TIB)])
    assert "Net increase: USD 43.75/run" in report.render_terminal(rep)


def test_net_sums_across_models_and_can_be_a_saving_while_the_gate_fails():
    """The net is a measurement; the gate is a verdict. They may disagree.

    A pull request can reduce total cost while still containing one model that
    breached its own threshold, and the report must not imply otherwise.
    """
    deltas = [_delta("a", 2 * _TIB, 9 * _TIB), _delta("b", 12 * _TIB, 3 * _TIB)]
    rep = _net_report(deltas, status=Status.FAIL, breaches=["a: over"])
    out = report.render_terminal(rep)
    assert "Net saving: USD 12.50/run" in out  # +7 TiB then -9 TiB = -2 TiB
    assert "GATE: FAIL" in out


def test_no_net_movement_says_so_rather_than_showing_zero():
    rep = _net_report([_delta("fct_orders", 4 * _TIB, 4 * _TIB)])
    assert "Net change: none" in report.render_terminal(rep)


def test_net_discloses_when_some_models_could_not_be_estimated():
    deltas = [_delta("a", 2 * _TIB, 9 * _TIB), _delta("b", None, None, error="not estimated")]
    out = report.render_terminal(_net_report(deltas))
    assert "across 1 of 2 models" in out


def test_net_is_bytes_when_no_rate_is_configured():
    rep = _net_report([_delta("fct_orders", 9 * _TIB, 2 * _TIB, rate=0.0)], rate=0.0)
    out = report.render_terminal(rep)
    assert "Net saving: 7.00 TiB/run scanned" in out
    assert "USD" not in out


def test_absolute_mode_has_no_net_because_it_has_no_baseline():
    rep = _net_report([_delta("fct_orders", None, 9 * _TIB)], mode="absolute")
    assert "Net " not in report.render_terminal(rep)
    assert "Net " not in report.render_markdown(rep)


def test_monthly_net_is_omitted_rather_than_summed_partially():
    # one model has no run frequency, so a monthly total would silently omit it
    deltas = [_delta("a", 2 * _TIB, 9 * _TIB), _delta("b", 1 * _TIB, 2 * _TIB, runs=None)]
    rep = _net_report(deltas)
    assert rep.net_usd_per_month is None
    out = report.render_terminal(rep)
    assert "/run" in out and "/month" not in out.split("Net increase")[1].split("\n")[0]


def test_json_net_is_signed_so_a_saving_is_negative():
    saving = json.loads(report.render_json(_net_report([_delta("m", 9 * _TIB, 2 * _TIB)])))
    assert saving["net"]["usd_per_run"] < 0
    assert saving["net"]["bytes"] < 0
    assert saving["net"]["models_estimated"] == 1

    rise = json.loads(report.render_json(_net_report([_delta("m", 2 * _TIB, 9 * _TIB)])))
    assert rise["net"]["usd_per_run"] > 0


def test_markdown_shows_the_net_above_the_gate_verdict():
    out = report.render_markdown(_net_report([_delta("m", 9 * _TIB, 2 * _TIB)]))
    assert "**Net saving:** USD 43.75/run" in out
    assert out.index("Net saving") < out.index("Gate:")


# --- advisory notices -------------------------------------------------------

_NOTICE = Notice(
    id="dead-money-thresholds",
    message="thresholds.max_usd_total cannot fire: no per-byte price is configured.",
)


def test_notices_render_in_every_format_and_sit_with_the_disclosure():
    rep = _report()
    rep.notices = [_NOTICE]
    body = _NOTICE.message

    term = report.render_terminal(rep)
    # The id leads the line: it is what a user types into `notices.silence`.
    assert f"⚠ {_NOTICE.id} {body}" in _flat(term)
    # Placed with the provenance footer, not with the gate: both describe how the
    # run was configured rather than what the change did.
    assert term.index(body) > term.index("GATE:")
    assert term.index(body) < term.index("Pricing:")

    md = report.render_markdown(rep)
    assert f"> ⚠ **{_NOTICE.id}** — {body}" in md
    assert md.index(body) < md.index("<sub>")

    assert json.loads(report.render_json(rep))["notices"] == [{"id": _NOTICE.id, "message": body}]


def test_absent_notices_add_nothing():
    rep = _report()
    assert rep.notices == []
    # The fixture's incremental model still warns, now as the collapsed footnote.
    # Asserted as the complete list of ⚠ lines rather than by stripping known
    # text: a reworded footnote fails here loudly instead of quietly leaving an
    # assertion that matches nothing.
    warned = [line for line in report.render_terminal(rep).splitlines() if "⚠" in line]
    assert len(warned) == 1
    assert _FULL_REFRESH.footnote in _flat(report.render_terminal(rep))
    assert json.loads(report.render_json(rep))["notices"] == []


def test_a_notice_is_not_confusable_with_a_gate_breach():
    """Breaches are the reason a run failed; a notice is not. Rendering one where
    the other belongs would make an advisory note read as a blocking one."""
    rep = _report(status=Status.PASS)
    rep.verdict.breaches = []
    rep.notices = [_NOTICE]
    term = report.render_terminal(rep)
    assert "GATE: PASS" in term
    assert f"    - {_NOTICE.message}" not in term
    md = report.render_markdown(rep)
    assert "✅ **Gate: PASS**" in md
    assert f"\n- {_NOTICE.message}" not in md


def _md_footer(out: str) -> list[str]:
    return out.rsplit("<sub>", 1)[1].split("</sub>")[0].split("<br/>")


def test_a_priced_footer_discloses_the_free_tier_it_does_not_deduct():
    """The one adjustment that separates these figures from the same bytes on an
    invoice, and it is not applied. It belongs beside the figure rather than only
    in the docs — a reader comparing a report against a bill has the report in
    front of them and the docs somewhere else."""
    for out in (report.render_terminal(_report()), report.render_markdown(_report())):
        assert "free tier" in out
        assert "never deducted" in out


def test_the_footer_stays_on_the_meter_the_report_is_about():
    """Every figure here prices bytes scanned, which is compute. Storage is a
    separate BigQuery meter that this tool does not price at all — a scope
    boundary, documented as a non-goal, and not an adjustment pending on a compute
    number. Naming it under one would imply the report had weighed it and found it
    zero, and would invite the same disclaimer for every other meter dbt-costgate
    is equally not about.
    """
    for out in (report.render_terminal(_report()), report.render_markdown(_report())):
        assert "storage" not in out.lower()


def test_an_unpriced_footer_claims_no_adjustment_to_a_price_it_never_quoted():
    """Under slots the report quotes no money at all, so "the free tier is not
    deducted" would describe arithmetic that did not happen — and the free tier is
    an on-demand allowance that does not apply to capacity pricing anyway. The
    unpriced disclosure already says the report measures scanned bytes only, which
    is the same statement for a report with no money in it."""
    for out in (
        report.render_terminal(_unpriced_report()),
        report.render_markdown(_unpriced_report()),
    ):
        assert "free tier" not in out
        assert "measures scanned bytes only" in out


def test_both_renderers_carry_the_same_footer_notes():
    """The free-tier note is conditional, and the terminal and markdown footers
    are assembled separately. Left unchecked, one renderer keeps a note the other
    drops and the same run discloses different things depending on where it is
    read. Compared as a whole list so an added note is covered without anyone
    remembering to extend this test."""
    for rep in (_report(), _unpriced_report()):
        # The terminal footer is wrapped to the width, so it is compared as text
        # rather than line by line — the notes have to be the same sentences in
        # the same order, not the same line breaks.
        terminal = _flat(report.render_terminal(rep))
        markdown = _md_footer(report.render_markdown(rep))
        assert terminal.endswith(_flat(" ".join(markdown)))


def _incrementals(n: int) -> Report:
    """n incremental models, every one of them carrying the warning."""
    deltas = [
        CostDelta(
            name=f"fct_{i}",
            unique_id=f"model.pkg.fct_{i}",
            is_incremental=True,
            basis=EstimateBasis.FULL_REFRESH,
            is_new=False,
            gateable=True,
            bytes_baseline=1_000_000,
            bytes_current=2_000_000,
            usd_baseline=0.01,
            usd_current=0.02,
            region="US",
            warnings=[BASIS_LABELS[EstimateBasis.FULL_REFRESH].warning],
        )
        for i in range(n)
    ]
    return Report(
        deltas=deltas,
        disclosure=PricingDisclosure(
            regions={"US": 6.25},
            source="built-in table",
            table_version="2026.07",
            last_verified="2026-07-23",
        ),
        verdict=Verdict(status=Status.PASS, breaches=[], exit_code=0),
        mode="diff",
    )


def test_the_incremental_warning_is_said_once_however_many_rows_carry_it():
    """It explains what the `full-refresh` tag means, which is the same sentence
    for every row that has one. Repeated per model it grew with the number of
    incrementals in the change and pushed the caveats that are about a specific
    model — a dynamic filter, a missing baseline — under a wall of repeats.

    Both halves are asserted. Counting the footnote alone proves nothing: it is
    worded differently from the warning it replaces, so it stays at one whether or
    not the per-row repeats came back beneath it.
    """
    for render in (report.render_terminal, report.render_markdown):
        out = _flat(render(_incrementals(4)))
        assert out.count(_FULL_REFRESH.footnote) == 1
        assert _FULL_REFRESH.warning not in out


def test_every_incremental_row_keeps_its_own_tag():
    """The footnote replaces the repeated sentence, not the per-row marking. A
    reader still has to be able to tell *which* rows it is talking about, so
    collapsing the prose while also dropping the tag would leave the footnote
    referring to nothing."""
    rep = _incrementals(4)
    # One per table row: the tag has its own column now, so it is counted on the
    # rows rather than by a parenthetical that no longer exists.
    tagged = [line for line in report.render_terminal(rep).splitlines() if "full-refresh" in line]
    assert len([line for line in tagged if line.strip().startswith("fct_")]) == 4
    assert report.render_markdown(rep).count("_full-refresh_") == 4


def test_a_models_own_warnings_still_render_on_its_row():
    """Only the shared one collapses. A caveat that applies to one model is the
    reason the warning list exists, and must not be swept into the footnote."""
    rep = _incrementals(2)
    rep.deltas[0].warnings.append("dynamic filter — dry-run may be worst-case (overestimate)")
    assert "  fct_0" in report.render_terminal(rep)
    assert "⚠ fct_0 dynamic filter" in _flat(report.render_terminal(rep))
    assert "> ⚠ **fct_0** — dynamic filter" in report.render_markdown(rep)


def test_no_footnote_when_no_row_carries_the_warning():
    rep = _incrementals(1)
    rep.deltas[0].warnings = []
    for render in (report.render_terminal, report.render_markdown):
        assert _FULL_REFRESH.footnote not in render(rep)


def test_the_footnote_follows_the_warning_and_not_the_tag():
    """The two conditions look interchangeable and are not: `sql_warnings`
    returns nothing at all for a model with no compiled SQL, so a row can be
    incremental — and tagged — while never having carried the warning. Keyed on
    the tag, the footnote would speak for rows the per-row lines never covered,
    which is a claim about a figure that was never measured."""
    rep = _incrementals(1)
    rep.deltas[0].warnings = []
    assert rep.deltas[0].is_incremental
    terminal = report.render_terminal(rep)
    assert _FULL_REFRESH.tag in terminal
    assert _FULL_REFRESH.footnote not in _flat(terminal)


def _basis_row(name: str, basis: EstimateBasis) -> CostDelta:
    return CostDelta(
        name=name,
        unique_id=f"model.pkg.{name}",
        is_incremental=True,
        basis=basis,
        is_new=False,
        gateable=True,
        bytes_baseline=1_000_000,
        bytes_current=2_000_000,
        usd_baseline=0.01,
        usd_current=0.02,
        region="US",
        warnings=[BASIS_LABELS[basis].warning],
    )


def _basis_report(*bases: EstimateBasis) -> Report:
    return Report(
        deltas=[_basis_row(f"m{i}", b) for i, b in enumerate(bases)],
        disclosure=PricingDisclosure(
            regions={"US": 6.25},
            source="built-in table",
            table_version="2026.07",
            last_verified="2026-07-23",
        ),
        verdict=Verdict(status=Status.PASS, breaches=[], exit_code=0),
        mode="diff",
    )


def test_an_incremental_form_row_is_not_labelled_a_rebuild():
    """The defect this exists to prevent. The model is incremental either way, so
    a tag derived from `is_incremental` called both rows `full-refresh` — telling
    a reader the figure was a rebuild when the dry-run measured a single run
    against the table as already built. On a large fact table those differ by
    orders of magnitude, and the wrong one reads low."""
    rep = _basis_report(EstimateBasis.INCREMENTAL_FORM)
    for out in (report.render_terminal(rep), report.render_markdown(rep)):
        assert _INCREMENTAL_FORM.tag in out
        assert _FULL_REFRESH.tag not in out
        assert _FULL_REFRESH.footnote not in out


def test_the_tag_follows_the_basis_and_not_is_incremental():
    """Stated directly, because the two agree on every row except the one that
    matters. `is_incremental` is a fact about the model; the basis is a fact about
    the number printed beside it, and only the second can label that number."""
    rep = _basis_report(EstimateBasis.INCREMENTAL_FORM)
    assert rep.deltas[0].is_incremental
    assert report._row_tag(rep.deltas[0]) == _INCREMENTAL_FORM.tag


def test_a_row_with_an_unknown_basis_is_left_unlabelled():
    """No basis means nothing was established about the shape that was measured.
    Falling back to a tag would be a guess printed as a fact; an absent tag is
    merely silent."""
    rep = _basis_report(EstimateBasis.FULL_REFRESH)
    # Both come from the same `detect_basis` call in the pipeline, so an unknown
    # basis means an unknown basis warning too. Clearing one and not the other
    # would build a state the estimator cannot produce.
    rep.deltas[0].basis = None
    rep.deltas[0].warnings = []
    assert report._row_tag(rep.deltas[0]) is None
    assert _FULL_REFRESH.tag not in report.render_terminal(rep)


def test_a_report_mixing_bases_explains_both_tags():
    """A change can touch one model compiled fresh and another compiled against
    its table. Printing a single footnote would leave one of the two tags on the
    page with nothing saying what it means — and the reader with no way to know
    which figure they are looking at."""
    rep = _basis_report(EstimateBasis.FULL_REFRESH, EstimateBasis.INCREMENTAL_FORM)
    for out in (_flat(report.render_terminal(rep)), report.render_markdown(rep)):
        assert out.count(_FULL_REFRESH.footnote) == 1
        assert out.count(_INCREMENTAL_FORM.footnote) == 1
        assert _FULL_REFRESH.tag in out and _INCREMENTAL_FORM.tag in out
        # still collapsed: neither per-model warning survives beside its row
        assert _FULL_REFRESH.warning not in out
        assert _INCREMENTAL_FORM.warning not in out


def test_json_states_which_basis_was_measured():
    """`is_incremental` was already there and cannot answer this: it is true for
    both shapes. A consumer gating on rebuild cost needs to know which figure it
    received, so the basis is exposed rather than left to be inferred."""
    payload = json.loads(report.render_json(_basis_report(EstimateBasis.INCREMENTAL_FORM)))
    assert payload["models"][0]["basis"] == "incremental_form"
    assert payload["models"][0]["is_incremental"] is True
    unknown = _basis_report(EstimateBasis.FULL_REFRESH)
    unknown.deltas[0].basis = None
    assert json.loads(report.render_json(unknown))["models"][0]["basis"] is None

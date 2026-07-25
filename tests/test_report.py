# SPDX-License-Identifier: Apache-2.0
import json

import pytest

from dbt_costgate import report
from dbt_costgate.models import (
    CostDelta,
    PricingDisclosure,
    Report,
    Status,
    Verdict,
)


def _report(mode="diff", status=Status.FAIL):
    deltas = [
        CostDelta(
            name="fct_orders_daily",
            unique_id="model.pkg.fct_orders_daily",
            is_incremental=True,
            is_new=False,
            gateable=True,
            bytes_baseline=71_000_000,
            bytes_current=3_200_000_000_000,
            usd_baseline=0.0004,
            usd_current=18.19,
            region="US",
            warnings=["incremental — figure is the full-refresh scan"],
            runs_per_month=30,
        ),
        CostDelta(
            name="inc_missing",
            unique_id="model.pkg.inc_missing",
            is_incremental=True,
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
        status=status, breaches=["fct_orders_daily: +USD 18.19/run exceeds USD 5.00"], exit_code=1
    )
    return Report(deltas=deltas, disclosure=disclosure, verdict=verdict, mode=mode)


def test_terminal_shows_rows_flags_disclosure_and_gate():
    out = report.render_terminal(_report())
    assert "fct_orders_daily" in out
    assert "full-refresh" in out
    assert "+USD 18.19" in out
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
    assert "+EUR 18.19" in out
    assert "$" not in out  # no currency symbol survives anywhere


def test_zero_rate_report_drops_money_and_shows_byte_growth():
    out = report.render_terminal(_unpriced_report())
    assert "bytes only (no per-byte price configured)" in out
    assert "2.00 TiB → 9.00 TiB" in out
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

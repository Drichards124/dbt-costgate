# SPDX-License-Identifier: Apache-2.0
import json

import pytest

from costgate import report
from costgate.models import (
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
        status=status, breaches=["fct_orders_daily: +$18.19/run exceeds $5.00"], exit_code=1
    )
    return Report(deltas=deltas, disclosure=disclosure, verdict=verdict, mode=mode)


def test_terminal_shows_rows_flags_disclosure_and_gate():
    out = report.render_terminal(_report())
    assert "fct_orders_daily" in out
    assert "full-refresh" in out
    assert "+$18.19" in out or "18.1" in out
    assert "not estimated" in out
    assert "GATE: FAIL" in out
    assert "$6.25/TiB" in out
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
    assert "europe-west3 $4.80/TiB (override)" in out
    assert "US $6.25/TiB (table)" in out


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

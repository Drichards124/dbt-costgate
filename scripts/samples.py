# SPDX-License-Identifier: Apache-2.0
"""Canonical generated doc content: example reports, and the config reference.

Single source of truth for every example in README.md and docs/usage.md.
`gen_samples.py` writes these into those files; `tests/test_samples.py` asserts
what is committed still matches what the renderers produce, so an example cannot
quietly drift away from the code the way a hand-written one does.

No BigQuery and no credentials are involved: `report.render_*` are pure functions
of a `Report`, so a hand-built one renders authentic output. The figures are
illustrative; the formatting is not.
"""

from __future__ import annotations

from dbt_costgate import notices, policy, report
from dbt_costgate.config import Config, Thresholds
from dbt_costgate.models import TIB, CostDelta, PricingDisclosure, Report
from dbt_costgate.pricing import PricingTable

MIB = 1024**2
GIB = 1024**3


def _table() -> PricingTable:
    """The bundled table, so the fallback notice quotes the real default rate."""
    return PricingTable.load()


# Rates are the real published ones for these regions, so an example never
# implies a price that dbt-costgate would not actually apply.
US_RATE = 6.25
NEGOTIATED_RATE = 4.10


def _delta(
    name: str,
    current: int | None,
    baseline: int | None = None,
    *,
    rate: float = US_RATE,
    incremental: bool = False,
    new: bool = False,
    warning: str | None = None,
    error: str | None = None,
    runs: int | None = 30,
    region: str = "US",
) -> CostDelta:
    def usd(b: int | None) -> float | None:
        return None if b is None else b / TIB * rate

    return CostDelta(
        name=name,
        unique_id=f"model.shop.{name}",
        is_incremental=incremental,
        is_new=new,
        gateable=error is None,
        bytes_baseline=baseline,
        bytes_current=current,
        usd_baseline=usd(baseline),
        usd_current=usd(current),
        region=region,
        warnings=[warning] if warning else [],
        error=error,
        runs_per_month=runs,
    )


def _report(
    deltas,
    *,
    mode: str,
    rate: float = US_RATE,
    source: str,
    thresholds: Thresholds,
    region: str = "US",
    rate_source: str | None = None,
):
    disclosure = PricingDisclosure(
        regions={region: rate},
        source=source,
        table_version="2026.07",
        last_verified="2026-07-25",
        region_sources={
            region: rate_source or ("region-table" if rate == US_RATE else "user-override")
        },
    )
    config = Config(thresholds=thresholds)
    return Report(
        deltas=deltas,
        disclosure=disclosure,
        verdict=policy.evaluate(deltas, config, currency="USD"),
        mode=mode,
        # Collected through the same registry the CLI uses, so a sample can never
        # advertise advice — or a silencing id — the tool does not actually give.
        notices=notices.collect(config, _table(), disclosure),
    )


# The change every example describes: a partition filter was dropped from an
# incremental model, and a small new dimension was added alongside it.
def _pr_deltas(rate: float = US_RATE):
    # The rate must be threaded in, not defaulted: pricing the deltas at one rate
    # while the disclosure line claims another produces a sample that contradicts
    # itself — which is exactly the class of error these generated samples exist
    # to prevent, so it must not be reintroduced here.
    return [
        _delta(
            "fct_orders_daily",
            int(2.91 * TIB),
            int(0.80 * TIB),
            rate=rate,
            incremental=True,
            warning="incremental — figure is the full-refresh scan",
        ),
        _delta("dim_customers", int(412.5 * MIB), rate=rate, new=True),
    ]


def _gate() -> Thresholds:
    return Thresholds(max_usd_increase_per_run=5.00, max_pct_increase=25.0)


def pr_comment() -> str:
    """What the GitHub Action posts. A PR comment *is* rendered markdown, so this
    is the comment itself rather than a picture of one."""
    return report.render_markdown(
        _report(_pr_deltas(), mode="diff", source="built-in table", thresholds=_gate())
    )


def diff_terminal() -> str:
    return report.render_terminal(
        _report(_pr_deltas(), mode="diff", source="built-in table", thresholds=_gate())
    )


def local_terminal() -> str:
    """Zero-setup local run: no baseline, so each model's current scan is priced."""
    deltas = [
        _delta(
            "fct_orders_daily",
            int(2.91 * TIB),
            incremental=True,
            warning="incremental — figure is the full-refresh scan",
        ),
        _delta("dim_customers", int(0.4016 * TIB)),
    ]
    return report.render_terminal(
        _report(deltas, mode="absolute", source="built-in table", thresholds=Thresholds())
    )


def saving_terminal() -> str:
    """A change that *lowers* cost — someone added a partition filter back."""
    deltas = [_delta("fct_orders_daily", int(2.00 * TIB), int(9.00 * TIB))]
    return report.render_terminal(
        _report(deltas, mode="diff", source="built-in table", thresholds=_gate())
    )


def negotiated_terminal() -> str:
    """A team on a negotiated or Editions rate: `pricing.usd_per_tib: 4.10`."""
    return report.render_terminal(
        _report(
            _pr_deltas(NEGOTIATED_RATE),
            mode="diff",
            rate=NEGOTIATED_RATE,
            source="user override",
            thresholds=_gate(),
        )
    )


def mixed_frequency_terminal() -> str:
    """The same change, with a per-model `run_frequency`.

    Each row states the frequency it used, so a monthly figure is never an
    unexplained number. The incremental here is fully rebuilt weekly rather than
    nightly, which is the distinction that matters: the figure being multiplied is
    the rebuild scan, not a nightly incremental run.
    """
    deltas = [
        _delta(
            "fct_orders_daily",
            int(2.91 * TIB),
            int(0.80 * TIB),
            incremental=True,
            warning="incremental — figure is the full-refresh scan",
            runs=4,
        ),
        _delta("dim_customers", int(412.5 * MIB), new=True, runs=30),
    ]
    return report.render_terminal(
        _report(deltas, mode="diff", source="built-in table", thresholds=_gate())
    )


def config_reference() -> str:
    """Every `.dbt-costgate.yml` key, rendered from `CONFIG_REFERENCE` — the same
    registry the `dbt-costgate config` command prints.

    Generated rather than written, because a hand-maintained config table is
    guaranteed to fall behind: it would have to be edited every time a key is
    added, by someone who has just finished adding the key. `CONFIG_REFERENCE`
    already has a bidirectional drift test against the `Config` dataclass, so a
    key cannot exist without appearing here.
    """
    from dbt_costgate.config import CONFIG_REFERENCE

    def default(value) -> str:
        if value is None:
            return "_none_"
        if value == {} or value == []:
            return "_empty_"
        return f"`{value}`"

    rows = [
        "| Key | Type | Default | What it does |",
        "|---|---|---|---|",
    ]
    for field in CONFIG_REFERENCE:
        help_text = " ".join(field.help.split())
        rows.append(
            f"| `{field.key}` | `{field.type_label}` | {default(field.default)} | {help_text} |"
        )
    return "\n".join(rows)


def _slot_deltas():
    return [
        _delta("fct_orders_daily", int(2.91 * TIB), int(0.80 * TIB), rate=0.0, incremental=True)
    ]


def slots_terminal() -> str:
    """Capacity/Editions slots: `pricing.usd_per_tib: 0.00`. No per-byte price
    exists, so the report measures scanned bytes and gates on growth.

    The thresholds here are deliberately the bytes-based ones. A dollar
    threshold would be inert at this rate — see `slots_dead_threshold_terminal`
    — so the canonical slot example must not quietly carry one.
    """
    return report.render_terminal(
        _report(
            _slot_deltas(),
            mode="diff",
            rate=0.0,
            source="user override",
            thresholds=Thresholds(max_pct_increase=25.0),
        )
    )


def slots_dead_threshold_terminal() -> str:
    """The same slot setup with a dollar threshold left in place from before the
    switch. It cannot fire, so the report says so rather than passing quietly."""
    return report.render_terminal(
        _report(
            _slot_deltas(),
            mode="diff",
            rate=0.0,
            source="user override",
            thresholds=_gate(),
        )
    )


def unknown_region_terminal() -> str:
    """A location the bundled table has no verified rate for — a region Google
    opened after the table was cut. It is priced from the default and the report
    says which direction that guess errs in."""
    fallback_rate = _table().default_usd_per_tib
    region = "northamerica-northeast3"
    deltas = [
        _delta(
            "fct_orders_daily",
            int(2.91 * TIB),
            int(0.80 * TIB),
            rate=fallback_rate,
            region=region,
        )
    ]
    return report.render_terminal(
        _report(
            deltas,
            mode="diff",
            rate=fallback_rate,
            source="default fallback",
            thresholds=_gate(),
            region=region,
            rate_source="default-fallback",
        )
    )


# name -> (renderer, fence language). The name is the marker used in the docs.
# A fence of None injects the sample raw: a PR comment *is* rendered markdown, so
# letting GitHub render it shows the comment itself rather than its source.
SAMPLES = {
    "pr-comment": (pr_comment, None),
    "diff-terminal": (diff_terminal, "text"),
    "local-terminal": (local_terminal, "text"),
    "saving-terminal": (saving_terminal, "text"),
    "negotiated-terminal": (negotiated_terminal, "text"),
    "slots-terminal": (slots_terminal, "text"),
    "slots-dead-threshold-terminal": (slots_dead_threshold_terminal, "text"),
    "unknown-region-terminal": (unknown_region_terminal, "text"),
    "mixed-frequency-terminal": (mixed_frequency_terminal, "text"),
    "config-reference": (config_reference, None),
}


def render(name: str) -> str:
    return SAMPLES[name][0]()


def fence(name: str) -> str:
    return SAMPLES[name][1]

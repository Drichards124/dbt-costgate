# SPDX-License-Identifier: Apache-2.0
"""Region-aware on-demand pricing.

Accuracy rule: never guess a region's rate silently. A region we don't have a
verified value for falls back to the default and the report says so, and a user
can always override with a negotiated rate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources

from dbt_costgate.models import TIB


@dataclass(frozen=True)
class RateResult:
    usd_per_tib: float
    region: str
    source: str  # "region-table" | "default-fallback" | "user-override"


def _ci_get(mapping: dict[str, float], region: str) -> float | None:
    """Look up a region rate case-insensitively. BigQuery reports multi-regions
    uppercase (US, EU) but regional locations lowercase (europe-west3), while
    config keys are hand-typed — so `europe-west3` must match `EUROPE-WEST3`."""
    if region in mapping:
        return mapping[region]
    folded = region.casefold()
    for key, value in mapping.items():
        if key.casefold() == folded:
            return value
    return None


@dataclass
class PricingTable:
    version: str
    last_verified: str
    source_url: str
    default_usd_per_tib: float
    regions: dict[str, float]
    cli_override_usd_per_tib: float | None = None
    override_regions: dict[str, float] = field(default_factory=dict)
    override_usd_per_tib: float | None = None
    override_region: str | None = None
    # ISO 4217 code the rates are denominated in. The bundled table is USD; a
    # different code only ever describes a rate the user supplied themselves.
    # dbt-costgate never converts between currencies — see `currency_is_sound`.
    currency: str = "USD"
    table_currency: str = "USD"

    @classmethod
    def load(
        cls,
        *,
        cli_override_usd_per_tib: float | None = None,
        override_regions: dict[str, float] | None = None,
        override_usd_per_tib: float | None = None,
        override_region: str | None = None,
        currency: str | None = None,
    ) -> PricingTable:
        raw = json.loads(
            resources.files("dbt_costgate.data").joinpath("pricing.json").read_text("utf-8")
        )
        table_currency = str(raw.get("currency") or "USD").upper()
        return cls(
            version=raw["version"],
            last_verified=raw["last_verified"],
            source_url=raw["source"],
            default_usd_per_tib=float(raw["default_usd_per_tib"]),
            regions={k: float(v) for k, v in raw["regions"].items()},
            cli_override_usd_per_tib=cli_override_usd_per_tib,
            override_regions=override_regions or {},
            override_usd_per_tib=override_usd_per_tib,
            override_region=override_region,
            currency=(currency or table_currency).upper(),
            table_currency=table_currency,
        )

    def currency_is_sound(self, applied_sources: dict[str, str]) -> str | None:
        """Reject a currency that would relabel bundled rates rather than describe
        the user's own.

        The bundled table is denominated in one currency (USD). Saying
        `currency: EUR` while any applied rate still comes from that table — or
        from the default fallback — would print a USD number with a EUR label,
        which is wrong in the one direction that matters: silently. Returns an
        explanatory message when that is happening, or None when it is not.

        dbt-costgate never converts. A non-default currency means "the rate I
        gave you is denominated in this", never "convert into this".
        """
        if self.currency == self.table_currency:
            return None
        bundled = sorted(r for r, src in applied_sources.items() if src != "user-override")
        if not bundled:
            return None
        return (
            f"pricing.currency is {self.currency} but the rate applied for "
            f"{', '.join(bundled)} came from the built-in table, which is "
            f"{self.table_currency}. dbt-costgate does not convert currencies. Set your "
            f"own {self.currency} rate for those regions with pricing.regions (or "
            f"pricing.usd_per_tib for all of them), or drop pricing.currency."
        )

    def fallback_notice(self, applied_sources: dict[str, str]) -> str | None:
        """Warn when a region was priced from the default rather than a verified rate.

        The bundled table cannot cover a location Google has only just opened, so
        the fallback exists and the disclosure footer already names it. What the
        footer does not say is which *direction* the guess errs in: the default is
        the lowest rate BigQuery charges anywhere, so a fallback under-reports for
        every region that is not among the cheapest — the wrong way round for a
        gate, which should rather over-state a cost than wave one through.

        Advisory only, and the remedy is entirely in the user's hands: an explicit
        rate for that location always wins over anything bundled.
        """
        fallen = sorted(r for r, src in applied_sources.items() if src == "default-fallback")
        if not fallen:
            return None
        names = ", ".join(fallen)
        subject = "those locations" if len(fallen) > 1 else "that location"
        return (
            f"No verified rate for {names} — priced at the "
            f"{self.currency} {self.default_usd_per_tib:,.2f}/TiB default, which is the lowest "
            f"rate BigQuery charges anywhere, so the cost shown may be understated. Set the rate "
            f"you actually pay: pricing.regions ({fallen[0]}: <rate>) for {subject}, "
            f"pricing.usd_per_tib for one flat rate everywhere, or pricing.region to pin pricing "
            f"to a location you have a rate for. Your value always wins over the built-in table."
        )

    def rate_for(self, region: str | None) -> RateResult:
        """Resolve the $/TiB for a region. Precedence, most-specific first:
        CLI flat override → config per-region map → config flat override →
        built-in table → default fallback. The displayed region keeps its
        original casing; only the lookup is case-insensitive."""
        effective_region = self.override_region or region or "US"
        if self.cli_override_usd_per_tib is not None:
            return RateResult(self.cli_override_usd_per_tib, effective_region, "user-override")
        mapped = _ci_get(self.override_regions, effective_region)
        if mapped is not None:
            return RateResult(mapped, effective_region, "user-override")
        if self.override_usd_per_tib is not None:
            return RateResult(self.override_usd_per_tib, effective_region, "user-override")
        table = _ci_get(self.regions, effective_region)
        if table is not None:
            return RateResult(table, effective_region, "region-table")
        return RateResult(self.default_usd_per_tib, effective_region, "default-fallback")

    def usd(self, bytes_scanned: int, region: str | None) -> tuple[float, RateResult]:
        rate = self.rate_for(region)
        return (bytes_scanned / TIB) * rate.usd_per_tib, rate

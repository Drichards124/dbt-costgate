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

    @classmethod
    def load(
        cls,
        *,
        cli_override_usd_per_tib: float | None = None,
        override_regions: dict[str, float] | None = None,
        override_usd_per_tib: float | None = None,
        override_region: str | None = None,
    ) -> PricingTable:
        raw = json.loads(
            resources.files("dbt_costgate.data").joinpath("pricing.json").read_text("utf-8")
        )
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

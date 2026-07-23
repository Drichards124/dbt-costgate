# SPDX-License-Identifier: Apache-2.0
"""Region-aware on-demand pricing.

Accuracy rule: never guess a region's rate silently. A region we don't have a
verified value for falls back to the default and the report says so, and a user
can always override with a negotiated rate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources

from costgate.models import TIB


@dataclass(frozen=True)
class RateResult:
    usd_per_tib: float
    region: str
    source: str  # "region-table" | "default-fallback" | "user-override"


@dataclass
class PricingTable:
    version: str
    last_verified: str
    source_url: str
    default_usd_per_tib: float
    regions: dict[str, float]
    override_usd_per_tib: float | None = None
    override_region: str | None = None

    @classmethod
    def load(
        cls,
        *,
        override_usd_per_tib: float | None = None,
        override_region: str | None = None,
    ) -> PricingTable:
        raw = json.loads(
            resources.files("costgate.data").joinpath("pricing.json").read_text("utf-8")
        )
        return cls(
            version=raw["version"],
            last_verified=raw["last_verified"],
            source_url=raw["source"],
            default_usd_per_tib=float(raw["default_usd_per_tib"]),
            regions={k: float(v) for k, v in raw["regions"].items()},
            override_usd_per_tib=override_usd_per_tib,
            override_region=override_region,
        )

    def rate_for(self, region: str | None) -> RateResult:
        """Resolve the $/TiB for a region, honoring an explicit override first."""
        effective_region = self.override_region or region or "US"
        if self.override_usd_per_tib is not None:
            return RateResult(self.override_usd_per_tib, effective_region, "user-override")
        if effective_region in self.regions:
            return RateResult(self.regions[effective_region], effective_region, "region-table")
        return RateResult(self.default_usd_per_tib, effective_region, "default-fallback")

    def usd(self, bytes_scanned: int, region: str | None) -> tuple[float, RateResult]:
        rate = self.rate_for(region)
        return (bytes_scanned / TIB) * rate.usd_per_tib, rate

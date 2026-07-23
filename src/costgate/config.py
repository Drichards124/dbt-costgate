# SPDX-License-Identifier: Apache-2.0
"""`.costgate.yml` loading and merge with CLI overrides (CLI always wins)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Thresholds:
    max_usd_increase_per_run: float | None = None
    max_pct_increase: float | None = None
    max_usd_increase_per_month: float | None = None

    @property
    def any_set(self) -> bool:
        return any(
            v is not None
            for v in (
                self.max_usd_increase_per_run,
                self.max_pct_increase,
                self.max_usd_increase_per_month,
            )
        )


@dataclass
class Config:
    region: str | None = None
    usd_per_tib: float | None = None
    pricing_regions: dict[str, float] = field(default_factory=dict)
    thresholds: Thresholds = field(default_factory=Thresholds)
    run_frequency_default: int | None = None
    run_frequency_models: dict[str, int] = field(default_factory=dict)
    exclude: list[str] = field(default_factory=list)
    warn_only: list[str] = field(default_factory=list)
    report_format: str = "terminal"
    fail_on: str = "fail"  # never | warn | fail

    DEFAULT_FILENAMES = (".costgate.yml", ".costgate.yaml", "costgate.yml")

    @classmethod
    def load(cls, path: Path | None, project_dir: Path) -> Config:
        """Load from an explicit path, else the first default filename found in
        the project directory. A missing file is fine — defaults apply."""
        cfg_path = path
        if cfg_path is None:
            for name in cls.DEFAULT_FILENAMES:
                candidate = project_dir / name
                if candidate.is_file():
                    cfg_path = candidate
                    break
        if cfg_path is None or not cfg_path.is_file():
            return cls()
        raw = yaml.safe_load(cfg_path.read_text("utf-8")) or {}
        return cls._from_dict(raw)

    @classmethod
    def _from_dict(cls, raw: dict) -> Config:
        pricing = raw.get("pricing") or {}
        thr = raw.get("thresholds") or {}
        freq = raw.get("run_frequency") or {}
        report = raw.get("report") or {}
        return cls(
            region=pricing.get("region"),
            usd_per_tib=_opt_float(pricing.get("usd_per_tib")),
            pricing_regions=_region_rates(pricing.get("regions")),
            thresholds=Thresholds(
                max_usd_increase_per_run=_opt_float(thr.get("max_usd_increase_per_run")),
                max_pct_increase=_opt_float(thr.get("max_pct_increase")),
                max_usd_increase_per_month=_opt_float(thr.get("max_usd_increase_per_month")),
            ),
            run_frequency_default=_opt_int(freq.get("default")),
            run_frequency_models={k: int(v) for k, v in (freq.get("models") or {}).items()},
            exclude=list(raw.get("exclude") or []),
            warn_only=list(raw.get("warn_only") or []),
            report_format=report.get("format", "terminal"),
            fail_on=raw.get("fail_on", "fail"),
        )

    def runs_per_month(self, model_name: str) -> int | None:
        return self.run_frequency_models.get(model_name, self.run_frequency_default)


def _region_rates(raw) -> dict[str, float]:
    """Parse a `pricing.regions` map. A rate may be 0 (flat-rate slots), but a
    negative rate is nonsense and would silently subtract cost."""
    rates: dict[str, float] = {}
    for region, value in (raw or {}).items():
        rate = float(value)
        if rate < 0:
            raise ValueError(f"pricing.regions[{region!r}]: rate must be >= 0, got {rate}")
        rates[region] = rate
    return rates


def _opt_float(v) -> float | None:
    return None if v is None else float(v)


def _opt_int(v) -> int | None:
    return None if v is None else int(v)

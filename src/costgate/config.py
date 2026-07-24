# SPDX-License-Identifier: Apache-2.0
"""`.costgate.yml` loading and merge with CLI overrides (CLI always wins)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


@dataclass(frozen=True)
class ConfigField:
    """One documented `.costgate.yml` key. The single source of truth behind the
    `costgate config` command; `attr` ties it to the Config attribute it fills so
    a test can prove the registry and the parser never drift apart."""

    key: str  # dotted YAML path, e.g. "pricing.regions"
    attr: str  # dotted Config attribute it populates (for the drift test)
    type_label: str  # human type hint, e.g. "map[str->float]"
    default: Any  # the literal parser default (native value, for a clean JSON contract)
    help: str  # plain-English explanation; notes the *effective* default when it differs


# Every key `Config._from_dict` understands, described once. Keep this in lockstep
# with the dataclass — `tests/test_config.py` enforces it in both directions.
CONFIG_REFERENCE: list[ConfigField] = [
    ConfigField(
        "pricing.region",
        "region",
        "str",
        None,
        "Force the pricing region. Default: auto-detected from the dry-run job "
        "location, falling back to US.",
    ),
    ConfigField(
        "pricing.usd_per_tib",
        "usd_per_tib",
        "float",
        None,
        "Flat on-demand rate override (USD/TiB) for every region. Default: the "
        "built-in per-region rate table.",
    ),
    ConfigField(
        "pricing.regions",
        "pricing_regions",
        "map[str->float]",
        {},
        "Per-region rate overrides (region -> USD/TiB) that patch the built-in "
        "table. Keys match case-insensitively; 0 is allowed. Unlisted regions "
        "use the table.",
    ),
    ConfigField(
        "thresholds.max_usd_increase_per_run",
        "thresholds.max_usd_increase_per_run",
        "float",
        None,
        "Gate fails if a model's per-run cost increase exceeds this many USD.",
    ),
    ConfigField(
        "thresholds.max_pct_increase",
        "thresholds.max_pct_increase",
        "float",
        None,
        "Gate fails if a model's cost increases by more than this percent.",
    ),
    ConfigField(
        "thresholds.max_usd_increase_per_month",
        "thresholds.max_usd_increase_per_month",
        "float",
        None,
        "Gate fails if a model's projected monthly cost increase exceeds this many USD.",
    ),
    ConfigField(
        "run_frequency.default",
        "run_frequency_default",
        "int",
        None,
        "Assumed runs per month for the monthly-cost estimate, for models "
        "without an explicit entry.",
    ),
    ConfigField(
        "run_frequency.models",
        "run_frequency_models",
        "map[str->int]",
        {},
        "Per-model runs-per-month overrides (model name -> runs) for the monthly estimate.",
    ),
    ConfigField(
        "exclude",
        "exclude",
        "list[str]",
        [],
        "Model names reported but never gated.",
    ),
    ConfigField(
        "warn_only",
        "warn_only",
        "list[str]",
        [],
        "Model names shown as a warning instead of gated.",
    ),
    ConfigField(
        "report.format",
        "report_format",
        "terminal|markdown|json",
        "terminal",
        "Output format when not overridden by --format.",
    ),
    ConfigField(
        "fail_on",
        "fail_on",
        "never|warn|fail",
        "fail",
        "Gate strictness: 'never' never fails the build, 'warn' fails on "
        "warnings, 'fail' fails only on threshold breaches.",
    ),
]

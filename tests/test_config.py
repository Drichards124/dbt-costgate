# SPDX-License-Identifier: Apache-2.0
from dataclasses import fields
from pathlib import Path

import pytest

from dbt_costgate.config import CONFIG_REFERENCE, Config, Thresholds


def test_load_missing_file_returns_defaults(tmp_path: Path):
    cfg = Config.load(None, tmp_path)
    assert cfg.fail_on == "fail"
    assert cfg.report_format == "terminal"
    assert not cfg.thresholds.any_set


def test_load_full_config(tmp_path: Path):
    (tmp_path / ".dbt-costgate.yml").write_text(
        """
pricing:
  region: europe-west3
  usd_per_tib: 5.0
thresholds:
  max_usd_increase_per_run: 5.0
  max_pct_increase: 25
  max_usd_total: 40.0
  max_tib_total: 6.5
run_frequency:
  default: 30
  models:
    fct_orders_daily: 60
exclude:
  - events
warn_only:
  - sessions
renames:
  fct_orders_daily: fct_orders_monthly
baselines:
  main:
    against: main
  ple:
    manifest: artifacts/ple/manifest.json
default_baseline: main
report:
  format: markdown
fail_on: warn
""",
        "utf-8",
    )
    cfg = Config.load(None, tmp_path)
    assert cfg.region == "europe-west3"
    assert cfg.usd_per_tib == 5.0
    assert cfg.thresholds.max_usd_increase_per_run == 5.0
    assert cfg.thresholds.max_usd_total == 40.0
    assert cfg.thresholds.max_tib_total == 6.5
    assert cfg.thresholds.any_set
    assert cfg.renames == {"fct_orders_daily": "fct_orders_monthly"}
    assert cfg.baselines["main"].against == "main"
    assert cfg.baselines["ple"].manifest == "artifacts/ple/manifest.json"
    assert cfg.default_baseline == "main"
    assert cfg.runs_per_month("fct_orders_daily") == 60
    assert cfg.runs_per_month("other") == 30
    assert cfg.exclude == ["events"]
    assert cfg.warn_only == ["sessions"]
    assert cfg.report_format == "markdown"
    assert cfg.fail_on == "warn"


def test_pricing_regions_parses_to_floats(tmp_path: Path):
    (tmp_path / ".dbt-costgate.yml").write_text(
        """
pricing:
  regions:
    europe-west3: 4.80
    US: 6
    asia-northeast1: 0.0
""",
        "utf-8",
    )
    cfg = Config.load(None, tmp_path)
    assert cfg.pricing_regions == {"europe-west3": 4.80, "US": 6.0, "asia-northeast1": 0.0}


def test_pricing_regions_rejects_negative_rate(tmp_path: Path):
    (tmp_path / ".dbt-costgate.yml").write_text(
        """
pricing:
  regions:
    europe-west3: -1.0
""",
        "utf-8",
    )
    with pytest.raises(ValueError, match="europe-west3"):
        Config.load(None, tmp_path)


# --- config reference registry (drift guards) ---


def _config_attr_paths() -> set[str]:
    """Every settable Config attribute, expanding the nested Thresholds container."""
    paths: set[str] = set()
    for f in fields(Config):
        if f.name == "thresholds":
            paths.update(f"thresholds.{sf.name}" for sf in fields(Thresholds))
        else:
            paths.add(f.name)
    return paths


def _get(obj, dotted: str):
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def test_registry_keys_unique_and_nonempty():
    keys = [cf.key for cf in CONFIG_REFERENCE]
    assert keys
    assert len(keys) == len(set(keys))


def test_registry_covers_every_config_field_bidirectionally():
    # equality, not subset: a new Config field with no registry entry (or a stray
    # registry entry) breaks this in one direction or the other.
    assert {cf.attr for cf in CONFIG_REFERENCE} == _config_attr_paths()


def test_every_documented_key_is_honored_by_the_parser(tmp_path: Path):
    # a config that sets every documented key to a non-default value
    (tmp_path / ".dbt-costgate.yml").write_text(
        """
pricing:
  region: europe-west3
  usd_per_tib: 5.0
  regions:
    US: 6.0
thresholds:
  max_usd_increase_per_run: 5.0
  max_pct_increase: 25
  max_usd_increase_per_month: 100.0
  max_usd_total: 40.0
  max_tib_total: 6.5
run_frequency:
  default: 30
  models:
    fct_orders_daily: 60
exclude:
  - events
warn_only:
  - sessions
renames:
  fct_orders_daily: fct_orders_monthly
baselines:
  main:
    against: main
default_baseline: main
report:
  format: markdown
fail_on: warn
""",
        "utf-8",
    )
    cfg = Config.load(None, tmp_path)
    for entry in CONFIG_REFERENCE:
        # each documented key's value actually landed — proving key ↔ parser match
        assert _get(cfg, entry.attr) != entry.default, entry.key

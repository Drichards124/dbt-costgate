# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

from costgate.config import Config


def test_load_missing_file_returns_defaults(tmp_path: Path):
    cfg = Config.load(None, tmp_path)
    assert cfg.fail_on == "fail"
    assert cfg.report_format == "terminal"
    assert not cfg.thresholds.any_set


def test_load_full_config(tmp_path: Path):
    (tmp_path / ".costgate.yml").write_text(
        """
pricing:
  region: europe-west3
  usd_per_tib: 5.0
thresholds:
  max_usd_increase_per_run: 5.0
  max_pct_increase: 25
run_frequency:
  default: 30
  models:
    fct_orders_daily: 60
exclude:
  - events
warn_only:
  - sessions
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
    assert cfg.thresholds.any_set
    assert cfg.runs_per_month("fct_orders_daily") == 60
    assert cfg.runs_per_month("other") == 30
    assert cfg.exclude == ["events"]
    assert cfg.warn_only == ["sessions"]
    assert cfg.report_format == "markdown"
    assert cfg.fail_on == "warn"

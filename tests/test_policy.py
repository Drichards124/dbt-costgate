# SPDX-License-Identifier: Apache-2.0
from costgate import policy
from costgate.config import Config, Thresholds
from costgate.models import CostDelta, Status


def _delta(name="m", usd_baseline=1.0, usd_current=10.0, gateable=True, runs=30):
    return CostDelta(
        name=name,
        unique_id=f"model.pkg.{name}",
        is_incremental=False,
        is_new=False,
        gateable=gateable,
        bytes_baseline=1,
        bytes_current=10,
        usd_baseline=usd_baseline,
        usd_current=usd_current,
        region="US",
        runs_per_month=runs,
    )


def test_pass_when_under_threshold():
    cfg = Config(thresholds=Thresholds(max_usd_increase_per_run=100.0))
    v = policy.evaluate([_delta()], cfg)
    assert v.status == Status.PASS
    assert v.exit_code == 0


def test_fail_when_usd_per_run_exceeded():
    cfg = Config(thresholds=Thresholds(max_usd_increase_per_run=5.0))
    v = policy.evaluate([_delta()], cfg)
    assert v.status == Status.FAIL
    assert v.exit_code == 1
    assert v.breaches


def test_fail_on_never_downgrades_to_warn_exit_zero():
    cfg = Config(thresholds=Thresholds(max_usd_increase_per_run=5.0), fail_on="never")
    v = policy.evaluate([_delta()], cfg)
    assert v.status == Status.WARN
    assert v.exit_code == 0


def test_fail_on_warn_reports_warn_but_exits_nonzero():
    cfg = Config(thresholds=Thresholds(max_usd_increase_per_run=5.0), fail_on="warn")
    v = policy.evaluate([_delta()], cfg)
    assert v.status == Status.WARN
    assert v.exit_code == 1


def test_non_gateable_delta_never_breaches():
    cfg = Config(thresholds=Thresholds(max_usd_increase_per_run=0.01))
    v = policy.evaluate([_delta(gateable=False)], cfg)
    assert v.status == Status.PASS


def test_pct_threshold():
    cfg = Config(thresholds=Thresholds(max_pct_increase=50.0))
    # 1.0 -> 10.0 is +900%
    v = policy.evaluate([_delta()], cfg)
    assert v.status == Status.FAIL


def test_monthly_threshold():
    cfg = Config(thresholds=Thresholds(max_usd_increase_per_month=100.0))
    # (10-1)/run * 30 = 270/month > 100
    v = policy.evaluate([_delta()], cfg)
    assert v.status == Status.FAIL

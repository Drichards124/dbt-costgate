# SPDX-License-Identifier: Apache-2.0
from costgate import policy
from costgate.config import Config, Thresholds
from costgate.models import TIB, CostDelta, Status


def _delta(name="m", usd_baseline=1.0, usd_current=10.0, gateable=True, runs=30, bytes_current=10):
    return CostDelta(
        name=name,
        unique_id=f"model.pkg.{name}",
        is_incremental=False,
        is_new=False,
        gateable=gateable,
        bytes_baseline=1,
        bytes_current=bytes_current,
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


# --- absolute per-run ceilings (max_usd_total / max_tib_total) ---


def test_absolute_usd_total_breaches_in_local_mode():
    # no baseline (local mode): the cap gates usd_current directly.
    cfg = Config(thresholds=Thresholds(max_usd_total=5.0))
    v = policy.evaluate([_delta(usd_baseline=None, usd_current=10.0)], cfg)
    assert v.status == Status.FAIL
    assert v.exit_code == 1
    assert v.breaches


def test_absolute_tib_total_breaches():
    cfg = Config(thresholds=Thresholds(max_tib_total=1.0))
    v = policy.evaluate([_delta(bytes_current=2 * TIB)], cfg)
    assert v.status == Status.FAIL
    assert "TiB" in v.breaches[0]


def test_absolute_caps_pass_under_cap():
    cfg = Config(thresholds=Thresholds(max_usd_total=100.0, max_tib_total=100.0))
    v = policy.evaluate([_delta(bytes_current=TIB)], cfg)
    assert v.status == Status.PASS


def test_absolute_cap_skips_non_gateable():
    cfg = Config(thresholds=Thresholds(max_usd_total=0.01, max_tib_total=0.01))
    v = policy.evaluate([_delta(gateable=False, bytes_current=TIB)], cfg)
    assert v.status == Status.PASS


def test_absolute_usd_total_catches_big_but_unchanged_model():
    # The capability delta thresholds lack: a huge model that barely changed
    # passes max_usd_increase_per_run yet must fail an absolute cap.
    cfg = Config(
        thresholds=Thresholds(max_usd_increase_per_run=5.0, max_usd_total=8.0),
    )
    barely_changed_but_huge = _delta(usd_baseline=9.99, usd_current=10.0)
    v = policy.evaluate([barely_changed_but_huge], cfg)
    assert v.status == Status.FAIL
    # the breach is the absolute cap, not the (tiny) per-run increase
    assert any("exceeds cap" in b for b in v.breaches)


def test_zero_scan_model_passes_zero_tolerance_caps():
    # A view/ephemeral scanning 0 bytes passes even a 0.0 cap (0.0 > 0.0 is False).
    cfg = Config(thresholds=Thresholds(max_usd_total=0.0, max_tib_total=0.0))
    v = policy.evaluate([_delta(usd_baseline=None, usd_current=0.0, bytes_current=0)], cfg)
    assert v.status == Status.PASS


def test_zero_tolerance_cap_breaches_any_nonzero_scan():
    cfg = Config(thresholds=Thresholds(max_tib_total=0.0))
    v = policy.evaluate([_delta(bytes_current=1)], cfg)
    assert v.status == Status.FAIL

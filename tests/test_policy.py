# SPDX-License-Identifier: Apache-2.0
from dbt_costgate import policy
from dbt_costgate.config import Config, Thresholds
from dbt_costgate.models import TIB, CostDelta, SkipReason, Status


def _delta(
    name="m",
    usd_baseline=1.0,
    usd_current=10.0,
    gateable=True,
    runs=30,
    bytes_current=10,
    bytes_baseline=1,
):
    return CostDelta(
        name=name,
        unique_id=f"model.pkg.{name}",
        is_incremental=False,
        is_new=False,
        gateable=gateable,
        bytes_baseline=bytes_baseline,
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


def test_pct_threshold_still_fires_at_a_zero_rate():
    """A percentage has no currency, so a zero rate must not disable it.

    `pricing.usd_per_tib: 0.00` is documented for capacity/flat-rate slots. It
    makes every dollar figure 0.00, which correctly neutralises the three USD
    thresholds — but it used to take the percentage threshold down with them,
    leaving `max_tib_total` as the only working gate.
    """
    slot_priced = _delta(
        bytes_baseline=1 * TIB,
        bytes_current=10 * TIB,  # +900% scan growth
        usd_baseline=0.0,
        usd_current=0.0,  # rate is 0 -> no dollars anywhere
    )
    assert slot_priced.pct_delta == 900.0
    v = policy.evaluate([slot_priced], Config(thresholds=Thresholds(max_pct_increase=25.0)))
    assert v.status == Status.FAIL
    assert v.exit_code == 1
    assert "+900%" in v.breaches[0]


def test_pct_delta_does_not_depend_on_the_rate():
    """Both sides are priced at one regional rate, so the rate cancels."""
    same_bytes = dict(bytes_baseline=2 * TIB, bytes_current=3 * TIB)  # +50%
    at_list_price = _delta(**same_bytes, usd_baseline=12.5, usd_current=18.75)
    at_negotiated = _delta(**same_bytes, usd_baseline=4.0, usd_current=6.0)
    at_zero = _delta(**same_bytes, usd_baseline=0.0, usd_current=0.0)
    assert at_list_price.pct_delta == 50.0
    assert at_negotiated.pct_delta == 50.0
    assert at_zero.pct_delta == 50.0


def test_pct_delta_is_none_without_a_baseline_scan():
    """No baseline bytes means no ratio to take — a new model, not a 0% change."""
    assert _delta(bytes_baseline=0, bytes_current=5 * TIB).pct_delta is None
    assert _delta(bytes_baseline=None, bytes_current=5 * TIB).pct_delta is None
    assert _delta(bytes_baseline=1 * TIB, bytes_current=None).pct_delta is None


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


def test_a_small_tib_cap_breach_names_two_numbers_a_reader_can_compare():
    # A 1 GiB ceiling is an ordinary thing to set, and both sides used to be
    # hand-formatted as `{tib:,.2f} TiB` — so a real breach printed
    # "0.00 TiB/run exceeds cap 0.00 TiB", comparing a number to itself. Found by
    # running the CLI against real BigQuery, where models this size are normal.
    cfg = Config(thresholds=Thresholds(max_tib_total=1 / 1024))  # 1 GiB
    v = policy.evaluate([_delta(bytes_current=3 * 1024**3)], cfg)  # 3 GiB
    assert v.status == Status.FAIL
    assert v.breaches[0] == "m: 3.00 GiB/run exceeds cap 1.00 GiB"


def test_a_sub_cent_usd_cap_breach_widens_until_both_amounts_are_real():
    # Two decimals gives "USD 0.00 exceeds cap USD 0.00"; stopping at the first
    # precision where the strings merely differ gives "USD 0.001 exceeds cap
    # USD 0.000", which is distinguishable and still reads as a cap of nothing.
    cfg = Config(thresholds=Thresholds(max_usd_total=0.0001))
    v = policy.evaluate([_delta(usd_baseline=None, usd_current=0.00094)], cfg)
    assert v.breaches[0] == "m: USD 0.0009/run exceeds cap USD 0.0001"


def test_an_ordinary_cap_breach_still_reads_at_two_places():
    # The widening is for the case that needs it and nowhere else.
    cfg = Config(thresholds=Thresholds(max_usd_total=0.50))
    v = policy.evaluate([_delta(usd_baseline=None, usd_current=0.84)], cfg)
    assert v.breaches[0] == "m: USD 0.84/run exceeds cap USD 0.50"


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


def test_breach_messages_use_the_shared_money_formatter():
    """Guards the drift that happened twice: policy.py had its own copy of the
    money format string, so the sign moved onto the number in the report and not
    in the breach message. Asserting against `format_money` itself means the two
    cannot disagree again — whichever side changes, this fails."""
    from dbt_costgate.models import format_money

    d = _delta(bytes_baseline=1 * TIB, bytes_current=9 * TIB, usd_baseline=6.25, usd_current=56.25)
    cfg = Config(thresholds=Thresholds(max_usd_increase_per_run=10.0))
    breach = policy.evaluate([d], cfg, currency="USD").breaches[0]

    assert format_money(50.0, "USD", signed=True) in breach  # the delta, signed
    assert format_money(10.0, "USD") in breach  # the threshold, unsigned
    assert "+USD" not in breach  # the old, wrong placement


def test_breach_messages_carry_the_configured_currency():
    d = _delta(bytes_baseline=1 * TIB, bytes_current=9 * TIB, usd_baseline=6.25, usd_current=56.25)
    cfg = Config(thresholds=Thresholds(max_usd_increase_per_run=10.0))
    breach = policy.evaluate([d], cfg, currency="EUR").breaches[0]
    assert "EUR +50.00/run exceeds EUR 10.00" in breach


# --- advisory notices -------------------------------------------------------


def test_dollar_threshold_at_zero_rate_is_reported_as_dead():
    thr = Thresholds(max_usd_increase_per_run=5.0, max_pct_increase=25.0)
    notice = policy.unpriced_threshold_notice(thr, priced=False)
    assert notice is not None
    assert "thresholds.max_usd_increase_per_run" in notice
    # It must not name the thresholds that still work — that is the whole advice.
    assert "thresholds.max_pct_increase cannot" not in notice
    assert "max_tib_total" in notice


def test_every_dead_money_threshold_is_named():
    thr = Thresholds(
        max_usd_increase_per_run=5.0,
        max_usd_increase_per_month=100.0,
        max_usd_total=20.0,
    )
    notice = policy.unpriced_threshold_notice(thr, priced=False)
    for key in ("max_usd_increase_per_run", "max_usd_increase_per_month", "max_usd_total"):
        assert f"thresholds.{key}" in notice


def test_no_notice_when_priced_or_when_no_money_threshold_is_set():
    money = Thresholds(max_usd_increase_per_run=5.0)
    bytes_only = Thresholds(max_pct_increase=25.0, max_tib_total=3.0)
    assert policy.unpriced_threshold_notice(money, priced=True) is None
    assert policy.unpriced_threshold_notice(bytes_only, priced=False) is None
    assert policy.unpriced_threshold_notice(Thresholds(), priced=False) is None


def test_zero_dollar_cap_is_still_dead_at_a_zero_rate():
    """A `max_usd_total: 0` zero-tolerance cap looks strictest of all, and is the
    one most likely to be trusted — but `0.00 > 0` is false, so it never fires."""
    assert policy.unpriced_threshold_notice(Thresholds(max_usd_total=0.0), priced=False) is not None


def test_notice_never_changes_the_verdict():
    """Advisory means advisory: the same deltas and config must produce the same
    verdict and exit code whether or not a notice is warranted."""
    cfg = Config(thresholds=Thresholds(max_usd_increase_per_run=5.0, max_pct_increase=25.0))
    unpriced = _delta(usd_baseline=0.0, usd_current=0.0, bytes_baseline=TIB, bytes_current=4 * TIB)
    v = policy.evaluate([unpriced], cfg)
    assert policy.unpriced_threshold_notice(cfg.thresholds, priced=False) is not None
    # The percentage threshold still fires; the dead dollar one contributes nothing.
    assert v.status == Status.FAIL
    assert v.exit_code == policy.EXIT_GATE_FAILED
    assert v.breaches == ["m: +300% exceeds 25%"]


def test_the_run_level_breach_agrees_with_its_own_count():
    """Every skip reason is used in two frames — after one model's name, and
    after a count of them. A reason written as "its dry-run…" reads correctly in
    the first and disagrees with the plural in the second, which is how the gate
    came to report "none of the 2 selected models could be gated — its dry-run
    did not return a size"."""
    cfg = Config(thresholds=Thresholds(max_pct_increase=25.0))
    for reason in SkipReason:
        if not reason.is_unchecked:
            continue
        deltas = [
            _delta(name=n, gateable=False, bytes_current=None, usd_current=None) for n in ("a", "b")
        ]
        for d in deltas:
            object.__setattr__(d, "skip_reason", reason)
        (breach,) = [b for b in policy.evaluate(deltas, cfg).breaches if "nothing" in b]
        assert "2 selected models" in breach
        assert " its " not in f" {breach} ", breach

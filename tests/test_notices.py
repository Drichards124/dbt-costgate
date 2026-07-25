# SPDX-License-Identifier: Apache-2.0
"""The advisory-notice registry: ids, silencing, and the advisory tail."""

from dbt_costgate import notices
from dbt_costgate.config import Config, Thresholds
from dbt_costgate.models import PricingDisclosure
from dbt_costgate.pricing import PricingTable


def _disclosure(rate=0.0, source="user-override", region="US"):
    return PricingDisclosure(
        regions={region: rate},
        source="user override",
        table_version="2026.07",
        last_verified="2026-07-25",
        region_sources={region: source},
    )


def _config(**kw):
    kw.setdefault("thresholds", Thresholds(max_usd_increase_per_run=5.0))
    return Config(**kw)


def test_ids_are_unique_and_every_check_is_reachable():
    assert len(notices.NOTICE_IDS) == len(set(notices.NOTICE_IDS))
    assert set(notices.NOTICE_IDS) == {"dead-money-thresholds", "unverified-region-rate"}


def test_collect_produces_the_notice_that_applies():
    got = notices.collect(_config(), PricingTable.load(), _disclosure())
    assert [n.id for n in got] == ["dead-money-thresholds"]


def test_silencing_removes_only_the_named_notice():
    """The point of per-notice ids: turning one off must not hide another. A
    blanket off-switch would also suppress notices the user has never seen."""
    table = PricingTable.load()
    # A run that warrants both: no per-byte price *and* an unpriced region... the
    # two cannot co-occur in practice, so check each independently instead.
    dead = notices.collect(
        _config(silence_notices=["unverified-region-rate"]), table, _disclosure()
    )
    assert [n.id for n in dead] == ["dead-money-thresholds"]

    fallback_disclosure = _disclosure(rate=6.25, source="default-fallback", region="mars-central1")
    fallback = notices.collect(
        _config(silence_notices=["dead-money-thresholds"]), table, fallback_disclosure
    )
    assert [n.id for n in fallback] == ["unverified-region-rate"]


def test_silencing_a_notice_actually_silences_it():
    got = notices.collect(
        _config(silence_notices=["dead-money-thresholds"]), PricingTable.load(), _disclosure()
    )
    assert got == []


def test_there_is_no_blanket_off_switch():
    """`silence: [all]` must not work by accident — it names nothing, so it is
    rejected as an unknown id rather than quietly silencing everything."""
    assert notices.unknown_ids(["all"]) == ["all"]
    assert notices.unknown_ids(["*"]) == ["*"]


def test_unknown_ids_are_reported_not_ignored():
    assert notices.unknown_ids(["dead-money-threshold"]) == ["dead-money-threshold"]  # typo
    assert notices.unknown_ids(list(notices.NOTICE_IDS)) == []
    assert notices.unknown_ids([]) == []


def test_every_notice_ends_by_naming_its_own_silencing_id():
    """The tail is composed from the registry id, not written into each check, so
    a notice can never tell a user to silence an id that is not its own."""
    table = PricingTable.load()
    cases = [
        (_config(), _disclosure()),
        (
            _config(thresholds=Thresholds()),
            _disclosure(rate=6.25, source="default-fallback", region="mars-central1"),
        ),
    ]
    produced = [n for config, d in cases for n in notices.collect(config, table, d)]
    assert {n.id for n in produced} == set(notices.NOTICE_IDS)  # every check exercised
    for n in produced:
        assert n.message.endswith(f"Silence it with notices.silence: [{n.id}].")
        assert "does not affect the gate or the exit code" in n.message

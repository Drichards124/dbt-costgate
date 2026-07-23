# SPDX-License-Identifier: Apache-2.0
from costgate.models import TIB
from costgate.pricing import PricingTable


def test_known_region_uses_table_rate():
    table = PricingTable.load()
    rate = table.rate_for("US")
    assert rate.source == "region-table"
    assert rate.usd_per_tib == 6.25


def test_unknown_region_falls_back_and_discloses():
    table = PricingTable.load()
    rate = table.rate_for("mars-central1")
    assert rate.source == "default-fallback"
    assert rate.usd_per_tib == table.default_usd_per_tib


def test_user_override_wins_over_table():
    table = PricingTable.load(override_usd_per_tib=2.0, override_region="EU")
    rate = table.rate_for("US")
    assert rate.source == "user-override"
    assert rate.usd_per_tib == 2.0
    assert rate.region == "EU"


def test_usd_math_is_bytes_over_tib_times_rate():
    table = PricingTable.load()
    usd, rate = table.usd(TIB, "US")
    assert usd == 6.25
    half, _ = table.usd(TIB // 2, "US")
    assert round(half, 4) == 3.125

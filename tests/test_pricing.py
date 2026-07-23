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


def test_config_region_map_wins_and_discloses_override():
    table = PricingTable.load(override_regions={"europe-west3": 4.80})
    rate = table.rate_for("europe-west3")
    assert rate.usd_per_tib == 4.80
    assert rate.source == "user-override"
    assert rate.region == "europe-west3"


def test_cli_flat_override_beats_region_map():
    table = PricingTable.load(cli_override_usd_per_tib=9.0, override_regions={"EU": 3.0})
    rate = table.rate_for("EU")
    assert rate.usd_per_tib == 9.0
    assert rate.source == "user-override"


def test_region_map_beats_config_global_and_falls_through_for_others():
    table = PricingTable.load(override_regions={"EU": 3.0}, override_usd_per_tib=7.0)
    assert table.rate_for("EU").usd_per_tib == 3.0  # map wins for its region
    other = table.rate_for("US")  # not in map -> config global flat
    assert other.usd_per_tib == 7.0
    assert other.source == "user-override"


def test_region_absent_from_map_falls_through_table_then_default():
    table = PricingTable.load(override_regions={"EU": 3.0})
    assert table.rate_for("US").source == "region-table"  # built-in table
    assert table.rate_for("mars-central1").source == "default-fallback"


def test_region_matching_is_case_insensitive():
    # config key lower-case, BigQuery reports the regional location upper-cased
    table = PricingTable.load(override_regions={"europe-west3": 4.80})
    mapped = table.rate_for("EUROPE-WEST3")
    assert mapped.usd_per_tib == 4.80
    assert mapped.source == "user-override"
    assert mapped.region == "EUROPE-WEST3"  # display keeps the incoming casing
    # built-in multi-region table is also matched case-insensitively
    builtin = table.rate_for("us")
    assert builtin.usd_per_tib == 6.25
    assert builtin.source == "region-table"


def test_zero_rate_is_valid_override():
    table = PricingTable.load(override_regions={"US": 0.0})
    rate = table.rate_for("US")
    assert rate.usd_per_tib == 0.0
    assert rate.source == "user-override"

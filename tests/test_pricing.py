# SPDX-License-Identifier: Apache-2.0
from dbt_costgate.models import TIB
from dbt_costgate.pricing import PricingTable


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


def test_regional_locations_resolve_from_the_table_not_the_default():
    """Regional (non-multi-region) locations carry their own published rate.

    Before the table listed them these all fell through to `default-fallback` at
    the US rate, which under-reported cost for every team outside US/EU.
    """
    table = PricingTable.load()
    for region, expected in [
        ("europe-west3", 8.125),  # Frankfurt
        ("asia-northeast1", 7.50),  # Tokyo
        ("southamerica-east1", 11.25),  # Sao Paulo — most expensive
        ("us-south1", 7.50),  # Dallas — a US region that is NOT 6.25
        ("asia-southeast4", 6.5625),  # Kuala Lumpur
    ]:
        rate = table.rate_for(region)
        assert rate.source == "region-table", f"{region} should come from the table"
        assert rate.usd_per_tib == expected, region


def test_multi_region_rates_are_unchanged():
    """The two rates that existed before the table was extended still hold."""
    table = PricingTable.load()
    for region in ("US", "EU"):
        rate = table.rate_for(region)
        assert rate.source == "region-table"
        assert rate.usd_per_tib == 6.25


def test_regional_location_lookup_is_case_insensitive():
    # BigQuery reports multi-regions upper-cased; a regional id may arrive either way
    table = PricingTable.load()
    assert table.rate_for("EUROPE-WEST3").usd_per_tib == 8.125
    assert table.rate_for("europe-west3").usd_per_tib == 8.125


def test_every_builtin_rate_is_in_a_plausible_band():
    """Guards against a decimal-point slip in a future one-line rate PR.

    A user override may legitimately be 0 (flat-rate slots), but a *published*
    on-demand rate never is, and none is anywhere near 0.625 or 62.5.
    """
    table = PricingTable.load()
    assert table.regions, "the built-in table must not be empty"
    for region, rate in table.regions.items():
        assert 1.0 <= rate <= 100.0, f"{region}={rate} is outside the plausible band"


def test_currency_defaults_to_the_bundled_tables_own_code():
    table = PricingTable.load()
    assert table.currency == "USD"
    assert table.table_currency == "USD"
    assert table.currency_is_sound({"US": "region-table"}) is None


def test_non_default_currency_is_refused_while_any_rate_is_the_bundled_one():
    """The bundled table is USD. Relabelling it EUR would be silently wrong."""
    table = PricingTable.load(currency="EUR")
    problem = table.currency_is_sound({"US": "region-table", "europe-west3": "user-override"})
    assert problem is not None
    assert "US" in problem and "EUR" in problem
    assert "does not convert" in problem
    # the region that IS user-supplied must not be blamed
    assert "europe-west3" not in problem


def test_non_default_currency_is_fine_once_every_rate_is_user_supplied():
    table = PricingTable.load(currency="EUR")
    assert table.currency_is_sound({"US": "user-override", "EU": "user-override"}) is None


def test_default_fallback_also_counts_as_a_bundled_rate():
    # the fallback is a USD number too, so it is not ours to relabel
    table = PricingTable.load(currency="GBP")
    assert table.currency_is_sound({"mars-central1": "default-fallback"}) is not None


def test_currency_is_normalised_to_upper_case():
    assert PricingTable.load(currency="eur").currency == "EUR"

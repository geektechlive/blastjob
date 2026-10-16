"""Round 2 D9: a country-level region ("US", "United States") is unknown, not a
conflicting state, so the same city still matches across sources."""

from __future__ import annotations

import pytest

import tests.geo_fixture as geo_fixture
from blastjob.core import role_clusters as rc
from blastjob.models.config import BlastJobConfig

GEO = geo_fixture.region_cfg()
RADIUS = BlastJobConfig().discovery.commute_radius_miles


def _place(city: str, region: str) -> rc.Place:
    row = {
        "geo_city": city,
        "geo_region": region,
        "geo_location_raw": f"{city}, {region}",
        "geo_remote": 0,
    }
    return rc.place_of(row, GEO)


@pytest.mark.parametrize("country", ["US", "USA", "United States", "united states"])
def test_country_level_region_is_unknown(country):
    assert _place("Oakbrook", country).region == ""


def test_same_city_matches_across_a_country_level_and_a_state_region():
    a = _place("Zzyzx Falls", "United States")
    b = _place("Zzyzx Falls", "WI")
    assert rc.same_metro(a, b, RADIUS, loose=False)


def test_same_city_name_in_two_different_states_still_differs():
    a = _place("Zzyzx Falls", "IL")
    b = _place("Zzyzx Falls", "MO")
    assert not rc.same_metro(a, b, RADIUS, loose=False)

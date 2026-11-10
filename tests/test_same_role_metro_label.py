"""#117: the same-role metro evidence names each place, even when a row has no
geo_city (a manual row placed from its location text, or a search-firm row
placed by the place backfill). A pair matched on location must never be shown
as "same metro: ? / ?"."""

from __future__ import annotations

import tests.geo_fixture as geo_fixture
from blastjob.core import same_role as sr

GEO = geo_fixture.region_cfg()


def _row(*, city: str, raw: str, region: str = "") -> dict[str, object]:
    return {
        "geo_city": city,
        "geo_region": region,
        "geo_location_raw": raw,
        "geo_remote": 0,
        "title": "Chief Information Officer",
        "first_seen": "2026-11-09T07:00:00",
    }


def test_metro_evidence_falls_back_to_the_location_text() -> None:
    employer = _row(city="", raw="Fairhaven, OH")
    agency = _row(city="", raw="West Fairhaven", region="OH")
    assert sr._evidence(employer, agency, GEO)[1] == "same metro: Fairhaven, OH / West Fairhaven"


def test_metro_evidence_uses_the_primary_location_of_a_multi_location_row() -> None:
    a = _row(city="", raw="Fairhaven, OH; Remote")
    b = _row(city="Fairhaven", raw="Fairhaven, OH")
    assert sr._evidence(a, b, GEO)[1] == "same metro: Fairhaven, OH / Fairhaven"


def test_metro_evidence_still_says_unknown_when_nothing_is_known() -> None:
    a = _row(city="", raw="")
    b = _row(city="Fairhaven", raw="Fairhaven, OH")
    assert sr._evidence(a, b, GEO)[1] == "same metro: ? / Fairhaven"

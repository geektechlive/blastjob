"""Place inference for search-firm postings: earliest city wins, page text fills gaps (#113)."""

from __future__ import annotations

import pytest

from blastjob.discovery import searchfirms as sf
from blastjob.models.config import SearchFirm

FIRM = SearchFirm(name="Acme Executive Search", slug="acmesearch", domain="acmesearch.example")
_POSTING = {
    "title": "Chief Technology Officer",
    "url": "https://www.acmesearch.example/opportunities/2001/chief-technology-officer",
    "description": "A retained search for a technology leader.",
}


def test_infer_geo_prefers_a_city_over_an_earlier_state():
    loc, region, _remote = sf.infer_geo(
        "Chief Officer",
        "Ohio based. The role sits in Fairhaven.",
        places=["Ohio", "Fairhaven"],
        home_state="oh",
    )
    assert (loc, region) == ("Fairhaven", "OH")


def test_infer_geo_takes_the_earliest_city_not_the_first_configured():
    loc, _region, _remote = sf.infer_geo(
        "Chief Officer",
        "Offices in Rivertown and Fairhaven.",
        places=["Fairhaven", "Rivertown"],
        home_state="oh",
    )
    assert loc == "Rivertown"


def test_infer_geo_falls_back_to_the_state_when_no_city_appears():
    loc, region, _remote = sf.infer_geo(
        "Chief Officer", "Somewhere in Ohio.", places=["Fairhaven", "Ohio"], home_state="oh"
    )
    assert (loc, region) == ("Ohio", "OH")


def _adapter(page_text: str, snippet: str = _POSTING["description"]) -> sf.SearchFirmAdapter:
    async def fake_search(query: str, api_key: str) -> list[dict]:
        return [{**_POSTING, "description": snippet}]

    async def fake_reader(url: str) -> str:
        return page_text

    return sf.SearchFirmAdapter(
        [FIRM],
        queries=["chief technology officer"],
        regions=["Ohio"],
        places=["Ohio", "Fairhaven"],
        home_state="oh",
        search=fake_search,
        reader=fake_reader,
    )


@pytest.mark.asyncio
async def test_page_text_supplies_the_place_the_snippet_lacked():
    a = _adapter("Back to Jobs\nChief Technology Officer\nContoso Fairhaven, OH\nThe role.")
    (posting,) = await a.fetch()
    assert posting.location_raw == "Fairhaven"
    assert posting.region == "OH"


@pytest.mark.asyncio
async def test_page_text_never_sets_remote():
    a = _adapter("Contoso Fairhaven. Our teams work remote on some days.")
    (posting,) = await a.fetch()
    assert posting.location_raw == "Fairhaven"
    assert posting.remote is None


@pytest.mark.asyncio
async def test_page_naming_the_state_then_a_city_gives_the_city():
    a = _adapter("Ohio retained search. Headquarters: Fairhaven.")
    (posting,) = await a.fetch()
    assert posting.location_raw == "Fairhaven"


@pytest.mark.asyncio
async def test_snippet_place_is_not_overridden_by_the_page():
    a = _adapter("Offices in Fairhaven.", snippet="Based in Ohio.")
    (posting,) = await a.fetch()
    assert posting.location_raw == "Ohio"

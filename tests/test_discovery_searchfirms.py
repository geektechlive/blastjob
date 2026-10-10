"""Pure tests for the retained-search-firm monitor, plus an adapter test with
injected search/reader and a pipeline round-trip — no network."""

from __future__ import annotations

import tempfile
from datetime import date

import pytest

from blastjob.db import client as dbc
from blastjob.discovery import searchfirms as sf
from blastjob.discovery.pipeline import run_discovery
from blastjob.models.config import BlastJobConfig, DiscoveryConfig, SearchFirm

ACME_SEARCH = SearchFirm(
    name="Acme Executive Search", slug="acmesearch", domain="acmesearch.example"
)

# Config defaults ship empty (a fresh install targets no search firms until
# you add your own); tests exercise the mechanism with explicit fictional
# values instead of relying on defaults.
_QUERIES = ["chief technology officer", "svp of enterprise technology"]
_REGIONS = ["Ohio", "Columbus", "Cleveland", "remote"]


# ---------------------------------------------------------------------------
# build_queries
# ---------------------------------------------------------------------------


def test_build_queries_at_most_three_per_firm():
    queries = sf.build_queries(ACME_SEARCH, _QUERIES, _REGIONS)
    assert 1 <= len(queries) <= 3


def test_build_queries_each_under_260_chars_and_scoped_to_domain():
    queries = sf.build_queries(ACME_SEARCH, _QUERIES, _REGIONS)
    assert queries
    for q in queries:
        assert len(q) < 260
        assert "site:acmesearch.example" in q


def test_build_queries_chunks_long_title_list():
    long_titles = [f"exec title number {i} that is fairly long text" for i in range(10)]
    queries = sf.build_queries(ACME_SEARCH, long_titles, ["Ohio"])
    assert len(queries) > 1
    assert len(queries) <= 3
    for q in queries:
        assert len(q) < 260


def test_build_queries_empty_when_no_domain_or_no_queries():
    blank = SearchFirm(name="No Domain", slug="nodomain", domain="")
    assert sf.build_queries(blank, ["chief operations officer"], ["Ohio"]) == []
    assert sf.build_queries(ACME_SEARCH, [], ["Ohio"]) == []


# ---------------------------------------------------------------------------
# looks_like_posting
# ---------------------------------------------------------------------------


def test_looks_like_posting_accepts_real_opportunity():
    assert sf.looks_like_posting(
        "https://www.acmesearch.example/opportunities/12345/chief-operations-officer",
        "Chief Operations Officer",
        "acmesearch.example",
    )


def test_looks_like_posting_rejects_insights_page():
    assert not sf.looks_like_posting(
        "https://www.acmesearch.example/insights/this-week-in-leadership",
        "Chief Operations Officer",
        "acmesearch.example",
    )


def test_looks_like_posting_rejects_wrong_host():
    assert not sf.looks_like_posting(
        "https://www.globexsearch.example/opportunities/12345/chief-operations-officer",
        "Chief Operations Officer",
        "acmesearch.example",
    )


def test_looks_like_posting_rejects_ic_title():
    assert not sf.looks_like_posting(
        "https://www.acmesearch.example/opportunities/12345/senior-software-engineer",
        "Senior Software Engineer",
        "acmesearch.example",
    )


def test_looks_like_posting_accepts_interim_jobs_subdomain():
    """Real shape from a live sweep: a firm's interim-placement board can live
    on an `interimjobs.` subdomain, not a listed opportunities path."""
    assert sf.looks_like_posting(
        "https://interimjobs.acmesearch.example/jobs/vp-of-ops-1576442",
        "VP of Ops Job | Job #1576442 - Interim Jobs | Acme Executive Search",
        "acmesearch.example",
    )


def test_looks_like_posting_rejects_practice_page():
    """False-positive shape from a live sweep: a firm's functional-roles page
    describes the Chief Officer role as a practice area, not an actual search
    assignment. Title vocabulary matches (`_EXEC_TITLE`), so only the
    `/what-we-do/functional-roles/` path exclusion catches it."""
    assert not sf.looks_like_posting(
        "https://www.globexsearch.example/what-we-do/functional-roles/"
        "operations-officer/chief-operations-officer",
        "Chief Operations Officer - Globex Retained Search",
        "globexsearch.example",
    )


def test_looks_like_posting_rejects_missing_fields():
    assert not sf.looks_like_posting("", "Chief Operations Officer", "acmesearch.example")
    assert not sf.looks_like_posting(
        "https://www.acmesearch.example/opportunities/1", "", "acmesearch.example"
    )
    assert not sf.looks_like_posting(
        "https://www.acmesearch.example/opportunities/1", "Chief Operations Officer", ""
    )


# ---------------------------------------------------------------------------
# external_id_for
# ---------------------------------------------------------------------------


def test_external_id_for_strips_scheme_query_fragment_and_trailing_slash():
    a = sf.external_id_for(
        "https://www.acmesearch.example/opportunities/12345/chief-operations-officer/"
        "?utm_source=x#frag"
    )
    b = sf.external_id_for(
        "https://www.acmesearch.example/opportunities/12345/chief-operations-officer"
    )
    assert a == b == "www.acmesearch.example/opportunities/12345/chief-operations-officer"


def test_external_id_for_lowercases_host():
    assert sf.external_id_for("https://WWW.AcmeSearch.example/opportunities/1") == (
        "www.acmesearch.example/opportunities/1"
    )


# ---------------------------------------------------------------------------
# infer_geo
# ---------------------------------------------------------------------------


def test_infer_geo_detects_configured_region_and_remote():
    loc, region, remote = sf.infer_geo(
        "Chief Officer", "Based in Ohio. Remote considered.", places=["Ohio", "remote"]
    )
    assert loc == "Ohio"
    assert region == "OH"
    assert remote is True


def test_infer_geo_reads_a_town_as_in_the_home_state():
    loc, region, remote = sf.infer_geo(
        "Chief Officer",
        "Our Fairhaven headquarters.",
        places=["Ohio", "Fairhaven"],
        home_state="oh",
    )
    assert (loc, region, remote) == ("Fairhaven", "OH", None)


def test_adapter_falls_back_to_search_regions_for_places():
    a = sf.SearchFirmAdapter([], queries=[], regions=["Ohio"])
    assert a.places == ["Ohio"]
    b = sf.SearchFirmAdapter([], queries=[], regions=["Ohio"], places=["Fairhaven"])
    assert b.places == ["Fairhaven"]


def test_infer_geo_unknown_when_no_place():
    loc, region, remote = sf.infer_geo(
        "Chief Officer", "A great opportunity somewhere in the Midwest.", places=["Ohio", "remote"]
    )
    assert loc == ""
    assert region == ""
    assert remote is None


# ---------------------------------------------------------------------------
# due_today
# ---------------------------------------------------------------------------


def test_due_today_matches_configured_weekday():
    monday = date(2026, 9, 7)
    tuesday = date(2026, 9, 8)
    assert sf.due_today(monday, 0) is True
    assert sf.due_today(tuesday, 0) is False


# ---------------------------------------------------------------------------
# SearchFirmAdapter
# ---------------------------------------------------------------------------

_REAL_POSTING = {
    "title": "Chief Operations Officer",
    "url": "https://www.acmesearch.example/opportunities/999/chief-operations-officer",
    "description": "A retained search for a Chief Officer in Springfield, OH.",
}
_INSIGHTS_PAGE = {
    "title": "Chief Operations Officer",
    "url": "https://www.acmesearch.example/insights/this-week-in-leadership",
    "description": "Thought leadership on the Chief Officer role.",
}
_IC_TITLE = {
    "title": "Senior Software Engineer",
    "url": "https://www.acmesearch.example/opportunities/1000/senior-software-engineer",
    "description": "An engineering role.",
}


def _adapter(**kwargs):
    async def fake_search(query: str, api_key: str):
        return [_REAL_POSTING, _INSIGHTS_PAGE, _IC_TITLE]

    async def fake_reader(url: str):
        return "A much longer job description fetched from the firm's own site. " * 5

    defaults = dict(
        firms=[ACME_SEARCH],
        queries=_QUERIES,
        regions=_REGIONS,
        search=fake_search,
        reader=fake_reader,
    )
    defaults.update(kwargs)
    return sf.SearchFirmAdapter(**defaults)


@pytest.mark.asyncio
async def test_adapter_filters_and_upgrades_description():
    a = _adapter()
    out = await a.fetch()
    assert len(out) == 1
    posting = out[0]
    assert posting.source == "searchfirm:acmesearch"
    assert posting.company == "Acme Executive Search (retained search)"
    assert "much longer job description" in posting.description


@pytest.mark.asyncio
async def test_adapter_skips_reader_for_known_ids():
    known = {sf.external_id_for(_REAL_POSTING["url"])}
    read_calls: list[str] = []

    async def fake_reader(url: str):
        read_calls.append(url)
        return "should not be called"

    a = _adapter(known_ids=known, reader=fake_reader)
    out = await a.fetch()
    assert len(out) == 1
    assert out[0].description == _REAL_POSTING["description"]
    assert read_calls == []


@pytest.mark.asyncio
async def test_adapter_raises_without_key_or_injected_search():
    a = sf.SearchFirmAdapter(
        firms=[ACME_SEARCH],
        queries=["chief operations officer"],
        regions=["Ohio"],
    )
    with pytest.raises(RuntimeError, match="JINA_API_KEY"):
        await a.fetch()


# ---------------------------------------------------------------------------
# pipeline round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_stores_searchfirm_postings_and_no_stale_mark_on_empty_run():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)

    res = await run_discovery(cfg, conn, adapters=[_adapter()])
    assert res.inserted_or_updated == 1
    rows = conn.execute("SELECT source, source_state FROM jobs").fetchall()
    assert len(rows) == 1
    assert rows[0]["source"] == "searchfirm:acmesearch"
    assert rows[0]["source_state"] == "active"

    async def empty_search(query: str, api_key: str):
        return []

    empty_adapter = _adapter(search=empty_search)
    res2 = await run_discovery(cfg, conn, adapters=[empty_adapter])
    assert res2.fetched == 0
    assert res2.stale_marked == 0
    rows2 = conn.execute("SELECT source_state FROM jobs").fetchall()
    assert all(r["source_state"] == "active" for r in rows2)
    conn.close()


# ---------------------------------------------------------------------------
# config round-trip
# ---------------------------------------------------------------------------


def test_discovery_config_defaults_ship_empty():
    """A fresh install targets no search firms until the user adds their own
    (see config.example.toml for the shape of an entry)."""
    cfg = DiscoveryConfig()
    assert cfg.search_firms == []
    assert cfg.search_firm_queries == []
    assert cfg.search_firm_regions == []
    assert cfg.search_firms_enabled is True
    assert cfg.search_firm_weekday == 0


def test_discovery_config_accepts_four_explicit_search_firms():
    """The mechanism supports multiple configured firms; prove it with
    fictional ones rather than asserting on a shipped default list."""
    firms = [
        SearchFirm(name="Acme Executive Search", slug="acmesearch", domain="acmesearch.example"),
        SearchFirm(
            name="Fabrikam Search Partners", slug="fabrikamsearch", domain="fabrikamsearch.example"
        ),
        SearchFirm(
            name="Globex Retained Search", slug="globexsearch", domain="globexsearch.example"
        ),
        SearchFirm(
            name="Initech Leadership Advisors",
            slug="initechadvisors",
            domain="initechadvisors.example",
        ),
    ]
    cfg = DiscoveryConfig(search_firms=firms)
    assert len(cfg.search_firms) == 4
    slugs = {f.slug for f in cfg.search_firms}
    assert slugs == {"acmesearch", "fabrikamsearch", "globexsearch", "initechadvisors"}


def test_config_roundtrip_preserves_search_firm_fields(tmp_path, monkeypatch):
    import blastjob.config as mod

    config_file = tmp_path / "config.toml"
    monkeypatch.setattr(mod, "_CONFIG_FILE", config_file)
    monkeypatch.setattr(mod, "_CONFIG_DIR", config_file.parent)

    cfg = BlastJobConfig()
    cfg.discovery.search_firms_enabled = False
    cfg.discovery.search_firm_weekday = 2
    cfg.discovery.search_firms = [
        SearchFirm(name="Custom Firm", slug="custom", domain="custom.com")
    ]
    cfg.discovery.search_firm_queries = ["chief technology officer"]
    cfg.discovery.search_firm_regions = ["Ohio"]
    mod.save_config(cfg)

    loaded = mod.load_config()
    assert loaded.discovery.search_firms_enabled is False
    assert loaded.discovery.search_firm_weekday == 2
    assert [f.slug for f in loaded.discovery.search_firms] == ["custom"]
    assert loaded.discovery.search_firm_queries == ["chief technology officer"]
    assert loaded.discovery.search_firm_regions == ["Ohio"]

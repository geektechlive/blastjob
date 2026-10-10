"""Workday adapter: token parsing, listing/detail parsing (fixtures mirror the
live cxs responses captured 2026-09-04), the title gate, and the known-id stub
path via an httpx MockTransport — no network."""

from __future__ import annotations

import json
import tempfile

import httpx
import pytest

from blastjob.db import client as dbc
from blastjob.discovery import resolve, workday
from blastjob.discovery.normalize import to_job_columns
from blastjob.discovery.pipeline import known_jd_ids, run_discovery
from blastjob.discovery.textutil import normalize_region, split_location
from blastjob.models.config import BlastJobConfig

LISTING = {
    "total": 2,
    "jobPostings": [
        {
            "title": "Director, Business Infrastructure",
            "externalPath": "/job/Springfield-Ohio-United-States/Director_R26-1",
            "locationsText": "Springfield, Ohio, United States",
            "postedOn": "Posted 4 Days Ago",
            "bulletFields": ["R26-1"],
        },
        {
            "title": "Software Engineer II",
            "externalPath": "/job/Springfield-Ohio-United-States/Software-Engineer-II_R26-2",
            "locationsText": "Springfield, Ohio, United States",
            "postedOn": "Posted Today",
            "bulletFields": ["R26-2"],
        },
    ],
}

DETAIL = {
    "jobPostingInfo": {
        "id": "abc",
        "title": "Director, Business Infrastructure",
        "jobDescription": (
            "<p>Lead <b>enterprise</b> infrastructure.</p><ul><li>Own the budget</li></ul>"
        ),
        "location": "Springfield, Ohio, United States",
        "postedOn": "Posted 4 Days Ago",
        "startDate": "2026-08-31",
        "timeType": "Full time",
        "jobReqId": "R26-1",
        "country": {"descriptor": "United States of America"},
        "remoteType": "Hybrid",
        "externalUrl": "https://acme.wd1.myworkdayjobs.com/Careers/job/Springfield/Director_R26-1",
    },
    "hiringOrganization": {},
}


def test_parse_board_token_roundtrip():
    ref = workday.parse_board_token("fabrikam/wd503/ExternalCareers")
    assert (ref.tenant, ref.wd, ref.site) == ("fabrikam", "wd503", "ExternalCareers")
    assert (
        ref.jobs_url
        == "https://fabrikam.wd503.myworkdayjobs.com/wday/cxs/fabrikam/ExternalCareers/jobs"
    )
    assert ref.token == "fabrikam/wd503/ExternalCareers"


@pytest.mark.parametrize(
    "bad", ["", "fabrikam", "fabrikam/ExternalCareers", "fabrikam/five/site", "a//b"]
)
def test_parse_board_token_rejects_bad_shapes(bad):
    with pytest.raises(ValueError):
        workday.parse_board_token(bad)


def test_normalize_region_maps_full_state_names():
    assert normalize_region("Ohio") == "OH"
    assert normalize_region("OH") == "OH"
    assert normalize_region("Ontario") == "Ontario"
    assert split_location("Springfield, Ohio, United States") == ("Springfield", "OH")


def test_parse_listing_and_external_id():
    items = workday.parse_listing(LISTING)
    assert [i["req_id"] for i in items] == ["R26-1", "R26-2"]
    assert workday.external_id_for(items[0]) == "R26-1"
    assert workday.external_id_for({"external_path": "/job/x"}) == "/job/x"


_KEEP_TERMS = ["director"]
_DROP_TERMS = ["engineer"]


def test_title_gate_drops_ic_families_keeps_leadership_and_ambiguous():
    assert workday.title_wanted("Director, Business Infrastructure", _KEEP_TERMS, _DROP_TERMS)
    # ambiguous -> deferred to LLM
    assert workday.title_wanted("Business Manager", _KEEP_TERMS, _DROP_TERMS)
    assert not workday.title_wanted("Software Engineer II", _KEEP_TERMS, _DROP_TERMS)


def test_title_gate_with_no_configured_terms_gates_nothing():
    """An unconfigured install (both lists empty) must keep everything -- the
    LLM judges, not the discovery-time gate."""
    assert workday.title_wanted("Director, Business Infrastructure")
    assert workday.title_wanted("Software Engineer II")


def test_parse_detail_builds_posting():
    ref = workday.parse_board_token("acme/wd1/Careers")
    item = workday.parse_listing(LISTING)[0]
    p = workday.parse_detail("Acme", ref, item, DETAIL)
    assert p.source == "workday:acme"
    assert p.external_id == "R26-1"
    assert p.company == "Acme"
    assert "Lead enterprise infrastructure." in p.description
    assert "Own the budget" in p.description
    assert (p.city, p.region) == ("Springfield", "OH")
    assert p.workplace_type == "Hybrid"
    assert p.remote is None
    assert p.url.endswith("Director_R26-1")
    assert p.posted_at == "2026-08-31"


def test_stub_posting_has_empty_description_and_columns_omit_raw_jd():
    ref = workday.parse_board_token("acme/wd1/Careers")
    item = workday.parse_listing(LISTING)[0]
    stub = workday.stub_posting("Acme", ref, item)
    assert stub.description == ""
    assert stub.extra["detail_skipped"] is True
    cols = to_job_columns(stub)
    assert "raw_jd" not in cols
    assert cols["geo_region"] == "OH"


def _mock_transport(calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json=LISTING)
        if "/job/" in request.url.path:
            return httpx.Response(200, json=DETAIL)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_adapter_fetches_detail_only_for_unknown_wanted_titles():
    calls: list[str] = []
    a = workday.WorkdayAdapter(
        "Acme",
        "acme/wd1/Careers",
        queries=["director"],
        drop_terms=_DROP_TERMS,
        transport=_mock_transport(calls),
    )
    out = await a.fetch()
    # engineer dropped by the title gate; director fetched in full
    assert [p.external_id for p in out] == ["R26-1"]
    assert out[0].description
    assert sum(c.startswith("GET") for c in calls) == 1
    assert sum(c.startswith("POST") for c in calls) == 1


@pytest.mark.asyncio
async def test_adapter_stubs_known_ids_without_detail_fetch():
    calls: list[str] = []
    a = workday.WorkdayAdapter(
        "Acme",
        "acme/wd1/Careers",
        queries=["director"],
        known_ids={"R26-1"},
        drop_terms=_DROP_TERMS,
        transport=_mock_transport(calls),
    )
    out = await a.fetch()
    assert [p.external_id for p in out] == ["R26-1"]
    assert out[0].description == ""
    assert not any(c.startswith("GET") for c in calls)


@pytest.mark.asyncio
async def test_adapter_keeps_stub_when_detail_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json=LISTING)
        return httpx.Response(403)

    a = workday.WorkdayAdapter(
        "Acme",
        "acme/wd1/Careers",
        queries=["director"],
        max_retries=1,
        drop_terms=_DROP_TERMS,
        transport=httpx.MockTransport(handler),
    )
    out = await a.fetch()
    assert [p.external_id for p in out] == ["R26-1"]
    assert out[0].description == ""


@pytest.mark.asyncio
async def test_pipeline_refresh_never_blanks_a_stored_jd():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)
    calls: list[str] = []
    full = workday.WorkdayAdapter(
        "Acme",
        "acme/wd1/Careers",
        queries=["director"],
        drop_terms=_DROP_TERMS,
        transport=_mock_transport(calls),
    )
    await run_discovery(cfg, conn, adapters=[full])
    row = conn.execute("SELECT raw_jd FROM jobs WHERE source='workday:acme'").fetchone()
    assert "Lead enterprise infrastructure." in row["raw_jd"]

    assert known_jd_ids(conn, "workday:acme") == {"R26-1"}
    stubbed = workday.WorkdayAdapter(
        "Acme",
        "acme/wd1/Careers",
        queries=["director"],
        known_ids=known_jd_ids(conn, "workday:acme"),
        drop_terms=_DROP_TERMS,
        transport=_mock_transport(calls),
    )
    res = await run_discovery(cfg, conn, adapters=[stubbed])
    row = conn.execute(
        "SELECT raw_jd, source_state FROM jobs WHERE source='workday:acme'"
    ).fetchone()
    assert "Lead enterprise infrastructure." in row["raw_jd"]
    assert row["source_state"] == "active"
    assert res.stale_marked == 0
    conn.close()


def test_workday_candidates_from_search_text():
    text = json.dumps(
        {
            "data": [
                {
                    "url": "https://fabrikam.wd503.myworkdayjobs.com/ExternalCareers/job/Springfield/Director_R1"
                },
                {"url": "https://fabrikam.wd503.myworkdayjobs.com/en-US/ExternalCareers"},
                {"url": "https://wingtip.wd1.myworkdayjobs.com/Wingtip_Careers?q=director"},
                {"url": "https://globex.wd504.myworkdayjobs.com/en-us"},
                {"url": "https://initech.wd1.myworkdayjobs.com/search"},
            ]
        }
    )
    refs = resolve.workday_candidates(text)
    assert [(r.tenant, r.wd, r.site) for r in refs] == [
        ("fabrikam", "wd503", "ExternalCareers"),
        ("wingtip", "wd1", "Wingtip_Careers"),
    ]

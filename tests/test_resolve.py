"""Tests for ATS token slug generation and URL-candidate extraction (live network
probing is exercised via `python -m blastjob.discovery.resolve`, not here)."""

from __future__ import annotations

import httpx
import pytest

from blastjob.discovery.dayforce import BoardRef as DayforceRef
from blastjob.discovery.dayforce import count_postings
from blastjob.discovery.resolve import dayforce_candidates, slug_variants, workday_candidates

CSRF = {"csrfToken": "0" * 64}


def test_slug_variants_strips_suffixes_and_punctuation():
    v = slug_variants("Acme Corp, Inc.")
    assert "acme" in v
    assert all(" " not in s for s in v)


def test_slug_variants_hyphen_and_compact():
    v = slug_variants("Summit Supply")
    assert "summitsupply" in v
    assert "summit-supply" in v


def test_slug_variants_dedupes_and_no_empties():
    v = slug_variants("Nimbus")
    assert v == ["nimbus"]
    assert "" not in v


def test_dayforce_candidates_from_search_text():
    text = """
    Search results:
    https://jobs.dayforcehcm.com/lakeshorecu/CANDIDATEPORTAL/jobs/18280
    https://jobs.dayforcehcm.com/carrow/CANDIDATEPORTAL
    https://us231.dayforcehcm.com/CandidatePortal/en-US/toolforge
    https://jobs.dayforcehcm.com/api/auth/csrf
    """
    refs = dayforce_candidates(text)
    assert [(r.client, r.board) for r in refs] == [
        ("lakeshorecu", "CANDIDATEPORTAL"),
        ("carrow", "CANDIDATEPORTAL"),
        ("toolforge", "CANDIDATEPORTAL"),
    ]


def test_workday_candidates_from_search_text():
    text = """
    https://nwmc.wd503.myworkdayjobs.com/ExternalCareers/job/Springfield/Director_R1
    https://northwind.wd1.myworkdayjobs.com/Northwind_Careers?q=director
    https://carrow.wd504.myworkdayjobs.com/en-us
    https://harborlake.wd1.myworkdayjobs.com/search
    """
    refs = workday_candidates(text)
    assert [(r.tenant, r.site) for r in refs] == [
        ("nwmc", "ExternalCareers"),
        ("northwind", "Northwind_Careers"),
    ]


def _mock_transport(search_response: httpx.Response) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/csrf":
            return httpx.Response(200, json=CSRF)
        if request.url.path.endswith("/jobposting/search"):
            return search_response
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_count_postings_via_mock_transport():
    ref = DayforceRef(client="lakeshorecu", board="CANDIDATEPORTAL")

    ok_transport = _mock_transport(httpx.Response(200, json={"jobPostings": [], "maxCount": 7}))
    assert await count_postings(ref, transport=ok_transport) == 7

    forbidden_transport = _mock_transport(httpx.Response(403))
    assert await count_postings(ref, transport=forbidden_transport) is None

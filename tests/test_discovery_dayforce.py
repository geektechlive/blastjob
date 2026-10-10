"""Dayforce adapter: token parsing, listing parsing, the title gate, the
anonymous-CSRF handshake and pagination — all through an httpx MockTransport, no
network. Fixtures are trimmed from the live Lakeshore Credit Union responses
captured 2026-09-05 (jobDescription truncated; everything else verbatim)."""

from __future__ import annotations

import httpx
import pytest

from blastjob.discovery import dayforce

CSRF = {"csrfToken": "0" * 64}

VP_IT = {
    "clientNamespace": "lakeshorecu",
    "jobBoardId": 1,
    "jobPostingId": 18280,
    "jobReqId": 2155,
    "jobTitle": "Generic VP Role",
    "jobDescription": (
        "<p>At Lakeshore Credit Union, we succeed by putting members first "
        "&mdash; and that starts with you.</p><ul><li>Own the platform "
        "roadmap</li></ul>"
    ),
    "hasVirtualLocation": False,
    "postingStartTimestampUTC": "2026-08-01T05:00:00+00:00",
    "postingExpiryTimestampUTC": None,
    "isEvergreen": False,
    "postingLocations": [
        {
            "formattedAddress": "555 S Executive Dr, Fairhaven, OH 45201, USA",
            "locationId": 6,
            "locationType": 2,
            "isoCountryCode": "US",
            "stateCode": "OH",
            "cityName": "Fairhaven",
            "coordinates": "lat:39.9500000;lng:-83.0500000",
        }
    ],
    "postingAppliedStatus": {"jobPostingId": 18280, "hasApplied": False, "canApplyAgain": True},
    "searchScore": 0,
}

TELLER = {
    "clientNamespace": "lakeshorecu",
    "jobBoardId": 1,
    "jobPostingId": 18552,
    "jobReqId": 2206,
    "jobTitle": "Teller I, PT",
    "jobDescription": "<p>Performs transactional duties to serve members.</p>",
    "hasVirtualLocation": False,
    "postingStartTimestampUTC": "2026-09-04T05:00:00+00:00",
    "isEvergreen": False,
    "postingLocations": [
        {
            "formattedAddress": "6219 Washington Ave, Example City, OH 45201, USA",
            "isoCountryCode": "US",
            "stateCode": "OH",
            "cityName": "Example City",
        }
    ],
}

REMOTE_DIRECTOR = {
    "jobPostingId": 18303,
    "jobReqId": 2156,
    "jobTitle": "VP, Analytics & AI",
    "jobDescription": "<p>Lead the analytics function.</p>",
    "hasVirtualLocation": True,
    "postingStartTimestampUTC": "2026-08-05T05:00:00+00:00",
    "postingLocations": [],
}

# maxCount > PAGE_SIZE so the loop must ask for a second page, then stop because
# paginationStart has passed the server's own total (mirrors the live 54/3-page
# board, scaled down so the fixtures stay readable).
PAGE_0 = {"jobPostings": [VP_IT, TELLER], "maxCount": 26, "offset": 0, "count": 2}
PAGE_1 = {"jobPostings": [REMOTE_DIRECTOR], "maxCount": 26, "offset": 25, "count": 1}


def test_parse_board_token_roundtrip():
    ref = dayforce.parse_board_token("lakeshorecu/CANDIDATEPORTAL")
    assert (ref.client, ref.board) == ("lakeshorecu", "CANDIDATEPORTAL")
    assert ref.search_url == ("https://jobs.dayforcehcm.com/api/geo/lakeshorecu/jobposting/search")
    assert ref.public_url("18280") == (
        "https://jobs.dayforcehcm.com/lakeshorecu/CANDIDATEPORTAL/jobs/18280"
    )
    assert ref.token == "lakeshorecu/CANDIDATEPORTAL"
    assert dayforce.source_for(ref) == "dayforce:lakeshorecu"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "lakeshorecu",
        "lakeshorecu/CANDIDATEPORTAL/extra",
        "a//b",
        # the host form is a real config mistake: the API host is constant and
        # the board code is required, so it must fail loudly
        "us231.dayforcehcm.com/lakeshorecu",
    ],
)
def test_parse_board_token_rejects_bad_shapes(bad):
    with pytest.raises(ValueError):
        dayforce.parse_board_token(bad)


def test_search_body_carries_the_four_required_fields():
    ref = dayforce.parse_board_token("lakeshorecu/CANDIDATEPORTAL")
    assert dayforce.search_body(ref, 25) == {
        "clientNamespace": "lakeshorecu",
        "jobBoardCode": "CANDIDATEPORTAL",
        "cultureCode": "en-US",
        "paginationStart": 25,
    }


def test_parse_listing_skips_rows_without_an_id_and_reads_total():
    payload = {"jobPostings": [VP_IT, {"jobTitle": "no id"}], "maxCount": 54}
    items = dayforce.parse_listing(payload)
    assert [dayforce.external_id_for(i) for i in items] == ["18280"]
    assert dayforce.total_count(payload) == 54
    assert dayforce.total_count({}) == 0


_KEEP_TERMS = ["vp"]
_DROP_TERMS = ["teller"]


def test_title_gate_drops_ic_families_keeps_leadership():
    assert dayforce.title_wanted("Generic VP Role", _KEEP_TERMS, _DROP_TERMS)
    # ambiguous -> deferred to LLM
    assert dayforce.title_wanted("Manager, Branch", _KEEP_TERMS, _DROP_TERMS)
    assert not dayforce.title_wanted("Teller I, PT", _KEEP_TERMS, _DROP_TERMS)


def test_title_gate_with_no_configured_terms_gates_nothing():
    """An unconfigured install (both lists empty) must keep everything -- the
    LLM judges, not the discovery-time gate."""
    assert dayforce.title_wanted("Generic VP Role")
    assert dayforce.title_wanted("Teller I, PT")


def test_parse_posting_uses_structured_location_and_strips_html():
    ref = dayforce.parse_board_token("lakeshorecu/CANDIDATEPORTAL")
    p = dayforce.parse_posting("Lakeshore Credit Union", ref, VP_IT)
    assert p.source == "dayforce:lakeshorecu"
    assert p.external_id == "18280"  # jobPostingId, not jobReqId
    assert p.extra["req_id"] == "2155"
    assert p.title == "Generic VP Role"
    assert p.url.endswith("/jobs/18280")
    assert "putting members first — and that starts with you." in p.description
    assert "Own the platform roadmap" in p.description
    assert "<p>" not in p.description
    assert (p.city, p.region, p.country) == ("Fairhaven", "OH", "US")
    assert p.location_raw.startswith("555 S Executive Dr")
    assert p.remote is None and p.workplace_type == ""
    assert p.posted_at == "2026-08-01T05:00:00+00:00"


def test_parse_posting_marks_virtual_location_remote_and_tolerates_no_location():
    ref = dayforce.parse_board_token("lakeshorecu/CANDIDATEPORTAL")
    p = dayforce.parse_posting("Lakeshore Credit Union", ref, REMOTE_DIRECTOR)
    assert p.remote is True and p.workplace_type == "Remote"
    assert (p.location_raw, p.city, p.region, p.country) == ("", "", "", "")


def _mock_transport(calls: list[httpx.Request], pages=(PAGE_0, PAGE_1)) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/auth/csrf":
            return httpx.Response(200, json=CSRF)
        if request.url.path.endswith("/jobposting/search"):
            n = sum(1 for c in calls if c.method == "POST")
            return httpx.Response(200, json=pages[min(n - 1, len(pages) - 1)])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_adapter_sends_csrf_header_and_pages_to_the_server_total():
    calls: list[httpx.Request] = []
    a = dayforce.DayforceAdapter(
        "Lakeshore Credit Union",
        "lakeshorecu/CANDIDATEPORTAL",
        queries=["director", "vp"],
        drop_terms=_DROP_TERMS,
        transport=_mock_transport(calls),
    )
    out = await a.fetch()

    # CSRF fetched once, then one POST per page; the loop stops when
    # paginationStart passes maxCount rather than walking forever.
    assert [c.method for c in calls] == ["GET", "POST", "POST"]
    assert calls[0].url.path == "/api/auth/csrf"
    posts = [c for c in calls if c.method == "POST"]
    assert all(c.headers["x-csrf-token"] == CSRF["csrfToken"] for c in posts)
    assert [c.read().decode() for c in posts] == [
        '{"clientNamespace":"lakeshorecu","jobBoardCode":"CANDIDATEPORTAL",'
        '"cultureCode":"en-US","paginationStart":0}',
        '{"clientNamespace":"lakeshorecu","jobBoardCode":"CANDIDATEPORTAL",'
        '"cultureCode":"en-US","paginationStart":25}',
    ]

    # Teller dropped by the scope gate; both VP rows kept, with full JDs.
    assert [p.external_id for p in out] == ["18280", "18303"]
    assert all(p.description for p in out)


@pytest.mark.asyncio
async def test_known_ids_still_emit_the_full_jd():
    """Unlike Workday, the JD ships with the listing — there is no detail fetch
    to skip, so a known id must not be blanked into a stub."""
    calls: list[httpx.Request] = []
    a = dayforce.DayforceAdapter(
        "Lakeshore Credit Union",
        "lakeshorecu/CANDIDATEPORTAL",
        known_ids={"18280"},
        transport=_mock_transport(calls),
    )
    out = await a.fetch()
    vp = next(p for p in out if p.external_id == "18280")
    assert "Own the platform roadmap" in vp.description


@pytest.mark.asyncio
async def test_max_pages_caps_a_server_that_never_reports_a_short_page():
    calls: list[httpx.Request] = []
    full = {
        "jobPostings": [dict(VP_IT, jobPostingId=i) for i in range(1, 26)],
        "maxCount": 10_000,
    }
    a = dayforce.DayforceAdapter(
        "Lakeshore Credit Union",
        "lakeshorecu/CANDIDATEPORTAL",
        max_pages=3,
        transport=_mock_transport(calls, pages=(full,)),
    )
    await a.fetch()
    assert sum(1 for c in calls if c.method == "POST") == 3


@pytest.mark.asyncio
async def test_empty_page_stops_the_loop_even_when_max_count_lies_high():
    calls: list[httpx.Request] = []
    empty = {"jobPostings": [], "maxCount": 10_000}
    a = dayforce.DayforceAdapter(
        "Lakeshore Credit Union",
        "lakeshorecu/CANDIDATEPORTAL",
        drop_terms=_DROP_TERMS,
        transport=_mock_transport(calls, pages=(PAGE_0, empty)),
    )
    out = await a.fetch()
    assert sum(1 for c in calls if c.method == "POST") == 2
    assert [p.external_id for p in out] == ["18280"]


@pytest.mark.asyncio
async def test_missing_csrf_token_raises_rather_than_posting_blind():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/csrf":
            return httpx.Response(200, json={})
        raise AssertionError("must not POST without a token")

    a = dayforce.DayforceAdapter(
        "Lakeshore Credit Union",
        "lakeshorecu/CANDIDATEPORTAL",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ValueError):
        await a.fetch()

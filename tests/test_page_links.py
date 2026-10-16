"""Employer careers pages as a closure link (`page:<host>`).

The fixture is the live shape that motivated it (2026-10-08): Northwind Services
runs JazzHR on careers.groundmasters.com; the page carries schema.org
JobPosting JSON-LD naming "Northwind Services Inc", and its apply form posts to
northwindservices1.applytojob.com. An unknown posting URL on that host answers 410.

No test here touches the network: search and fetch are injected fakes, and the
real `_safe_get` / `_jina_search` run against `httpx.MockTransport` with a fake
DNS resolver. linkedin.com is never requested (a standing rule since 2026-09-13).
"""

from __future__ import annotations

import datetime
import json

import httpx
import pytest

from blastjob.core import page_fetch as pf
from blastjob.core import page_links as pl
from blastjob.core.closure_links import Link
from blastjob.core.page_fetch import jina_search as real_jina_search
from blastjob.core.page_fetch import safe_get as real_safe_get

HOST = "careers.groundmasters.com"
POSTING_URL = f"https://{HOST}/apply/jobs/details/AbC123"
TODAY = datetime.date(2026, 10, 8)
COMPANY, TITLE, REGION = "Northwind Services", "VP of Technology", "Ohio"


def _posting(**over) -> dict:
    p = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "VP of Technology",
        "url": POSTING_URL,
        "datePosted": "2026-09-01",
        "validThrough": "2099-12-31",
        "hiringOrganization": {"@type": "Organization", "name": "Northwind Services Inc"},
        "jobLocation": {
            "@type": "Place",
            "address": {"addressLocality": "Oakbrook", "addressRegion": "OH"},
        },
    }
    return {**p, **over}


def _html(
    *blocks: object,
    form: str = "https://northwindservices1.applytojob.com/apply/AbC123",
    extra: str = "",
) -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{b if isinstance(b, str) else json.dumps(b)}</script>'
        for b in blocks
    )
    action = f'<form action="{form}" method="post"></form>' if form else ""
    return f"<html><head>{scripts}</head><body>{extra}{action}</body></html>"


NORTHWIND = _html(_posting())


def _page(status: int = 200, html: str = NORTHWIND, url: str = POSTING_URL, cross: bool = False):
    return pf.Page(status=status, url=url, html=html, cross_host=cross)


# --- JSON-LD parsing -----------------------------------------------------------


def test_single_jobposting_object_is_found():
    (p,) = pl.job_postings(NORTHWIND)
    assert p["title"] == "VP of Technology"


def test_graph_and_top_level_lists_are_flattened():
    html = _html(
        {"@context": "x", "@graph": [{"@type": "Organization"}, _posting(title="A")]},
        [_posting(title="B"), {"@type": "WebPage"}],
    )
    assert [p["title"] for p in pl.job_postings(html)] == ["A", "B"]


def test_invalid_json_is_skipped():
    html = _html("{not json", _posting())
    assert len(pl.job_postings(html)) == 1


def test_type_as_a_list_counts():
    html = _html(_posting(**{"@type": ["Thing", "JobPosting"]}))
    assert len(pl.job_postings(html)) == 1


def test_other_script_types_are_ignored():
    html = f'<script type="text/javascript">{json.dumps(_posting())}</script>'
    assert pl.job_postings(html) == []


# --- the trust test --------------------------------------------------------------


def _trusted(html: str = NORTHWIND, url: str = POSTING_URL, posting: dict | None = None, **kw):
    posting = posting if posting is not None else pl.job_postings(html)[0]
    args = {"company": COMPANY, "title": TITLE, "region": REGION, **kw}
    return pl.trusted_employer_page(url, html, posting, **args)


def test_jazzhr_form_matching_org_and_title_is_trusted():
    assert _trusted()
    assert pl.ats_binding(POSTING_URL, NORTHWIND, COMPANY) == "northwindservices1.applytojob.com"


def test_aggregator_host_is_never_trusted():
    assert not _trusted(url="https://www.indeed.com/viewjob?jk=1")
    assert not _trusted(url="https://uk.linkedin.com/jobs/view/1")


def test_no_ats_binding_on_an_unrelated_domain_is_not_trusted():
    assert not _trusted(html=_html(_posting(), form=""))


def test_script_src_and_link_href_and_anchor_do_not_count():
    extra = (
        '<script src="https://northwindservices1.applytojob.com/x.js"></script>'
        '<link href="https://northwindservices1.applytojob.com/x.css">'
        '<a href="https://northwindservices1.applytojob.com/apply/AbC123">Apply on company site</a>'
    )
    assert not _trusted(html=_html(_posting(), form="", extra=extra))


def test_ats_tenant_must_belong_to_the_company():
    assert not _trusted(html=_html(_posting(), form="https://otherco.applytojob.com/apply/1"))


def test_ats_host_is_parsed_not_substring_matched():
    form = "https://evil.example.com/?next=northwindservices1.applytojob.com"
    assert not _trusted(html=_html(_posting(), form=form))


def test_page_on_the_ats_host_itself_is_trusted():
    url = "https://northwindservices1.applytojob.com/apply/AbC123/VP-of-Technology"
    html = _html(_posting(url=url), form="")
    assert _trusted(html=html, url=url)


def test_path_tenanted_ats_uses_the_first_path_segment():
    url = "https://jobs.lever.co/northwindservices/abc-123"
    assert _trusted(html=_html(_posting(url=url), form=""), url=url)
    other = "https://jobs.lever.co/acme/abc-123"
    assert not _trusted(html=_html(_posting(url=other), form=""), url=other)


def test_org_mismatch_is_not_trusted():
    html = _html(_posting(hiringOrganization={"name": "Acme Snow Removal"}))
    assert not _trusted(html=html)


def test_org_as_a_plain_string_is_read():
    assert _trusted(html=_html(_posting(hiringOrganization="Northwind Services, Inc.")))


def test_title_mismatch_is_not_trusted():
    assert not _trusted(html=_html(_posting(title="Director of Field Operations")))


def test_two_equally_good_titles_on_one_page_are_not_trusted():
    html = _html(_posting(), _posting(url=POSTING_URL + "-2"))
    assert not _trusted(html=html)


def test_a_multi_employer_page_is_not_trusted():
    html = _html(_posting(), _posting(title="CFO", hiringOrganization={"name": "Acme"}))
    assert not _trusted(html=html)


def test_region_from_a_location_list_must_agree():
    loc = [{"address": {"addressRegion": "IL"}}]
    assert not _trusted(html=_html(_posting(jobLocation=loc)))


def test_missing_location_and_odd_shapes_do_not_raise():
    html = _html(
        _posting(jobLocation="Oakbrook, OH", hiringOrganization=[{"name": "Northwind Services"}])
    )
    assert _trusted(html=html)


# --- check_page -------------------------------------------------------------------


def _fetch_returning(page):
    calls: list[str] = []

    async def fetch(url: str):
        calls.append(url)
        return page

    return fetch, calls


LINK = Link(f"page:{HOST}", POSTING_URL, POSTING_URL)


async def _check(page, **kw):
    fetch, _ = _fetch_returning(page)
    return await pl.check_page(LINK, COMPANY, TITLE, REGION, fetch=fetch, today=TODAY, **kw)


@pytest.mark.parametrize("status", [404, 410])
async def test_gone_status_from_the_same_host_is_closed(status):
    assert await _check(_page(status, html="")) is False


async def test_gone_after_a_cross_host_redirect_is_no_evidence():
    assert (
        await _check(_page(410, html="", url="https://elsewhere.example/gone", cross=True)) is None
    )


async def test_open_posting_is_verified_open():
    assert await _check(_page()) is True


async def test_past_valid_through_is_no_evidence():
    assert await _check(_page(html=_html(_posting(validThrough="2026-10-01")))) is None


async def test_page_without_a_jobposting_is_no_evidence():
    assert await _check(_page(html="<html><body>Jobs</body></html>")) is None


async def test_forbidden_is_no_evidence():
    assert await _check(_page(403, html="")) is None


async def test_a_page_now_naming_another_employer_is_no_evidence():
    html = _html(_posting(hiringOrganization={"name": "Acme"}))
    assert await _check(_page(html=html)) is None


async def test_a_rejected_fetch_is_no_evidence():
    assert await _check(None) is None


async def test_a_listing_page_after_a_redirect_is_no_evidence():
    listing = _html(_posting(url=f"https://{HOST}/apply/jobs/details/Other1"))
    assert await _check(_page(html=listing, url=f"https://{HOST}/apply")) is None


# --- find_page_link -------------------------------------------------------------------


def _search_returning(urls: list[str]):
    queries: list[str] = []

    async def search(query: str, key: str) -> list[str]:
        queries.append(query)
        return list(urls)

    return search, queries


def _fetch_map(pages: dict[str, pf.Page | None]):
    calls: list[str] = []

    async def fetch(url: str):
        assert "linkedin.com" not in (httpx.URL(url).host or ""), f"fetched {url}"
        calls.append(url)
        return pages.get(url)

    return fetch, calls


async def _find(urls: list[str], pages: dict):
    search, queries = _search_returning(urls)
    fetch, calls = _fetch_map(pages)
    link = await pl.find_page_link(COMPANY, TITLE, REGION, "k", search=search, fetch=fetch)
    return link, calls, queries


async def test_linkedin_results_are_never_fetched():
    urls = [
        "https://www.linkedin.com/jobs/view/123",
        "https://uk.linkedin.com/jobs/view/9",
        "https://www.linkedin.com./jobs/view/7",
    ]

    link, calls, queries = await _find(urls, {})

    assert link is None and calls == []
    assert queries == ['"Northwind Services" "VP of Technology"']


async def test_aggregator_results_are_skipped():
    urls = ["https://www.indeed.com/viewjob?jk=1", "https://www.ziprecruiter.com/c/x", POSTING_URL]

    link, calls, _ = await _find(urls, {POSTING_URL: _page()})

    assert calls == [POSTING_URL]
    assert link == Link(f"page:{HOST}", POSTING_URL, POSTING_URL)


async def test_only_the_first_three_candidates_are_fetched():
    urls = [f"https://site{i}.example/job" for i in range(5)]

    _link, calls, _ = await _find(urls, {})

    assert calls == urls[:3]


async def test_two_trusted_pages_are_ambiguous():
    other = "https://northwindservices1.applytojob.com/apply/Zz9/VP-of-Technology"
    pages = {
        POSTING_URL: _page(),
        other: _page(html=_html(_posting(url=other), form=""), url=other),
    }

    link, _calls, _ = await _find([POSTING_URL, other], pages)

    assert link is None


async def test_the_same_posting_found_twice_is_one_link():
    alias = f"https://{HOST}/apply/jobs/details/AbC123?source=google"
    pages = {POSTING_URL: _page(), alias: _page(url=alias)}

    link, _calls, _ = await _find([POSTING_URL, alias], pages)

    assert link is not None and link.external_id == POSTING_URL


async def test_a_posting_whose_own_url_is_not_the_fetched_page_is_not_linked():
    """Link only the URL actually fetched and verified: a self-reported JobPosting
    url that was never requested could 404 later and falsely close a live job."""
    fetched = f"https://{HOST}/careers/vp-technology"
    pages = {fetched: _page(url=fetched)}  # its JobPosting says url=POSTING_URL

    link, _calls, _ = await _find([fetched], pages)

    assert link is None


async def test_a_page_listing_several_postings_is_not_linked():
    """A 404 on a multi-role page says the page moved, not that this role closed."""
    board = _html(_posting(), _posting(title="Fleet Mechanic", url=f"https://{HOST}/apply/x"))
    pages = {POSTING_URL: _page(html=board)}

    link, _calls, _ = await _find([POSTING_URL], pages)

    assert link is None


async def test_a_failed_candidate_fetch_means_no_link():
    async def fetch(url: str):
        raise httpx.ConnectError("down")

    search, _ = _search_returning([POSTING_URL])
    assert await pl.find_page_link(COMPANY, TITLE, REGION, "k", search=search, fetch=fetch) is None


# --- _safe_get: every hop validated before it is requested ----------------------------


async def _public(host: str) -> list[str]:
    return {"internal.example": ["10.1.2.3"]}.get(host, ["93.184.216.34"])


def _transport(routes: dict[str, httpx.Response]):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # safe_get connects to the validated IP and keeps the name in the Host
        # header (SSRF pin), so key routes by the URL the caller asked for.
        asked = str(request.url.copy_with(host=request.headers["host"].split(":")[0]))
        seen.append(asked)
        return routes.get(asked, httpx.Response(404))

    return httpx.MockTransport(handler), seen


def _redirect(to: str) -> httpx.Response:
    return httpx.Response(302, headers={"Location": to})


@pytest.mark.parametrize(
    "target",
    [
        "https://www.linkedin.com/jobs/view/1",
        "https://www.linkedin.com./jobs/view/1",
        "HTTPS://WWW.LINKEDIN.COM/jobs/view/1",
        "http://127.0.0.1/admin",
        "http://10.0.0.8/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://100.100.100.200/latest/meta-data/",
        "http://internal.example/",
        "file:///etc/passwd",
    ],
)
async def test_a_redirect_to_a_forbidden_hop_is_never_requested(target):
    transport, seen = _transport({POSTING_URL: _redirect(target)})

    page = await real_safe_get(POSTING_URL, transport=transport, resolve=_public)

    assert page is None
    assert seen == [POSTING_URL]


async def test_a_forbidden_first_url_is_never_requested():
    transport, seen = _transport({})

    assert (
        await real_safe_get("https://linkedin.com/x", transport=transport, resolve=_public) is None
    )
    assert seen == []


async def test_more_than_five_hops_is_no_page():
    routes = {f"https://{HOST}/{i}": _redirect(f"https://{HOST}/{i + 1}") for i in range(10)}
    transport, seen = _transport(routes)

    assert await real_safe_get(f"https://{HOST}/0", transport=transport, resolve=_public) is None
    assert len(seen) == pf.MAX_HOPS + 1


async def test_same_host_redirect_then_gone_is_same_host():
    routes = {
        POSTING_URL: _redirect("/apply/gone"),
        f"https://{HOST}/apply/gone": httpx.Response(410),
    }
    transport, _ = _transport(routes)

    page = await real_safe_get(POSTING_URL, transport=transport, resolve=_public)

    assert page is not None and page.status == 410 and not page.cross_host


async def test_cross_host_redirect_is_flagged():
    routes = {
        POSTING_URL: _redirect("https://northwindservices1.applytojob.com/apply"),
        "https://northwindservices1.applytojob.com/apply": httpx.Response(
            200, text="<html></html>"
        ),
    }
    transport, _ = _transport(routes)

    page = await real_safe_get(POSTING_URL, transport=transport, resolve=_public)

    assert page is not None and page.status == 200 and page.cross_host


async def test_body_is_capped():
    big = "x" * (pf.MAX_BODY_BYTES + 10_000)
    transport, _ = _transport({POSTING_URL: httpx.Response(200, text=big)})

    page = await real_safe_get(POSTING_URL, transport=transport, resolve=_public)

    assert page is not None and len(page.html) <= pf.MAX_BODY_BYTES


async def test_unresolvable_host_is_no_page():
    async def nowhere(host: str) -> list[str]:
        return []

    transport, seen = _transport({})
    assert await real_safe_get(POSTING_URL, transport=transport, resolve=nowhere) is None
    assert seen == []


# --- _jina_search -----------------------------------------------------------------------


async def test_jina_search_sends_the_query_and_reads_result_urls():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"code": 200, "data": [{"url": POSTING_URL}, {"x": 1}]})

    urls = await real_jina_search('"A" "B"', "key", transport=httpx.MockTransport(handler))

    assert urls == [POSTING_URL]
    (req,) = seen
    assert req.url.host == "s.jina.ai" and req.url.params["q"] == '"A" "B"'
    assert req.headers["Authorization"] == "Bearer key"
    assert req.headers["X-Respond-With"] == "no-content"


async def test_jina_search_with_an_unexpected_body_finds_nothing():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"jobs": []}))
    assert await real_jina_search("q", "key", transport=transport) == []


# --- the suite-wide guard ---------------------------------------------------------------


async def test_unfaked_network_calls_fail_loudly():
    with pytest.raises(pytest.fail.Exception, match="page_links"):
        await pl._safe_get(POSTING_URL)
    with pytest.raises(pytest.fail.Exception, match="page_links"):
        await pl._jina_search("q", "k")
    assert not issubclass(pytest.fail.Exception, Exception)  # survives `except Exception`


async def test_an_expired_posting_is_not_linked():
    # check_page would call it no evidence; linking would write verified_open now.
    expired = _page(html=_html(_posting(validThrough="2026-01-01")))
    search, _ = _search_returning([POSTING_URL])
    fetch, _ = _fetch_map({POSTING_URL: expired})

    link = await pl.find_page_link(
        COMPANY, TITLE, REGION, "k", search=search, fetch=fetch, today=TODAY
    )

    assert link is None

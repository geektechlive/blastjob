"""Health of the retained-search-firm channel: query construction, the
confirmed-live 422/"no results" classification, and making a silent channel
loud.

Context (see `searchfirms.py` module docstring and commit 1b08b38): a weekly
log once showed 7 identical `searchfirm <firm> query failed: 422 Unprocessable
Entity` failures. The ticket that spawned this file hypothesized a
URL-encoding/parameter bug in query construction. Investigation here proves
that hypothesis false with no network call: `build_queries()` produces a
structurally identical query for every firm (only the domain token differs),
and `httpx.Request(...).url` shows no malformed encoding. The 422 is Jina's
documented "a narrow site: query matched nothing" response (confirmed live
2026-09-07, see commit 1b08b38 in web/people.py) -- an ordinary outcome, not a
request-shape bug -- and `web/people.jina_search` (a file this workstream does
not own) already classifies it as `[]` rather than raising. The tests below
lock in that classification via the REAL `_default_search` integration path
(httpx.MockTransport -- a canned fixture, never a live socket) so a future
regression anywhere in that chain is caught here too, then add the genuinely
new piece this workstream *does* own: `dead_firms()`, which turns N
consecutive zero-yield weekly runs into a signal loud enough for the cockpit
to show, mirroring `web/funnel.dead_boards` for a channel that almost never
reaches that function's >=10-postings threshold.
"""

from __future__ import annotations

import httpx
import pytest

from blastjob.discovery import searchfirms as sf
from blastjob.models.config import DiscoveryConfig, SearchFirm

ACME_SEARCH = SearchFirm(
    name="Acme Executive Search", slug="acmesearch", domain="acmesearch.example"
)
FABRIKAM_SEARCH = SearchFirm(
    name="Fabrikam Search Partners", slug="fabrikamsearch", domain="fabrikamsearch.example"
)
_QUERIES = ["chief technology officer", "svp of enterprise technology"]
_REGIONS = ["Ohio", "Columbus", "Cleveland", "remote"]

# ---------------------------------------------------------------------------
# (a) Query construction — no network. Rules out the encoding hypothesis.
# ---------------------------------------------------------------------------


def _configured_cfg() -> DiscoveryConfig:
    return DiscoveryConfig(
        search_firms=[ACME_SEARCH, FABRIKAM_SEARCH],
        search_firm_queries=_QUERIES,
        search_firm_regions=_REGIONS,
    )


def _all_configured_firm_queries() -> dict[str, list[str]]:
    cfg = _configured_cfg()
    return {
        firm.slug: sf.build_queries(firm, cfg.search_firm_queries, cfg.search_firm_regions)
        for firm in cfg.search_firms
    }


def test_query_construction_is_well_formed_for_every_configured_firm():
    """Balanced quotes/parens, exactly one site: token, no control characters,
    and an httpx-encoded URL under a sane length -- everything a URL-encoding
    bug would break, checked without a network call."""
    for slug, queries in _all_configured_firm_queries().items():
        assert queries, f"{slug} produced no queries"
        for q in queries:
            assert q.count('"') % 2 == 0, q
            assert q.count("(") == q.count(")"), q
            assert q.count("site:") == 1, q
            assert not any(ord(c) < 0x20 for c in q), q
            req = httpx.Request("GET", sf.SEARCH_URL, params={"q": q})
            encoded = str(req.url)
            assert encoded.startswith(sf.SEARCH_URL)
            assert " " not in encoded and '"' not in encoded  # properly percent-encoded
            assert len(encoded) < 500, encoded


def test_query_shape_identical_across_firms_only_domain_token_differs():
    """Direct evidence against the URL-encoding-problem hypothesis: every
    firm's query is byte-identical except for its own domain."""
    by_slug = _all_configured_firm_queries()
    cfg = _configured_cfg()
    acme_query = by_slug["acmesearch"][0].replace("acmesearch.example", "{domain}")
    for slug, queries in by_slug.items():
        domain = next(f.domain for f in cfg.search_firms if f.slug == slug)
        assert queries[0].replace(domain, "{domain}") == acme_query


# ---------------------------------------------------------------------------
# (a) Real default-search integration path, no live network (MockTransport
# canned fixtures reproducing the confirmed-live Jina response shapes).
# ---------------------------------------------------------------------------


def _confirmed_no_results_response(query: str) -> httpx.Response:
    """Reproduces the exact shape confirmed live 2026-09-07 (see commit
    1b08b38): HTTP 422, body message "No search results available for
    query ...". This is what Jina returns when a narrow site: query matches
    nothing -- not a malformed-request error."""
    body = {
        "code": 422,
        "status": 40206,
        "message": f'No search results available for query "{query}"',
    }
    return httpx.Response(422, json=body)


def _genuine_bad_request_response() -> httpx.Response:
    """A 422 that is NOT the no-results shape -- must still be treated as a
    real failure, or a truly malformed query would go unnoticed forever."""
    return httpx.Response(422, json={"code": 422, "status": 40001, "message": "Invalid request"})


@pytest.mark.asyncio
async def test_confirmed_zero_result_422_via_real_default_search_is_not_a_query_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        query = httpx.QueryParams(request.url.query)["q"]
        return _confirmed_no_results_response(query)

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)

    a = sf.SearchFirmAdapter(
        firms=[FABRIKAM_SEARCH],
        queries=_QUERIES,
        regions=_REGIONS,
        api_key="fake-key",
    )
    out = await a.fetch()
    assert out == []
    (report,) = a.firm_reports
    assert report.errors == 0
    assert report.kept == 0
    assert report.silent is True


@pytest.mark.asyncio
async def test_genuine_non_no_results_422_via_real_default_search_still_counts_as_error(
    monkeypatch,
):
    """Contrast case: we must not have overcorrected into swallowing every
    422 -- only the confirmed "no results" shape is silent."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _genuine_bad_request_response()

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)

    a = sf.SearchFirmAdapter(
        firms=[FABRIKAM_SEARCH],
        queries=_QUERIES,
        regions=_REGIONS,
        api_key="fake-key",
    )
    out = await a.fetch()
    assert out == []
    (report,) = a.firm_reports
    assert report.errors > 0


# ---------------------------------------------------------------------------
# (b) dead_firms — make a silent zero-yield loud.
# ---------------------------------------------------------------------------


def _report(
    slug: str, kept: int, queries: int = 1, results: int = 0, errors: int = 0
) -> sf.FirmReport:
    return sf.FirmReport(
        slug=slug,
        name=slug,
        source=sf.source_for(slug),
        queries=queries,
        results=results,
        kept=kept,
        errors=errors,
    )


def test_dead_firms_flags_firm_silent_for_threshold_consecutive_runs():
    history = {
        "fabrikamsearch": [
            _report("fabrikamsearch", 0),
            _report("fabrikamsearch", 0),
            _report("fabrikamsearch", 0),
        ]
    }
    assert sf.dead_firms(history, threshold=3) == ["fabrikamsearch"]


def test_dead_firms_ignores_firm_with_fewer_than_threshold_runs():
    history = {"fabrikamsearch": [_report("fabrikamsearch", 0), _report("fabrikamsearch", 0)]}
    assert sf.dead_firms(history, threshold=3) == []


def test_dead_firms_not_flagged_if_any_recent_run_kept_something():
    history = {
        "acmesearch": [
            _report("acmesearch", 0),
            _report("acmesearch", 1),
            _report("acmesearch", 0),
        ]
    }
    assert sf.dead_firms(history, threshold=3) == []


def test_dead_firms_only_looks_at_the_most_recent_window():
    """A firm that used to work but has gone silent for the last 3 runs is
    still flagged even though an older run (outside the window) kept one."""
    history = {
        "acmesearch": [
            _report("acmesearch", 1),
            _report("acmesearch", 0),
            _report("acmesearch", 0),
            _report("acmesearch", 0),
        ]
    }
    assert sf.dead_firms(history, threshold=3) == ["acmesearch"]


def test_dead_firms_evaluates_multiple_firms_independently():
    history = {
        "fabrikamsearch": [_report("fabrikamsearch", 0)] * 3,
        "acmesearch": [_report("acmesearch", 1)] * 3,
    }
    assert sf.dead_firms(history, threshold=3) == ["fabrikamsearch"]


# ---------------------------------------------------------------------------
# (b) SearchFirmAdapter surfaces dead_firms when given run history.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_computes_dead_firms_when_history_supplied(caplog):
    import logging

    async def empty_search(query: str, api_key: str):
        return []

    history = {"fabrikamsearch": [_report("fabrikamsearch", 0), _report("fabrikamsearch", 0)]}
    a = sf.SearchFirmAdapter(
        firms=[FABRIKAM_SEARCH],
        queries=["chief operations officer"],
        regions=["Ohio"],
        search=empty_search,
        history=history,
        dead_threshold=3,
    )
    with caplog.at_level(logging.WARNING):
        await a.fetch()
    assert a.dead_firms == ["fabrikamsearch"]
    assert "fabrikamsearch" in caplog.text and "zero" in caplog.text.lower()


@pytest.mark.asyncio
async def test_adapter_dead_firms_empty_without_history():
    async def empty_search(query: str, api_key: str):
        return []

    a = sf.SearchFirmAdapter(
        firms=[FABRIKAM_SEARCH],
        queries=["chief operations officer"],
        regions=["Ohio"],
        search=empty_search,
    )
    await a.fetch()
    assert a.dead_firms == []


@pytest.mark.asyncio
async def test_adapter_not_flagged_dead_when_this_runs_kept_breaks_the_streak():
    async def one_result_search(query: str, api_key: str):
        return [
            {
                "title": "Chief Operations Officer",
                "url": "https://www.fabrikamsearch.example/opportunities/1/chief-operations-officer",
                "description": "A retained search in Ohio.",
            }
        ]

    async def fake_reader(url: str):
        return "A longer description fetched from the firm's own site. " * 5

    history = {"fabrikamsearch": [_report("fabrikamsearch", 0), _report("fabrikamsearch", 0)]}
    a = sf.SearchFirmAdapter(
        firms=[FABRIKAM_SEARCH],
        queries=["chief operations officer"],
        regions=["Ohio"],
        search=one_result_search,
        reader=fake_reader,
        history=history,
        dead_threshold=3,
    )
    await a.fetch()
    assert a.dead_firms == []

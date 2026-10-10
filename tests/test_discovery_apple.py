"""Pure tests for the Apple careers adapter, plus an adapter test with an
injected Jina reader — no network."""

from __future__ import annotations

import pytest

from blastjob.discovery import apple

_SEARCH_MARKDOWN = """
# Search results

[Director, IS&T Business Process Reengineering](https://jobs.apple.com/en-us/details/200682798-0836/director-ist?team=SFTWR-SFTWR)
[See full role description](https://jobs.apple.com/en-us/details/200682798-0836/director-ist?team=SFTWR-SFTWR)
[Where we're hiring](https://jobs.apple.com/en-us/locationPicker)
[Director, Infrastructure](https://jobs.apple.com/en-us/details/200700111-0912/director-infrastructure?team=HRDWR-HRDWR)
"""

_SPLIT_LOCATION_MARKDOWN = """
[Director, IS&T Business Process Reengineering](https://jobs.apple.com/en-us/details/200682798-0836/director-ist?team=SFTWR-SFTWR)
[Director, IS&T Business Process Reengineering (Remote)](https://jobs.apple.com/en-us/details/200682798-0912/director-ist?team=SFTWR-SFTWR)
"""


# ---------------------------------------------------------------------------
# parse_search_links
# ---------------------------------------------------------------------------


def test_parse_search_links_extracts_title_and_id():
    links = apple.parse_search_links(_SEARCH_MARKDOWN)
    ids = {item["id"] for item in links}
    assert "200682798-0836" in ids
    assert "200700111-0912" in ids
    titles = {item["id"]: item["title"] for item in links}
    assert titles["200682798-0836"] == "Director, IS&T Business Process Reengineering"


def test_parse_search_links_drops_boilerplate_and_location_picker():
    links = apple.parse_search_links(_SEARCH_MARKDOWN)
    titles = {item["title"] for item in links}
    urls = {item["url"] for item in links}
    assert "See full role description" not in titles
    assert "Where we're hiring" not in titles
    assert not any(u.endswith("/locationPicker") for u in urls)


def test_parse_search_links_keeps_distinct_location_suffixes_as_two_postings():
    links = apple.parse_search_links(_SPLIT_LOCATION_MARKDOWN)
    ids = [item["id"] for item in links]
    assert ids == ["200682798-0836", "200682798-0912"]


def test_parse_search_links_dedupes_by_id_first_title_wins():
    md = (
        "[First Title](https://jobs.apple.com/en-us/details/1-1/first-title)\n"
        "[Second Title](https://jobs.apple.com/en-us/details/1-1/second-title)\n"
    )
    links = apple.parse_search_links(md)
    assert len(links) == 1
    assert links[0]["title"] == "First Title"


# ---------------------------------------------------------------------------
# build_search_url
# ---------------------------------------------------------------------------


def test_build_search_url_quotes_query_and_location():
    url = apple.build_search_url("IS&T director", "united-states-USA")
    assert url.startswith("https://jobs.apple.com/en-us/search?search=")
    assert "IS%26T" in url
    assert "location=united-states-USA" in url


# ---------------------------------------------------------------------------
# extract_location
# ---------------------------------------------------------------------------


def test_extract_location_finds_labeled_line():
    text = "Some intro text.\nLocation: Cupertino, California, United States\nMore text."
    assert apple.extract_location(text) == "Cupertino, California, United States"


def test_extract_location_empty_when_no_match():
    assert apple.extract_location("No location info here.") == ""


# ---------------------------------------------------------------------------
# AppleAdapter
# ---------------------------------------------------------------------------


def _adapter(**kwargs):
    calls: list[str] = []

    async def fake_reader(url: str) -> str:
        calls.append(url)
        if "/search?" in url:
            return _SEARCH_MARKDOWN
        return "Location: Cupertino, California, United States\n\nFull job description text."

    defaults = dict(
        queries=["IS&T director"],
        locations=["united-states-USA"],
        reader=fake_reader,
    )
    defaults.update(kwargs)
    adapter = apple.AppleAdapter(**defaults)
    return adapter, calls


@pytest.mark.asyncio
async def test_adapter_parses_postings_as_raw_postings_with_apple_source():
    a, _ = _adapter()
    out = await a.fetch()
    assert len(out) == 2
    for p in out:
        assert p.source == "apple"
        assert p.company == "Apple"
    ids = {p.external_id for p in out}
    assert ids == {"200682798-0836", "200700111-0912"}


@pytest.mark.asyncio
async def test_adapter_fetches_detail_jd_for_new_postings():
    a, calls = _adapter()
    out = await a.fetch()
    for p in out:
        assert "Full job description text" in p.description
    assert any("/details/" in c for c in calls)


@pytest.mark.asyncio
async def test_adapter_skips_detail_fetch_for_known_ids():
    known = {"200682798-0836"}
    a, calls = _adapter(known_ids=known)
    out = await a.fetch()

    detail_calls = [c for c in calls if "/details/" in c and "200682798-0836" in c]
    assert detail_calls == []

    known_posting = next(p for p in out if p.external_id == "200682798-0836")
    assert known_posting.description == ""

    other_posting = next(p for p in out if p.external_id == "200700111-0912")
    assert "Full job description text" in other_posting.description


@pytest.mark.asyncio
async def test_adapter_tolerates_a_failed_query_and_keeps_other_results():
    async def flaky_reader(url: str) -> str:
        if "search=bad" in url:
            raise RuntimeError("boom")
        if "/search?" in url:
            return _SEARCH_MARKDOWN
        return "Full job description text."

    a = apple.AppleAdapter(
        queries=["bad query", "IS&T director"],
        locations=["united-states-USA"],
        reader=flaky_reader,
    )
    out = await a.fetch()
    assert len(out) == 2


def test_adapter_is_incremental():
    assert apple.AppleAdapter.snapshot is False


@pytest.mark.asyncio
async def test_adapter_raises_without_key_or_injected_reader():
    a = apple.AppleAdapter(queries=["IS&T director"], locations=["united-states-USA"])
    with pytest.raises(RuntimeError, match="JINA_API_KEY"):
        await a.fetch()


# --- the search read must ask Jina for markdown, and must not use the JD cap ---
#
# Regression (2026-09-10): the reader was copied from searchfirms.py, which sends
# `X-Return-Format: text` and truncates to 6000 chars. Both are right for a job
# description and fatal for the search page: text format strips the `[title](url)`
# links this adapter parses, and a rendered search page is ~50KB, so the JD cap
# threw away most of the results. A live fetch returned 0 postings while every
# fake-based test passed, because the fakes feed markdown regardless of headers.


def test_default_reader_requests_markdown_for_search_and_text_for_detail():
    import asyncio

    import httpx

    from blastjob.discovery import apple

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(200, text="x" * 300_000)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def fake_client(*a, **k):
        k["transport"] = transport
        return real_client(*a, **k)

    apple.httpx.AsyncClient = fake_client  # type: ignore[misc]
    try:
        search = asyncio.run(
            apple._default_reader(
                "https://jobs.apple.com/en-us/search?search=x",
                "k",
                markdown=True,
                max_chars=apple.MAX_SEARCH_CHARS,
            )
        )
        detail = asyncio.run(apple._default_reader("https://jobs.apple.com/en-us/details/1/x", "k"))
    finally:
        apple.httpx.AsyncClient = real_client  # type: ignore[misc]

    # Search: markdown (no text-format header), and not clipped to the JD cap.
    assert "x-return-format" not in seen[0]
    assert len(search) == apple.MAX_SEARCH_CHARS > apple.MAX_READER_CHARS
    # Detail: plain text, JD cap.
    assert seen[1].get("x-return-format") == "text"
    assert len(detail) == apple.MAX_READER_CHARS


def test_search_cap_is_large_enough_for_a_real_rendered_page():
    """A live Apple search page rendered to ~51KB of markdown on 2026-09-10."""
    from blastjob.discovery.apple import MAX_SEARCH_CHARS

    assert MAX_SEARCH_CHARS >= 100_000

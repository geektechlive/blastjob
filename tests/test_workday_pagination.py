"""Workday listing pagination: `total` may be reported on page 1 only. MockTransport, no network."""

from __future__ import annotations

import json

import httpx
import pytest

from blastjob.discovery import workday

PAGE_SIZE = 20


def _paged_transport(
    totals: list[int], sizes: list[int], seen_offsets: list[int]
) -> httpx.MockTransport:
    """Page i holds sizes[i] postings and reports total totals[i]; past the end: empty."""

    def handler(request: httpx.Request) -> httpx.Response:
        offset = json.loads(request.content)["offset"]
        seen_offsets.append(offset)
        page = offset // PAGE_SIZE
        if page >= len(sizes):
            return httpx.Response(200, json={"total": 0, "jobPostings": []})
        postings = [
            {
                "title": f"Director {offset + i}",
                "externalPath": f"/job/Springfield-Ohio-United-States/Director_R{offset + i}",
                "locationsText": "Springfield, Ohio, United States",
                "postedOn": "Posted Today",
                "bulletFields": [f"R{offset + i}"],
            }
            for i in range(sizes[page])
        ]
        return httpx.Response(200, json={"total": totals[page], "jobPostings": postings})

    return httpx.MockTransport(handler)


async def _list_all(transport: httpx.MockTransport, **kwargs: int) -> list[dict]:
    adapter = workday.WorkdayAdapter(
        "Acme",
        "acme/wd1/Careers",
        queries=["director"],
        page_size=PAGE_SIZE,
        transport=transport,
        **kwargs,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        return await adapter._list_all(client)


@pytest.mark.asyncio
async def test_paginates_when_total_is_only_on_the_first_page():
    offsets: list[int] = []
    out = await _list_all(_paged_transport([50, 0, 0], [20, 20, 10], offsets))
    assert len(out) == 50
    assert offsets == [0, 20, 40]


@pytest.mark.asyncio
async def test_paginates_when_every_page_reports_total():
    offsets: list[int] = []
    out = await _list_all(_paged_transport([50, 50, 50], [20, 20, 10], offsets))
    assert len(out) == 50
    assert offsets == [0, 20, 40]


@pytest.mark.asyncio
async def test_stops_on_an_empty_page():
    offsets: list[int] = []
    out = await _list_all(_paged_transport([0, 0, 0], [20, 0, 20], offsets))
    assert len(out) == 20
    assert offsets == [0, 20]


@pytest.mark.asyncio
async def test_stops_at_the_known_total_on_a_full_last_page():
    offsets: list[int] = []
    out = await _list_all(_paged_transport([40, 40, 40], [20, 20, 20], offsets))
    assert len(out) == 40
    assert offsets == [0, 20]


@pytest.mark.asyncio
async def test_respects_the_page_cap_when_total_is_never_reported():
    offsets: list[int] = []
    out = await _list_all(_paged_transport([0] * 10, [20] * 10, offsets), max_pages=3)
    assert len(out) == 60
    assert offsets == [0, 20, 40]

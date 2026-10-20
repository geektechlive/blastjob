"""An Adzuna pass reads the API only; it never fetches adzuna.com details pages.

Those pages sit behind a human check blastjob never tries to pass. Full
descriptions come from the employer's posting (docs/how-it-works/employer-data.md).
"""

from __future__ import annotations

import httpx

from blastjob.discovery import adzuna, enrich

PAYLOAD = {
    "results": [
        {
            "id": "az-1",
            "title": "Operations Director",
            "company": {"display_name": "Acme Inc"},
            "location": {"display_name": "Springfield, OH", "area": ["US", "Ohio", "Springfield"]},
            "redirect_url": "https://www.adzuna.com/details/az-1",
            "description": "Lead Ops.",
        }
    ]
}


async def test_adzuna_pass_fetches_no_adzuna_page(monkeypatch):
    # Arrange: the API call is faked; any other HTTP client use is recorded.
    async def fake_get_json(url, **kwargs):
        return PAYLOAD

    page_fetches: list[str] = []

    async def spy_enrich(*args, **kwargs):
        page_fetches.append("enrich_full_jds")
        return 0

    real_send = httpx.AsyncClient.send

    async def spy_send(self, request, **kwargs):
        page_fetches.append(str(request.url))
        return await real_send(self, request, **kwargs)

    monkeypatch.setattr(adzuna, "get_json", fake_get_json)
    monkeypatch.setattr(enrich, "enrich_full_jds", spy_enrich)
    monkeypatch.setattr(httpx.AsyncClient, "send", spy_send)
    monkeypatch.setenv("TEST_ADZ_ID", "id")
    monkeypatch.setenv("TEST_ADZ_KEY", "key")
    adapter = adzuna.AdzunaAdapter(
        queries=["cio"],
        where="Springfield",
        distance_km=50,
        salary_min=0,
        app_id_env="TEST_ADZ_ID",
        app_key_env="TEST_ADZ_KEY",
    )

    # Act
    postings = await adapter.fetch()

    # Assert
    assert [p.external_id for p in postings] == ["az-1"]
    assert page_fetches == []

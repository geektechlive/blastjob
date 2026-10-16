"""enrich.py fetches posting pages whose URLs come from feeds and alert emails,
so it must not follow a redirect (or a name) into the machine's own network.
Every hop is validated before it is requested; no raw follow_redirects client.
"""

from __future__ import annotations

import httpx
import pytest

from blastjob.discovery import enrich
from blastjob.discovery.base import RawPosting

JD = "Own IT strategy. " * 60
PAGE = (
    '<script type="application/ld+json">{"@type":"JobPosting","description":"' + JD + '"}</script>'
)
PUBLIC = ["93.184.216.34"]


def _posting(url: str) -> RawPosting:
    return RawPosting(source="adzuna", external_id="1", url=url, description="short")


async def _resolve_public(_host: str) -> list[str]:
    return PUBLIC


def _transport(handler, seen: list[str]):
    def wrapped(req: httpx.Request) -> httpx.Response:
        # Requests go to the pinned address; the Host header keeps the name.
        seen.append(str(req.url.copy_with(host=req.headers["host"])))
        return handler(req)

    return httpx.MockTransport(wrapped)


@pytest.mark.asyncio
async def test_public_page_upgrades_the_snippet_including_aggregator_hosts():
    seen: list[str] = []
    p = _posting("https://www.adzuna.com/details/1")
    n = await enrich.enrich_full_jds(
        [p],
        transport=_transport(lambda r: httpx.Response(200, text=PAGE), seen),
        resolve=_resolve_public,
    )
    assert n == 1 and p.description.startswith("Own IT strategy")


@pytest.mark.asyncio
async def test_redirect_to_loopback_is_never_requested():
    seen: list[str] = []

    def handler(req):
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8765/secret"})

    p = _posting("https://www.adzuna.com/details/1")
    n = await enrich.enrich_full_jds(
        [p], transport=_transport(handler, seen), resolve=_resolve_public
    )
    assert n == 0 and p.description == "short"
    assert seen == ["https://www.adzuna.com/details/1"]


@pytest.mark.asyncio
async def test_name_resolving_to_a_private_address_is_never_requested():
    seen: list[str] = []

    async def resolve(_host):
        return ["10.0.0.5"]

    p = _posting("https://internal.example.com/job")
    await enrich.enrich_full_jds(
        [p], transport=_transport(lambda r: httpx.Response(200, text=PAGE), seen), resolve=resolve
    )
    assert seen == [] and p.description == "short"


@pytest.mark.asyncio
async def test_linkedin_is_never_requested():
    seen: list[str] = []
    p = _posting("https://www.linkedin.com/jobs/view/1")
    await enrich.enrich_full_jds(
        [p],
        transport=_transport(lambda r: httpx.Response(200, text=PAGE), seen),
        resolve=_resolve_public,
    )
    assert seen == []


@pytest.mark.asyncio
async def test_non_http_scheme_and_ip_literals_are_refused():
    seen: list[str] = []
    ps = [_posting("file:///etc/passwd"), _posting("http://169.254.169.254/latest")]
    await enrich.enrich_full_jds(
        ps,
        transport=_transport(lambda r: httpx.Response(200, text=PAGE), seen),
        resolve=_resolve_public,
    )
    assert seen == []


@pytest.mark.asyncio
async def test_redirect_loop_is_bounded():
    seen: list[str] = []

    def handler(req):
        return httpx.Response(302, headers={"location": "https://www.adzuna.com/again"})

    p = _posting("https://www.adzuna.com/details/1")
    await enrich.enrich_full_jds([p], transport=_transport(handler, seen), resolve=_resolve_public)
    assert len(seen) <= 6 and p.description == "short"


@pytest.mark.asyncio
async def test_enrich_connects_to_the_address_it_validated():
    """No second DNS lookup decides where the request goes (DNS rebinding): the
    request is made to the validated address, with the original Host header."""
    hits: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append((req.url.host, req.headers.get("host", "")))
        return httpx.Response(200, text=PAGE)

    p = _posting("https://www.adzuna.com/details/1")
    await enrich.enrich_full_jds(
        [p], transport=httpx.MockTransport(handler), resolve=_resolve_public
    )
    assert hits == [(PUBLIC[0], "www.adzuna.com")]

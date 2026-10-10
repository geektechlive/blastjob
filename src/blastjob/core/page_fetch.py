"""The network side of employer-page links (core/page_links): the only two
functions there that reach out, `safe_get` and `jina_search`.

`safe_get` follows redirects by hand and validates EVERY hop before requesting
it: http/https only, no aggregator host (linkedin.com is on that list and is
never contacted), and a host that resolves only to public
addresses, so a careers page cannot bounce us onto LinkedIn or into the LAN.
Bodies are capped. A refused hop is no page, never an error.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel

AGGREGATOR_HOSTS: frozenset[str] = frozenset(
    {
        "linkedin.com", "indeed.com", "glassdoor.com", "glassdoor.co.uk", "ziprecruiter.com",
        "adzuna.com", "jooble.org", "simplyhired.com", "monster.com", "careerbuilder.com",
        "bebee.com", "lensa.com", "talents.vaia.com", "vaia.com", "tealhq.com",
        "remoterocketship.com", "jobleads.com", "theladders.com", "ladders.com",
        "jobgether.com", "swooped.co", "dice.com", "builtin.com", "wellfound.com",
        "salary.com", "talent.com", "jobrapido.com", "snagajob.com", "careerjet.com",
        "whatjobs.com", "recruit.net", "jobilize.com", "ihire.com", "nexxt.com", "upward.net",
        "jobs.com", "google.com", "bing.com",
    }
)  # fmt: skip
MAX_HOPS = 5
MAX_BODY_BYTES = 2 * 1024 * 1024
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_TIMEOUT_S = 15
_SEARCH_TIMEOUT_S = 40
# Browser-like: several ATSs answer a bare client with a bot wall (403).
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


@dataclass(frozen=True)
class Page:
    status: int
    url: str  # final URL after redirects
    html: str
    cross_host: bool  # some redirect in the chain changed host


Fetch = Callable[[str], Awaitable[Page | None]]
Search = Callable[[str, str], Awaitable[list[str]]]
Resolve = Callable[[str], Awaitable[list[str]]]


def host_of(url: str) -> str:
    """Lower-cased host, trailing dot dropped ("linkedin.com." is linkedin.com)."""
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def is_aggregator_host(host: str) -> bool:
    labels = host.lower().rstrip(".").split(".")
    return any(".".join(labels[i:]) in AGGREGATOR_HOSTS for i in range(len(labels)))


def fetchable(url: str) -> bool:
    """http(s), a host, and not an aggregator (linkedin.com included). Parsed by
    httpx, the parser that sends the request, so the two cannot disagree."""
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError):
        return False
    host = parsed.host.lower().rstrip(".")
    return parsed.scheme in ("http", "https") and bool(host) and not is_aggregator_host(host)


# --- the network ---------------------------------------------------------------


async def _resolve_host(host: str) -> list[str]:
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except OSError:
        return []
    return [str(info[4][0]) for info in infos]


def _public_ip(text: str) -> bool:
    # `is_global` rather than a list of is_private/is_loopback/...: those miss
    # shared address space (100.64.0.0/10), where some clouds and VPNs put
    # metadata endpoints. Multicast is global for IPv6, hence the second test.
    ip = ipaddress.ip_address(text.split("%")[0])
    return ip.is_global and not ip.is_multicast


async def _hop_ok(url: str, resolve: Resolve) -> bool:
    if not fetchable(url):
        return False
    host = httpx.URL(url).host.lower().rstrip(".")
    try:
        return _public_ip(host)
    except ValueError:
        pass  # a name, not an address literal
    addresses = await resolve(host)
    return bool(addresses) and all(_public_ip(a) for a in addresses)


async def safe_get(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolve: Resolve | None = None,
) -> Page | None:
    """GET with redirects followed by hand, each hop validated before it is
    requested (scheme, aggregator/linkedin, public address). None when a hop
    is refused or there are more than MAX_HOPS redirects."""
    first_host, cross, current = host_of(url), False, url
    headers = {"User-Agent": _BROWSER_UA, "Accept": "text/html,application/xhtml+xml"}
    async with httpx.AsyncClient(timeout=_TIMEOUT_S, transport=transport) as client:
        for _hop in range(MAX_HOPS + 1):
            if not await _hop_ok(current, resolve or _resolve_host):
                return None
            async with client.stream("GET", current, headers=headers) as resp:
                location = resp.headers.get("location", "")
                if resp.status_code in _REDIRECTS and location:
                    current = urljoin(current, location)
                    cross = cross or host_of(current) != first_host
                    continue
                body = await _capped_body(resp)
                return Page(resp.status_code, current, body, cross)
    return None


async def _capped_body(resp: httpx.Response) -> str:
    chunks, size = [], 0
    async for chunk in resp.aiter_bytes():
        chunks.append(chunk[: MAX_BODY_BYTES - size])
        size += len(chunks[-1])
        if size >= MAX_BODY_BYTES:
            break
    return b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")


class _JinaHit(BaseModel):
    url: str = ""


class _JinaResults(BaseModel):
    data: list[_JinaHit] = []


async def jina_search(
    query: str, jina_api_key: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> list[str]:
    """Result URLs for one Jina search (headers as resolve.probe_workday)."""
    headers = {
        "Authorization": f"Bearer {jina_api_key}",
        "Accept": "application/json",
        "X-Respond-With": "no-content",
    }
    async with httpx.AsyncClient(timeout=_SEARCH_TIMEOUT_S, transport=transport) as client:
        resp = await client.get("https://s.jina.ai/", params={"q": query}, headers=headers)
    if resp.status_code != httpx.codes.OK:
        return []
    try:
        results = _JinaResults.model_validate(resp.json())
    except ValueError:
        return []
    return [hit.url for hit in results.data if hit.url]

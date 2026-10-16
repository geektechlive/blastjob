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
        "jobs.com", "google.com", "bing.com", "lnkd.in", "linkedin.cn",
    }
)  # fmt: skip
# Never contacted from any code path, even by a caller that allows aggregators.
LINKEDIN_HOSTS: frozenset[str] = frozenset({"linkedin.com", "lnkd.in", "linkedin.cn"})
MAX_HOPS = 5
# Connections are pooled by (scheme, host, port) and the host is now the pinned IP, so a
# kept-alive TLS session for one name could carry a request for another name on the
# same IP without its certificate being checked. Never reuse a connection.
_NO_REUSE = httpx.Limits(max_keepalive_connections=0)
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


def _in_hosts(host: str, hosts: frozenset[str]) -> bool:
    labels = host.lower().rstrip(".").split(".")
    return any(".".join(labels[i:]) in hosts for i in range(len(labels)))


def is_aggregator_host(host: str) -> bool:
    return _in_hosts(host, AGGREGATOR_HOSTS)


def fetchable(url: str, *, allow_aggregators: bool = False) -> bool:
    """http(s), a host, and not an aggregator (linkedin.com included). Parsed by
    httpx, the parser that sends the request, so the two cannot disagree.
    `allow_aggregators` admits job boards (enrich.py reads Adzuna/Indeed pages)
    but never LinkedIn."""
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError):
        return False
    host = parsed.host.lower().rstrip(".")
    refused = LINKEDIN_HOSTS if allow_aggregators else AGGREGATOR_HOSTS
    return parsed.scheme in ("http", "https") and bool(host) and not _in_hosts(host, refused)


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


async def _connect_addresses(
    url: str, resolve: Resolve, *, allow_aggregators: bool = False
) -> list[str] | None:
    """The public addresses this hop may be requested at (all of them validated,
    in resolver order), or None when the hop is refused. The caller connects to
    one of THESE, so a second DNS lookup (which could answer differently:
    rebinding) never decides where the request goes. Trying the rest on a
    connect failure keeps a dead first A/AAAA record from failing the hop."""
    if not fetchable(url, allow_aggregators=allow_aggregators):
        return None
    host = httpx.URL(url).host.lower().rstrip(".")
    try:
        return [host] if _public_ip(host) else None
    except ValueError:
        pass  # a name, not an address literal
    addresses = [a.split("%")[0] for a in await resolve(host)]
    if not addresses or not all(_public_ip(a) for a in addresses):
        return None
    return list(dict.fromkeys(addresses))


def _pinned(url: str, address: str) -> tuple[httpx.URL, dict[str, str], dict[str, str]]:
    """(URL at `address`, Host header, request extensions) for one hop. The Host
    header and the TLS SNI/certificate check keep the ORIGINAL name, so the
    server and its certificate see exactly what an unpinned request would."""
    parsed = httpx.URL(url)
    host = parsed.host
    default_port = {"http": 80, "https": 443}.get(parsed.scheme)
    host_header = host if parsed.port in (None, default_port) else f"{host}:{parsed.port}"
    if ":" in host:  # IPv6 literal
        host_header = f"[{host}]" + host_header[len(host) :]
    name = host.rstrip(".")
    if address == host.lower().rstrip("."):
        return parsed, {"Host": host_header}, {}
    return parsed.copy_with(host=address), {"Host": host_header}, {"sni_hostname": name}


async def safe_get(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolve: Resolve | None = None,
    allow_aggregators: bool = False,
    timeout_s: float = _TIMEOUT_S,
) -> Page | None:
    """GET with redirects followed by hand, each hop validated before it is
    requested (scheme, aggregator/linkedin, public address). None when a hop
    is refused or there are more than MAX_HOPS redirects. `allow_aggregators`
    admits job boards on every hop; LinkedIn is refused regardless.

    Each hop connects to the address that was validated (no second lookup), with
    the original Host header and TLS name. `trust_env=False`: no proxy or CA
    override from the environment may reroute a validated fetch."""
    first_host, cross, current = host_of(url), False, url
    headers = {"User-Agent": _BROWSER_UA, "Accept": "text/html,application/xhtml+xml"}
    async with httpx.AsyncClient(
        timeout=timeout_s, transport=transport, trust_env=False, limits=_NO_REUSE
    ) as client:
        for _hop in range(MAX_HOPS + 1):
            addresses = await _connect_addresses(
                current, resolve or _resolve_host, allow_aggregators=allow_aggregators
            )
            if addresses is None:
                return None
            outcome = await _get_hop(client, current, addresses, headers)
            if isinstance(outcome, _Redirect):
                current = urljoin(current, outcome.location)
                cross = cross or host_of(current) != first_host
                continue
            return Page(outcome[0], current, outcome[1], cross)
    return None


@dataclass(frozen=True)
class _Redirect:
    location: str


async def _get_hop(
    client: httpx.AsyncClient, url: str, addresses: list[str], headers: dict[str, str]
) -> _Redirect | tuple[int, str]:
    """One hop: (status, body) or a redirect. A failed connect moves on to the
    next validated address; the last failure is raised."""
    for i, address in enumerate(addresses):
        target, host_headers, extensions = _pinned(url, address)
        try:
            async with client.stream(
                "GET", target, headers={**headers, **host_headers}, extensions=extensions
            ) as resp:
                location = resp.headers.get("location", "")
                if resp.status_code in _REDIRECTS and location:
                    return _Redirect(location)
                return resp.status_code, await _capped_body(resp)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if i == len(addresses) - 1:
                raise
    raise AssertionError("unreachable: addresses is never empty")


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

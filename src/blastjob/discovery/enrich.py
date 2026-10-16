"""Fetch the full JD for aggregator postings.

Adzuna's API returns only a ~500-char snippet, but its details page embeds the
complete description as schema.org JSON-LD (JobPosting.description). We parse that
structured data — reliable, not HTML scraping — so matching scores on the full
posting instead of a snippet. Fail-soft: on any problem the snippet is kept.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable

import httpx

from blastjob.core.page_fetch import safe_get
from blastjob.discovery.base import RawPosting
from blastjob.discovery.textutil import html_to_text

log = logging.getLogger(__name__)

_LDJSON = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)


def extract_jobposting_jd(html: str) -> str | None:
    """Return the full plain-text JD from a page's JSON-LD JobPosting, or None."""
    for block in _LDJSON.findall(html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if "JobPosting" in str(item.get("@type", "")):
                desc = item.get("description")
                if isinstance(desc, str) and desc.strip():
                    text = html_to_text(desc)
                    if text:
                        return text
    return None


# Posting URLs come from feeds and alert emails, i.e. from outside, so every fetch
# goes through core/page_fetch.safe_get: each hop is validated before it is
# requested (http/https, public addresses only, never LinkedIn), redirects are
# followed by hand, the connection is made to the address that was validated (no
# DNS-rebinding window) and the body is capped. Adzuna and Indeed pages are the
# whole point here, so aggregators are allowed (`allow_aggregators=True`).
Resolve = Callable[[str], Awaitable[list[str]]]


async def _fetch_one(
    posting: RawPosting,
    *,
    timeout_s: float,
    transport: httpx.AsyncBaseTransport | None,
    resolve: Resolve | None,
) -> None:
    try:
        page = await safe_get(
            posting.url,
            transport=transport,
            resolve=resolve,
            allow_aggregators=True,
            timeout_s=timeout_s,
        )
        if page is not None and page.status == 200:
            full = extract_jobposting_jd(page.html)
            # Only replace if we got materially more than the snippet.
            if full and len(full) > len(posting.description):
                posting.description = full
    except Exception as e:  # noqa: BLE001 — keep the snippet on any failure
        # Fail-soft, but not silent: a truncated JD means matching scores on the
        # ~500-char snippet, so leave a trace of which posting fell back.
        log.debug("full-JD enrich failed for %s: %s", posting.url, e)


async def enrich_full_jds(
    postings: list[RawPosting],
    *,
    timeout_s: int = 15,
    concurrency: int = 8,
    transport: httpx.AsyncBaseTransport | None = None,
    resolve: Resolve | None = None,
) -> int:
    """Enrich postings in place with their full JD. Returns how many were upgraded.
    Only postings with a URL are attempted; others are left untouched."""
    targets = [p for p in postings if p.url]
    if not targets:
        return 0
    before = {id(p): len(p.description) for p in targets}
    sem = asyncio.Semaphore(concurrency)

    async def guarded(p: RawPosting) -> None:
        async with sem:
            await _fetch_one(p, timeout_s=timeout_s, transport=transport, resolve=resolve)

    await asyncio.gather(*(guarded(p) for p in targets))
    return sum(1 for p in targets if len(p.description) > before[id(p)])

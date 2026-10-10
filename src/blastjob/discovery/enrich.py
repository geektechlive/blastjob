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

import httpx

from blastjob.discovery.base import RawPosting
from blastjob.discovery.http import USER_AGENT
from blastjob.discovery.textutil import html_to_text

log = logging.getLogger(__name__)

_LDJSON = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
# A browser-like UA — some detail pages 403 a bot UA.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)


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


async def _fetch_one(client: httpx.AsyncClient, posting: RawPosting) -> None:
    try:
        r = await client.get(posting.url)
        if r.status_code == 200:
            full = extract_jobposting_jd(r.text)
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
) -> int:
    """Enrich postings in place with their full JD. Returns how many were upgraded.
    Only postings with a URL are attempted; others are left untouched."""
    targets = [p for p in postings if p.url]
    if not targets:
        return 0
    before = {id(p): len(p.description) for p in targets}
    sem = asyncio.Semaphore(concurrency)
    headers = {"User-Agent": _BROWSER_UA or USER_AGENT}

    async with httpx.AsyncClient(
        timeout=timeout_s, follow_redirects=True, headers=headers
    ) as client:

        async def guarded(p: RawPosting) -> None:
            async with sem:
                await _fetch_one(client, p)

        await asyncio.gather(*(guarded(p) for p in targets))

    return sum(1 for p in targets if len(p.description) > before[id(p)])

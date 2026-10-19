"""Jina web search + page reader, shared by company research in `core/` and `web/`.

Moved here from `web/people.py` (search) and `web/research.py` (reader, page
picking, query shape) so that packet research in `core/build.py` can use them
without `core/` importing `web/`. Both web modules re-export these names, so
their callers and tests are unchanged.

Not to be confused with `core/page_fetch.jina_search`, which returns bare URLs
for the closure trust checks and goes through the safe-fetch transport.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from blastjob.core.page_fetch import LINKEDIN_HOSTS, _in_hosts, host_of
from blastjob.models.config import CandidateConfig

log = logging.getLogger(__name__)

JINA_KEY_ENV = "JINA_API_KEY"
SEARCH_URL = "https://s.jina.ai/"
READER_URL = "https://r.jina.ai/"
MAX_RESULTS_PER_QUERY = 8
MAX_PAGES = 4
PAGE_CHARS = 6000
_PREFER = re.compile(
    r"about|leadership|news|press|annual|10-k|form-990|propublica|ibanknet|ncua|sec\.gov|"
    r"wikipedia|bizjournals|prnewswire|glassdoor",
    re.I,
)

_NO_RESULTS_MESSAGE = "no search results available"


def _is_no_results(response: httpx.Response) -> bool:
    """True when Jina's 422 means "that query matched nothing".

    Confirmed live 2026-09-07: `site:example-search-firm.com (<titles>)
    (<regions>)` answers 422 / AssertionFailureError with message "No search
    results available for query ...", while the same query against a
    different search-firm domain answers 200. A narrow site: query matching
    nothing is an ordinary outcome, not a failure.
    """
    try:
        message = str(response.json().get("message") or "")
    except ValueError:
        message = response.text
    return _NO_RESULTS_MESSAGE in message.lower()


async def jina_search(query: str, api_key: str, *, timeout_s: int = 30) -> list[dict]:
    """[{title, url, description}] from Jina's search endpoint (no page content).

    A zero-hit search comes back as HTTP 422, so it is normalized to [] here
    rather than raised — otherwise every run logs a WARNING and the daily digest
    reports a per-source error for a search that simply found nothing.
    """
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.get(
            SEARCH_URL,
            params={"q": query},
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "X-Respond-With": "no-content",
            },
        )
        if r.status_code == 422 and _is_no_results(r):
            return []
        r.raise_for_status()
        data = r.json().get("data") or []
    out = []
    for d in data[:MAX_RESULTS_PER_QUERY]:
        if isinstance(d, dict) and d.get("url"):
            out.append(
                {
                    "title": str(d.get("title") or "")[:200],
                    "url": str(d.get("url")),
                    "description": str(d.get("description") or "")[:400],
                }
            )
    return out


def company_search_queries(company: str, candidate: CandidateConfig | None = None) -> list[str]:
    """Query shape derived from the candidate's own field/target titles
    (§2 Persona), not a hard-coded role. With no profile configured, the
    leadership-team query falls back to a neutral "technology" term.

    Ordered most-useful first: packet research takes only the first few."""
    c = f'"{company}"'
    field = candidate.field.strip() if candidate and candidate.field.strip() else "technology"
    return [
        f"{c} company revenue employees headquarters",
        f"{c} CEO leadership team",
        f"{c} news 2026",
        f"{c} acquisition OR expansion OR layoffs OR restructuring",
        f"{c} glassdoor reviews culture",
        f"{c} {field} leadership cloud ERP digital transformation",
        f'{c} annual report OR 10-K OR "Form 990" OR "call report"',
    ]


def is_linkedin_url(url: str) -> bool:
    """True when the URL's parsed host is LinkedIn or a subdomain of it. Never a
    substring test: notlinkedin.example is not LinkedIn."""
    return _in_hosts(host_of(url), LINKEDIN_HOSTS)


def pick_pages(results: list[dict], limit: int = MAX_PAGES) -> list[dict]:
    """Official/primary-looking URLs first, then the rest, deduped by host+path.
    LinkedIn pages are never picked: company research does not read them."""
    seen: set[str] = set()
    kept = [r for r in results if not is_linkedin_url(r["url"])]
    ranked = sorted(kept, key=lambda r: 0 if _PREFER.search(r["url"]) else 1)
    out = []
    for r in ranked:
        key = re.sub(r"[?#].*$", "", r["url"].lower())
        if key in seen or key.endswith((".pdf",)):
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= limit:
            break
    return out


async def read_page_json(client: httpx.AsyncClient, url: str, api_key: str) -> dict[str, Any]:
    """The reader's JSON form: {"text", "published", "metadata"}, text capped at
    `PAGE_CHARS`; {} on any failure. Same text as `read_page`, plus the page's
    `publishedTime` and meta tags, which packet research dates values with."""
    try:
        r = await client.get(
            f"{READER_URL}{url}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Return-Format": "text",
                "Accept": "application/json",
            },
        )
        if r.status_code != 200:
            return {}
        data = r.json().get("data") or {}
        text = str(data.get("text") or data.get("content") or "")
        metadata = data.get("metadata")
        return {
            "text": re.sub(r"\n{3,}", "\n\n", text)[:PAGE_CHARS],
            "published": str(data.get("publishedTime") or ""),
            "metadata": metadata if isinstance(metadata, dict) else {},
        }
    except Exception as e:  # noqa: BLE001 — one page is never worth failing research
        log.debug("reader (json) failed for %s: %s", url, e)
        return {}


async def read_page(client: httpx.AsyncClient, url: str, api_key: str) -> str:
    """The page's text through Jina's reader, capped at `PAGE_CHARS`; "" on any
    failure (one page is never worth failing a workup)."""
    try:
        r = await client.get(
            f"{READER_URL}{url}",
            headers={"Authorization": f"Bearer {api_key}", "X-Return-Format": "text"},
        )
        if r.status_code != 200:
            return ""
        return re.sub(r"\n{3,}", "\n\n", r.text)[:PAGE_CHARS]
    except Exception as e:  # noqa: BLE001 — one page is never worth failing the workup
        log.debug("reader failed for %s: %s", url, e)
        return ""

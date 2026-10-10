"""Apple careers site adapter (jobs.apple.com), via the Jina reader.

Feasibility was probed 2026-09-10 (see docs/ARCHITECTURE.md) and
is NOT re-investigated here:

- Plain `GET jobs.apple.com/en-us/search` returns real HTML but the results are
  client-rendered — only one promoted role appears in the markup.
- `POST .../api/role/search` and `GET .../api/v1/search` both dead-end (redirect
  to a 404, or a bare 401).
- `r.jina.ai` reading the search URL renders the JS and returns real titles +
  detail links as markdown `[title](url)` pairs. That's the only working path,
  so this adapter is Jina-reader-only, exactly like `searchfirms.py` (the
  reference implementation for this module's shape) — except Apple needs the
  reader on a known search URL rather than `s.jina.ai` free-text search.

Design points:

- `source = "apple"`, `snapshot = False`: a search result page says nothing
  about what's been pulled, so the pipeline must never stale-mark on this
  adapter's behalf.
- `company` is always `"Apple"`.
- `external_id` is the FULL details id including the location suffix (e.g.
  `200682798-0836`), not the base requisition. Apple posts one requisition per
  location, and the suffix is the location — collapsing on the base id would
  erase the Cupertino-vs-remote distinction. Cross-source clustering (which is
  reversible/display-only) groups them back together if warranted; identity
  here must not.
- One search read per (query, location) pair via the Jina reader, capped at
  `MAX_SEARCH_READS` (6) per run. `location` is Apple's own location code
  (`united-states-USA`, `home-office-HOME` for remote); a posting found only
  under the `home-office-HOME` search is flagged remote.
- The search page yields titles + detail URLs only. The JD comes from a
  second reader call on the detail URL, done ONLY for ids not already in
  `known_ids` and capped at `max_reads` — identical discipline to
  `workday.py`'s detail-fetch skip. An empty description means "presence
  only" and must never blank a JD already on file (enforced upstream by
  `normalize.to_job_columns`, which omits `raw_jd` entirely when the adapter
  sent nothing).
"""

from __future__ import annotations

import itertools
import logging
import re
from collections.abc import Awaitable, Callable
from urllib.parse import quote

import httpx

from blastjob.discovery.base import RawPosting
from blastjob.secrets import secrets_file_hint

log = logging.getLogger(__name__)

SOURCE = "apple"
JINA_READER = "https://r.jina.ai/"
JINA_KEY_ENV = "JINA_API_KEY"
SEARCH_BASE = "https://jobs.apple.com/en-us/search"
MAX_READER_CHARS = 6000
# A rendered search page is ~50KB of markdown; the JD cap would truncate away most
# of the results. Search reads get their own, much larger cap.
MAX_SEARCH_CHARS = 200_000
MAX_SEARCH_READS = 6
DEFAULT_MAX_READS = 10
DEFAULT_TIMEOUT_S = 30

# Titles that show up in the rendered search markup but are chrome, not a
# posting (e.g. a "see more" link, a location-filter widget label).
_BOILERPLATE_TITLES = {"See full role description", "Where we're hiring"}

_LINK_RE = re.compile(r"\[([^\]]+)\]\((https://jobs\.apple\.com/en-us/[^)\s]+)\)")
_DETAIL_RE = re.compile(r"^https://jobs\.apple\.com/en-us/details/([\w-]+)/([a-z0-9-]+)")
_LOCATION_RE = re.compile(r"(?im)^\s*(?:location|based in)\s*[:\-]\s*(.+)\s*$")


def build_search_url(query: str, location: str) -> str:
    return f"{SEARCH_BASE}?search={quote(query)}&location={quote(location)}"


def parse_search_links(markdown: str) -> list[dict]:
    """Markdown `[title](url)` pairs -> [{id, slug, title, url}], filtered to
    real `/details/<id>/<slug>` postings and deduped by id (first title wins)."""
    out: list[dict] = []
    seen: set[str] = set()
    for title, url in _LINK_RE.findall(markdown or ""):
        title = title.strip()
        url = url.strip()
        if not title or title in _BOILERPLATE_TITLES:
            continue
        if url.rstrip("/").endswith("/locationPicker"):
            continue
        m = _DETAIL_RE.match(url)
        if not m:
            continue
        ext_id, slug = m.group(1), m.group(2)
        if ext_id in seen:
            continue
        seen.add(ext_id)
        out.append({"id": ext_id, "slug": slug, "title": title, "url": url})
    return out


def extract_location(text: str) -> str:
    """Best-effort location line from a detail-page reader dump. Absent a
    match, callers keep whatever location info was already inferred (or none) —
    never guess."""
    m = _LOCATION_RE.search(text or "")
    return m.group(1).strip() if m else ""


ReaderFn = Callable[[str], Awaitable[str]]


async def _default_reader(
    url: str,
    api_key: str,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    *,
    markdown: bool = False,
    max_chars: int = MAX_READER_CHARS,
) -> str:
    """Read `url` through the Jina reader.

    `markdown=True` is REQUIRED for the search page: Jina's `X-Return-Format: text`
    flattens the page and strips the `[title](url)` links this adapter parses, so a
    text-format search read yields zero postings even though the request succeeds.
    Confirmed live 2026-09-10 — the same search URL returns 41 links as markdown and
    0 as text. Detail pages want the opposite (plain text makes a cleaner JD).
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    if not markdown:
        headers["X-Return-Format"] = "text"
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.get(f"{JINA_READER}{url}", headers=headers)
        r.raise_for_status()
        return r.text[:max_chars]


class AppleAdapter:
    """Incremental discovery source over jobs.apple.com search results (Jina
    reader only — no browser automation, no undocumented API). `snapshot =
    False`: a search result set says nothing about what's been pulled, so the
    pipeline never stale-marks on this adapter's behalf."""

    source = SOURCE
    snapshot = False

    def __init__(
        self,
        *,
        queries: list[str],
        locations: list[str],
        api_key: str = "",
        reader: ReaderFn | None = None,
        known_ids: set[str] | None = None,
        max_reads: int = DEFAULT_MAX_READS,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ):
        self.queries = list(queries)
        self.locations = list(locations)
        self.api_key = api_key
        self._reader = reader
        self.known_ids = set(known_ids or set())
        self.max_reads = max_reads
        self.timeout_s = timeout_s

    async def _read_one(
        self, url: str, *, markdown: bool = False, max_chars: int | None = None
    ) -> str:
        if self._reader is not None:
            return await self._reader(url)
        return await _default_reader(
            url,
            self.api_key,
            timeout_s=self.timeout_s,
            markdown=markdown,
            max_chars=max_chars if max_chars is not None else MAX_READER_CHARS,
        )

    async def fetch(self) -> list[RawPosting]:
        if self._reader is None and not self.api_key:
            raise RuntimeError(f"{JINA_KEY_ENV} is not set (add it to {secrets_file_hint()})")

        postings: dict[str, RawPosting] = {}
        combos = list(itertools.product(self.queries, self.locations))[:MAX_SEARCH_READS]
        for query, location in combos:
            search_url = build_search_url(query, location)
            try:
                text = await self._read_one(search_url, markdown=True, max_chars=MAX_SEARCH_CHARS)
            except Exception as e:  # noqa: BLE001 — one bad query never sinks the run
                log.warning(
                    "apple search failed for %r/%r: %s: %s", query, location, type(e).__name__, e
                )
                continue
            remote = location.lower().startswith("home-office") or None
            for item in parse_search_links(text):
                ext_id = item["id"]
                if ext_id in postings:
                    continue
                postings[ext_id] = RawPosting(
                    source=SOURCE,
                    external_id=ext_id,
                    company="Apple",
                    title=item["title"],
                    url=item["url"],
                    remote=remote,
                    extra={"slug": item["slug"]},
                )

        reads = 0
        for ext_id, p in list(postings.items()):
            if ext_id in self.known_ids:
                continue
            if reads >= self.max_reads:
                break
            reads += 1
            try:
                detail_text = await self._read_one(p.url)
            except Exception as e:  # noqa: BLE001 — fail-soft; the search hit stays
                log.warning("apple detail read failed for %s: %s: %s", p.url, type(e).__name__, e)
                continue
            location_raw = extract_location(detail_text)
            update: dict[str, object] = {"description": detail_text}
            if location_raw:
                update["location_raw"] = location_raw
            postings[ext_id] = p.model_copy(update=update)

        return list(postings.values())

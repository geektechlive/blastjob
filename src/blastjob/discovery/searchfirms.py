"""Retained executive-search-firm monitor (weekly, no key required by default).

Why: retained searches for mid-market leadership roles in the owner's target
region almost never land on Greenhouse/Ashby/Lever, and aggregators don't
mirror them either — the firm's own opportunities page is often the only
public trace. There is no sanctioned scraping path for these sites, so Jina
search (`web/people.jina_search`, already wired for the people-search
feature) is the one sanctioned way in:
`site:{domain}` search restricted to the firm's own opportunities pages, with
the exec-title vocabulary and the user's target regions as the query.

Cost/politeness control: this only runs once a week (`due_today` /
`search_firm_weekday`), the pipeline builds the adapter only on that weekday,
and a full-text page fetch (via Jina reader) is skipped for any posting we
already hold a JD for — exactly the same "known_ids -> skip the expensive step"
shape as `workday.py`'s detail-fetch skip.

Incremental, like the mail-alert adapter: a search result set is "what's
posted right now", not authoritative over what's gone, so `snapshot = False`
and the pipeline never stale-marks on this adapter's behalf.

Failing loudly: this channel is where a senior retained search most
plausibly lives, and a channel that quietly returns nothing forever looks
exactly like a channel that is working and finding nothing. Every run therefore
records a `FirmReport` per firm (queries issued, results seen, postings kept,
query errors). Two complementary checks turn silence into something the user
can see, because they catch different failure shapes: `pipeline._report_silent_channels`
flags a firm that is silent THIS run and has NEVER once produced a posting
(queried the `jobs` table directly — no history needed); `dead_firms` below
flags a firm that WAS producing (e.g. one real hit, then nothing) and has
since gone silent for `dead_threshold` consecutive runs, which the "never once"
check cannot see because it stops looking the moment any row ever lands. Feeding
`dead_firms` requires persisting each run's `FirmReport`s and handing prior runs
back in as `history=` — the adapter does not do this itself (no DB access here
by design); see the module's home for the not-yet-wired persistence/cockpit
piece.

A 2026-09-07 investigation (commit 1b08b38, in `web/people.py`) ruled out a
URL-encoding/query-construction bug as the cause of one firm's repeated 422s:
`build_queries()` produces a structurally identical query for every firm, and
the 422 is Jina's own "this narrow site: query matched nothing" response,
which `web/people.jina_search`'s `_is_no_results` already classifies as `[]`
rather than a raised error. `tests/test_searchfirms_health.py` locks that
classification in through this adapter's real (non-injected) search path.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlparse

import httpx

from blastjob.discovery.base import RawPosting
from blastjob.discovery.textutil import normalize_region
from blastjob.matching.prefilter import classify_scope
from blastjob.models.config import SearchFirm
from blastjob.secrets import secrets_file_hint

log = logging.getLogger(__name__)

SOURCE_PREFIX = "searchfirm"
SEARCH_URL = "https://s.jina.ai/"
JINA_READER = "https://r.jina.ai/"
JINA_KEY_ENV = "JINA_API_KEY"
MAX_QUERY_CHARS = 200
MAX_READER_CHARS = 6000
DEFAULT_MAX_READS = 10
DEFAULT_TIMEOUT_S = 30

# Pages that are never a posting even when the title vocabulary matches
# (insights/blog/about/etc. on a search firm's own site). `what-we-do` /
# `functional-roles` confirmed 2026-09-05 against a live false positive: a search
# firm's evergreen practice-area page under /what-we-do/functional-roles/,
# describing a leadership role in the abstract, not an actual search assignment.
_NOT_POSTING = re.compile(
    r"/(insights?|blog|news|press|about|people|consultants?|contact|events?|"
    r"podcasts?|reports?|research|careers-at|what-we-do|functional-roles)\b|\.pdf$",
    re.I,
)
_EXEC_TITLE = re.compile(r"\b(cio|cto|ciso|chief|vice president|vp|svp|director|head)\b", re.I)


def source_for(slug: str) -> str:
    return f"{SOURCE_PREFIX}:{slug}"


@dataclass(frozen=True)
class FirmReport:
    """What one firm's sweep actually did this run — the evidence behind a
    "this channel is dead" note. `results` counts raw search hits, `kept` the
    ones that survived `looks_like_posting`."""

    slug: str
    name: str
    source: str
    queries: int = 0
    results: int = 0
    kept: int = 0
    errors: int = 0

    @property
    def silent(self) -> bool:
        return self.kept == 0


# How many consecutive weekly runs of nothing before a firm is called dead.
# 3 = three weeks, sized for a channel that only runs on `search_firm_weekday`
# rather than daily -- the same order of evidence `web/funnel.dead_boards`
# waits for (>=10 postings with none good), just measured in runs instead of
# postings because a search firm rarely reaches double-digit postings at all.
DEFAULT_DEAD_THRESHOLD = 3


def dead_firms(
    history: dict[str, list[FirmReport]], threshold: int = DEFAULT_DEAD_THRESHOLD
) -> list[str]:
    """Slugs whose most recent `threshold` recorded runs all kept zero
    postings -- the search-firm analogue of `web/funnel.dead_boards`, for a
    channel that almost never accumulates enough postings for that function's
    volume-based check to ever fire.

    `history` maps firm slug -> its runs in chronological order (oldest
    first), THIS run's `FirmReport` already appended by the caller (see
    `SearchFirmAdapter.fetch`). A firm with fewer than `threshold` recorded
    runs is not yet judged either way -- silence needs a track record, not
    one bad week. Pure and DB-free by design: persisting `FirmReport` history
    across runs and calling this is the caller's job (a `firm_run_history`
    table or similar; this workstream owns only the adapter, not the DB
    schema or the pipeline that would write it -- see the module's
    INTEGRATION PATCH note)."""
    dead: list[str] = []
    for slug, reports in history.items():
        recent = reports[-threshold:]
        if len(recent) >= threshold and all(r.silent for r in recent):
            dead.append(slug)
    return dead


def _quoted(term: str) -> str:
    return f'"{term}"'


def _chunk_terms(terms: list[str], prefix_len: int, limit: int) -> list[str]:
    """Group `terms` into OR-groups so `prefix_len + group + suffix` stays under
    `limit` chars. Each term appears in exactly one group, in order."""
    groups: list[list[str]] = []
    current: list[str] = []
    for term in terms:
        candidate = current + [term]
        group_len = len(" OR ".join(_quoted(t) for t in candidate)) + 2  # parens
        if current and prefix_len + group_len > limit:
            groups.append(current)
            current = [term]
        else:
            current = candidate
    if current:
        groups.append(current)
    return [f"({' OR '.join(_quoted(t) for t in g)})" for g in groups]


def build_queries(firm: SearchFirm, queries: list[str], regions: list[str]) -> list[str]:
    """<=3 Jina search queries per firm: `site:{domain} (title OR ...) (region OR ...)`,
    chunking the title OR-group so each query stays under ~200 chars."""
    if not firm.domain or not queries:
        return []
    site = f"site:{firm.domain}"
    region_group = "(" + " OR ".join(regions) + ")" if regions else ""
    suffix_len = len(site) + 2 + len(region_group)
    title_groups = _chunk_terms(list(queries), suffix_len, MAX_QUERY_CHARS)
    out = []
    for group in title_groups[:3]:
        parts = [site, group]
        if region_group:
            parts.append(region_group)
        out.append(" ".join(parts))
    return out


def looks_like_posting(
    url: str,
    title: str,
    domain: str,
    keep_terms: Sequence[str] = (),
    drop_terms: Sequence[str] = (),
) -> bool:
    """True when `url` plausibly points at an actual opportunity listing on
    the firm's own site, for a leadership-scope title. `keep_terms`/
    `drop_terms` are the configured scope prefilter (`[discovery]
    title_keep_terms`/`title_drop_terms`); both empty gates nothing here
    either, same as everywhere else that calls `classify_scope`."""
    if not url or not title or not domain:
        return False
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if not (host == domain.lower() or host.endswith("." + domain.lower())):
        return False
    if _NOT_POSTING.search(parsed.path or ""):
        return False
    keep, _ = classify_scope(title, "", keep_terms, drop_terms)
    if not keep:
        return False
    return bool(_EXEC_TITLE.search(title))


def external_id_for(url: str) -> str:
    """Lowercase host + path, no scheme/query/fragment, no trailing slash —
    stable identity for a listing whose URL may be re-crawled with different
    tracking params."""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/")
    return f"{host}{path}"


def infer_geo(
    title: str, description: str, places: Sequence[str] = (), home_state: str = ""
) -> tuple[str, str, bool | None]:
    """(location_raw, region, remote) inferred from title/description text —
    search results carry no structured location field. `places` are the
    place names to look for (`discovery.search_firm_places`, else the search
    regions); the first one found in the text is read back as the posting's
    rough location. A spelled-out state name becomes its USPS code; any other
    place is taken to be in the configured `home_state` (the places are the
    user's own search area)."""
    text = f"{title} {description}".lower()
    remote: bool | None = True if "remote" in text else None
    for place in places:
        name = place.strip()
        if not name or name.lower() == "remote" or name.lower() not in text:
            continue
        code = normalize_region(name)
        region = code if code != name else home_state.strip().upper()
        return name.title(), region, remote
    return "", "", remote


def due_today(today: date, weekday: int) -> bool:
    return today.weekday() == weekday


SearchFn = Callable[[str, str], Awaitable[list[dict]]]
ReaderFn = Callable[[str], Awaitable[str]]


async def _default_search(
    query: str, api_key: str, timeout_s: int = DEFAULT_TIMEOUT_S
) -> list[dict]:
    from blastjob.web.people import jina_search

    return await jina_search(query, api_key, timeout_s=timeout_s)


async def _default_reader(url: str, api_key: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> str:
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.get(
            f"{JINA_READER}{url}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Return-Format": "text",
            },
        )
        r.raise_for_status()
        return r.text[:MAX_READER_CHARS]


class SearchFirmAdapter:
    """Incremental discovery source over retained-search-firm sites (Jina
    search + reader). `snapshot = False` — a search result set says nothing
    about what's gone, so the pipeline never stale-marks on this adapter's
    behalf. `source` is the aggregate name for error reporting only; each
    posting carries its own `searchfirm:{slug}` source, exactly like the mail
    adapter's postings carry their real linkedin/indeed origin."""

    source = "searchfirms"
    snapshot = False

    def __init__(
        self,
        firms: list[SearchFirm],
        *,
        queries: list[str],
        regions: list[str],
        api_key: str = "",
        search: SearchFn | None = None,
        reader: ReaderFn | None = None,
        known_ids: set[str] | None = None,
        max_reads: int = DEFAULT_MAX_READS,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        history: dict[str, list[FirmReport]] | None = None,
        dead_threshold: int = DEFAULT_DEAD_THRESHOLD,
        keep_terms: Sequence[str] = (),
        drop_terms: Sequence[str] = (),
        places: Sequence[str] = (),
        home_state: str = "",
    ):
        self.firms = list(firms)
        self.queries = list(queries)
        self.regions = list(regions)
        self.places = list(places) or list(regions)
        self.home_state = home_state
        self.keep_terms = list(keep_terms)
        self.drop_terms = list(drop_terms)
        self.api_key = api_key
        self._search = search
        self._reader = reader
        self.known_ids = set(known_ids or set())
        self.max_reads = max_reads
        self.timeout_s = timeout_s
        self.firm_reports: list[FirmReport] = []
        # Prior runs' reports per firm slug (oldest first), so `fetch()` can
        # tell a firm that's ALWAYS been silent from one that just went
        # silent this week. Populating this across real runs -- persisting
        # `firm_reports` somewhere and passing it back in -- is the caller's
        # job; see the module's INTEGRATION PATCH note.
        self.history = {slug: list(reports) for slug, reports in (history or {}).items()}
        self.dead_threshold = dead_threshold
        self.dead_firms: list[str] = []

    async def _search_one(self, query: str) -> list[dict]:
        if self._search is not None:
            return await self._search(query, self.api_key)
        return await _default_search(query, self.api_key, timeout_s=self.timeout_s)

    async def _read_one(self, url: str) -> str:
        if self._reader is not None:
            return await self._reader(url)
        return await _default_reader(url, self.api_key, timeout_s=self.timeout_s)

    async def fetch(self) -> list[RawPosting]:
        if self._search is None and not self.api_key:
            raise RuntimeError(f"{JINA_KEY_ENV} is not set (add it to {secrets_file_hint()})")

        postings: dict[str, RawPosting] = {}
        self.firm_reports = []
        for firm in self.firms:
            queries = build_queries(firm, self.queries, self.regions)
            seen_results = kept = errors = 0
            for query in queries:
                try:
                    results = await self._search_one(query)
                except Exception as e:  # noqa: BLE001 — one bad query never sinks the firm
                    errors += 1
                    log.warning(
                        "searchfirm %s query failed: %s: %s", firm.slug, type(e).__name__, e
                    )
                    continue
                seen_results += len(results or [])
                for r in results or []:
                    url = str(r.get("url") or "")
                    title = str(r.get("title") or "")[:200]
                    if not looks_like_posting(
                        url, title, firm.domain, self.keep_terms, self.drop_terms
                    ):
                        continue
                    ext_id = external_id_for(url)
                    if not ext_id or ext_id in postings:
                        continue
                    kept += 1
                    description = str(r.get("description") or "")
                    location_raw, region, remote = infer_geo(
                        title, description, self.places, self.home_state
                    )
                    postings[ext_id] = RawPosting(
                        source=source_for(firm.slug),
                        external_id=ext_id,
                        company=f"{firm.name} (retained search)",
                        title=title,
                        url=url,
                        description=description,
                        location_raw=location_raw,
                        region=region,
                        remote=remote,
                        extra={"firm": firm.slug, "from_search": True},
                    )
            self.firm_reports.append(
                FirmReport(
                    slug=firm.slug,
                    name=firm.name,
                    source=source_for(firm.slug),
                    queries=len(queries),
                    results=seen_results,
                    kept=kept,
                    errors=errors,
                )
            )

        merged = {
            report.slug: self.history.get(report.slug, []) + [report]
            for report in self.firm_reports
        }
        self.dead_firms = dead_firms(merged, self.dead_threshold)
        for slug in self.dead_firms:
            log.warning(
                "searchfirm %s has kept zero postings for %d consecutive runs -- "
                "the channel looks dead, not just quiet this week",
                slug,
                self.dead_threshold,
            )

        reads = 0
        for ext_id, p in postings.items():
            if ext_id in self.known_ids:
                continue
            if reads >= self.max_reads:
                break
            reads += 1
            try:
                text = await self._read_one(p.url)
            except Exception as e:  # noqa: BLE001 — fail-soft; the search snippet stays
                log.warning("searchfirm reader failed for %s: %s: %s", p.url, type(e).__name__, e)
                continue
            if text:
                postings[ext_id] = p.model_copy(update={"description": text})

        return list(postings.values())

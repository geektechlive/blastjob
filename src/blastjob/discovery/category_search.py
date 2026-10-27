"""The weekly category role search (#91; ADRs 0054, 0065, 0066).

For every enabled category (`core/category_store.py`) the daily run looks for leadership roles
two ways, on the search-firm weekday only:

- **Adzuna.** One `AdzunaAdapter` per category, `queries` = its terms. Results keep
  `source='adzuna'` and Adzuna's own id, so a posting the main search and two categories all
  find is ONE job with two hits. The adapter is not a snapshot, so it never stale-marks; saving
  refreshes `last_seen` like the main search, which keeps the main snapshot's TTL from retiring
  a category-found row. A dream category searches the whole country (no place sent).
- **Jina web search.** One query per term, `"<term>" ("CIO" OR ...) job`. A result is kept only
  when its URL is safe, not LinkedIn and `looks_like_category_posting`, and its title passes the
  configured leadership filter. Kept pages are read for their text and saved as
  `source='category-web'` with the canonical URL as the external id. A URL already stored
  (under any source) gets the hit without a re-read. The place comes from
  `searchfirms.infer_geo` on the page text; when it finds none the place stays unknown and the
  role follows today's rules (review), never a guessed location.

Caps per run (each logs when hit and shows as a note): `MAX_SEARCHES_TOTAL` (10) search terms,
`MAX_READS_PER_CATEGORY` / `MAX_READS_TOTAL` page reads, `MAX_NEW_PER_CATEGORY` new jobs. The
same planned (category, term) window bounds BOTH halves: at most 10 Adzuna queries and at most
10 Jina searches per run, because each Adzuna adapter is built with only its planned terms. When
more pairs exist than that, the starting offset rotates by ISO week, so every term is searched
over the weeks.

Each job and its hit are written in one transaction per category; a failure between the two
writes rolls both back. The run remembers each category's `id`, `terms_version` and `enabled`
and discards that category's results if any changed while it ran. Nothing here calls a model.
See docs/how-it-works/discovery.md#category-searches.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from urllib.parse import parse_qsl, urlparse

import httpx

from blastjob import logging as applog
from blastjob.core import category_store, jina
from blastjob.db import client as dbc
from blastjob.discovery.adzuna import AdzunaAdapter
from blastjob.discovery.base import RawPosting
from blastjob.discovery.pipeline import MILES_TO_KM, DiscoveryResult, _recluster_new, _SaveCtx
from blastjob.discovery.searchfirms import due_today, infer_geo
from blastjob.discovery.workday import title_wanted
from blastjob.models.config import BlastJobConfig
from blastjob.web.urls import safe_external_url

log = logging.getLogger(__name__)

SOURCE_WEB = "category-web"
SOURCE_ADZUNA_PREFIX = "category-adzuna:"
MAX_SEARCHES_TOTAL = 10
MAX_READS_PER_CATEGORY = 5
MAX_READS_TOTAL = 20
MAX_NEW_PER_CATEGORY = 20
READ_TIMEOUT_S = 30
LEADERSHIP_TITLES = ("CIO", "VP of IT", "Director of IT", "CTO")

_ATS_HOSTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "myworkdaysite.com",
    "dayforcehcm.com",
)
_AGGREGATOR_HOSTS = (
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "adzuna.com",
    "monster.com",
    "simplyhired.com",
    "careerbuilder.com",
    "talent.com",
    "jooble.org",
    "snagajob.com",
    "linkup.com",
)
_ARTICLE_PATHS = ("/news/", "/blog/", "/article/", "/articles/", "/press/")
_JOB_PATH_HINTS = ("/job", "/careers/", "/positions/", "/opening")

SearchFn = Callable[[str, str], Awaitable[list[dict]]]
ReadFn = Callable[[str, str], Awaitable[str]]


# --------------------------------------------------------------------------- default bindings
# Module-level so tests/conftest.py::_no_real_category_search can replace them.


def _jina_key() -> str:
    """The Jina key from the environment (the daily run loads the secrets file before this
    step). Patched to "" suite-wide in tests."""
    return os.environ.get(jina.JINA_KEY_ENV, "").strip()


async def _search_default(query: str, api_key: str) -> list[dict]:
    return await jina.jina_search(query, api_key, timeout_s=READ_TIMEOUT_S)


async def _read_default(url: str, api_key: str) -> str:
    async with httpx.AsyncClient(timeout=READ_TIMEOUT_S) as client:
        return await jina.read_page(client, url, api_key)


_search: SearchFn = _search_default
_read: ReadFn = _read_default


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class CategoryRef:
    """A category as the run saw it when it started."""

    id: int
    slug: str
    label: str
    terms: tuple[str, ...]
    terms_version: int
    is_dream: bool
    enabled: bool


AdzunaFactory = Callable[[BlastJobConfig, CategoryRef], AdzunaAdapter]


@dataclass
class CategoryRunResult:
    ran: bool = False
    saved: int = 0  # new jobs
    hits: int = 0  # hit rows written or refreshed
    queries: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)  # slugs whose results were dropped
    errors: dict[str, str] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    reclustered: int = 0


@dataclass(frozen=True)
class _Cand:
    """One result to save: a new/refreshed posting, or just a hit on a job we hold."""

    posting: RawPosting | None
    existing_id: int
    is_new: bool


def load_categories(conn: sqlite3.Connection) -> list[CategoryRef]:
    """The enabled categories, oldest first."""
    return [
        CategoryRef(
            id=int(r["id"]),
            slug=str(r["slug"]),
            label=str(r["label"]),
            terms=tuple(category_store.terms_of(r)),
            terms_version=int(r["terms_version"]),
            is_dream=bool(r["is_dream"]),
            enabled=bool(r["enabled"]),
        )
        for r in category_store.list_categories(conn)
        if r["enabled"]
    ]


# --------------------------------------------------------------------------- queries and plan


def build_query(term: str) -> str:
    """`"<term>" ("CIO" OR "VP of IT" OR ...) job`: the term scopes every title alternative.
    A double quote inside a term would break the grouping, so it becomes a space."""
    clean = " ".join(term.replace('"', " ").split())
    titles = " OR ".join(f'"{t}"' for t in LEADERSHIP_TITLES)
    return f'"{clean}" ({titles}) job'


def plan_searches(categories: Sequence[CategoryRef], today: date) -> list[tuple[CategoryRef, str]]:
    """The (category, term) pairs to search this week, at most `MAX_SEARCHES_TOTAL`. Over the
    cap the window starts at an offset that moves with the ISO week, so every term is searched
    over the weeks instead of the last ones starving."""
    pairs = [(c, t) for c in categories for t in c.terms]
    if len(pairs) <= MAX_SEARCHES_TOTAL:
        return pairs
    offset = (today.isocalendar().week * MAX_SEARCHES_TOTAL) % len(pairs)
    return (pairs[offset:] + pairs[:offset])[:MAX_SEARCHES_TOTAL]


def build_category_adzuna(cfg: BlastJobConfig, category: CategoryRef) -> AdzunaAdapter:
    """A non-snapshot Adzuna adapter for one category. A dream category sends no place."""
    d = cfg.discovery
    adapter = AdzunaAdapter(
        queries=list(category.terms),
        where="" if category.is_dream else d.adzuna_where,
        distance_km=0 if category.is_dream else int(d.commute_radius_miles * MILES_TO_KM),
        salary_min=0,
        app_id_env=d.adzuna_app_id_env,
        app_key_env=d.adzuna_app_key_env,
        timeout_s=d.provider_timeout_s,
        max_retries=d.provider_max_retries,
    )
    adapter.snapshot = False  # a category's window says nothing about what is gone
    adapter.source = f"{SOURCE_ADZUNA_PREFIX}{category.slug}"
    return adapter


# --------------------------------------------------------------------------- the URL filter


def _host_in(host: str, domains: Sequence[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


# Query keys that say WHICH posting a URL is (compared case-insensitively). Everything else
# (utm_*, gclid, fbclid, ref, source, ...) is tracking and is dropped.
_IDENTITY_KEYS = frozenset(
    k.lower()
    for k in (
        "gh_jid",
        "jobId",
        "job_id",
        "id",
        "req",
        "reqId",
        "requisitionId",
        "jk",
        "posting_id",
    )
)
_INTERNAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan")
_TLD = re.compile(r"[a-z][a-z0-9-]*")


def canonical_url(url: str) -> str:
    """Lowercase scheme and host, no fragment or trailing slash, and only the query keys that
    identify the posting (`gh_jid=123`), sorted. Tracking parameters are dropped, so a page
    re-crawled with different ones keeps one identity while two postings that differ only by an
    id stay two."""
    p = urlparse(url.strip())
    keep = sorted(
        (k.lower(), v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() in _IDENTITY_KEYS
    )
    query = "&".join(f"{k}={v}" for k, v in keep)
    base = f"{p.scheme.lower()}://{(p.netloc or '').lower()}{(p.path or '').rstrip('/')}"
    return f"{base}?{query}" if query else base


def _clean_url_text(url: str) -> bool:
    """No backslash, whitespace or control characters, and no userinfo `@` in the host part:
    shapes that make different parsers read different hosts."""
    if "\\" in url or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return False
    return "@" not in (urlparse(url).netloc or "")


def _public_host(host: str) -> bool:
    """False for an IP literal, localhost, a single-label name or an internal suffix."""
    if not host or host == "localhost" or host.endswith(_INTERNAL_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    labels = host.split(".")
    return len(labels) > 1 and bool(_TLD.fullmatch(labels[-1]))


def looks_like_category_posting(
    url: str, title: str, keep_terms: Sequence[str] = (), drop_terms: Sequence[str] = ()
) -> bool:
    """True when `url` plausibly is one job posting and `title` is a wanted leadership title.

    Not `searchfirms.looks_like_posting`, which only accepts a search firm's own domain. Here
    LinkedIn (never contacted, ADR 0041), job aggregators and article paths are refused, and
    the URL must be on a known ATS host or carry a job-path hint."""
    if not url or not title.strip() or not _clean_url_text(url):
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() not in ("http", "https") or not _public_host(host):
        return False
    if jina.is_linkedin_url(url) or _host_in(host, _AGGREGATOR_HOSTS):
        return False
    path = (parsed.path or "").lower()
    if any(p in path for p in _ARTICLE_PATHS):
        return False
    if not title_wanted(title, keep_terms, drop_terms):
        return False
    return _host_in(host, _ATS_HOSTS) or any(h in path for h in _JOB_PATH_HINTS)


def _company_hint(url: str) -> str:
    """A company label from the page address: the board slug on Greenhouse, Lever or Ashby,
    else the site's host. It names where the page lives, not who the employer is."""
    p = urlparse(url)
    host = (p.hostname or "").lower().removeprefix("www.")
    first = next((s for s in (p.path or "").split("/") if s), "")
    if _host_in(host, ("greenhouse.io", "lever.co", "ashbyhq.com")) and first:
        return first
    return host


def _existing_job(conn: sqlite3.Connection, canonical: str, url: str) -> tuple[int, bool]:
    """(id, archived) of a job we already hold for this page, under any source; (0, False) when
    none. An archived job is one the owner dismissed: it gets no hit."""
    row = conn.execute(
        "SELECT id, review_state FROM jobs WHERE (source=? AND external_id=?) OR url IN (?, ?, ?) "
        "ORDER BY (source=?) DESC, id LIMIT 1",
        (SOURCE_WEB, canonical, url, canonical, canonical + "/", SOURCE_WEB),
    ).fetchone()
    return (int(row["id"]), row["review_state"] == "archived") if row else (0, False)


def _adzuna_state(conn: sqlite3.Connection, external_id: str) -> str | None:
    """The review state of the stored Adzuna job, or None when we do not hold it."""
    row = conn.execute(
        "SELECT review_state FROM jobs WHERE source='adzuna' AND external_id=?", (external_id,)
    ).fetchone()
    return None if row is None else str(row["review_state"])


# --------------------------------------------------------------------------- the run


class _Run:
    def __init__(
        self,
        cfg: BlastJobConfig,
        conn: sqlite3.Connection,
        res: CategoryRunResult,
        *,
        search: SearchFn | None,
        read: ReadFn | None,
        adzuna_factory: AdzunaFactory | None,
    ) -> None:
        self.cfg = cfg
        self.conn = conn
        self.res = res
        self.api_key = _jina_key()
        self.search = search or _search
        self.read = read or _read
        self.web_on = search is not None or bool(self.api_key)
        d = cfg.discovery
        self.adzuna_on = d.enable_aggregators and bool(
            os.environ.get(d.adzuna_app_id_env) and os.environ.get(d.adzuna_app_key_env)
        )
        self.adzuna_factory = adzuna_factory or build_category_adzuna
        self.reads_total = 0
        self.ctx = _SaveCtx(conn, SOURCE_WEB, d.exclude_companies, set(), DiscoveryResult())

    def note(self, cat: CategoryRef, text: str) -> None:
        log.info("category %s: %s", cat.slug, text)
        key = f"category:{cat.slug}"
        self.res.notes[key] = f"{self.res.notes[key]}; {text}" if key in self.res.notes else text

    def error(self, source: str, exc: Exception | str) -> None:
        text = f"{type(exc).__name__}: {exc}" if isinstance(exc, Exception) else exc
        self.res.errors[source] = applog.redact(text)

    async def process(self, cat: CategoryRef, terms: Sequence[str]) -> None:
        cands: list[_Cand] = []
        if self.adzuna_on and terms:
            # Only the planned terms: the same window that bounds the Jina searches.
            cands += await self._adzuna(replace(cat, terms=tuple(terms)))
        if self.web_on and terms:
            cands += await self._web(cat, terms)
        self._save(cat, cands)

    async def _adzuna(self, cat: CategoryRef) -> list[_Cand]:
        d = self.cfg.discovery
        source = f"{SOURCE_ADZUNA_PREFIX}{cat.slug}"
        try:
            postings = await self.adzuna_factory(self.cfg, cat).fetch()
        except Exception as e:  # noqa: BLE001 - one category's failure never stops the next
            self.error(source, e)
            return []
        wanted = [
            p for p in postings if title_wanted(p.title, d.title_keep_terms, d.title_drop_terms)
        ]
        out: list[_Cand] = []
        for p in wanted:
            state = _adzuna_state(self.conn, p.external_id)
            if state == "archived":
                continue
            out.append(_Cand(p, 0, state is None))
        return out

    async def _web(self, cat: CategoryRef, terms: Sequence[str]) -> list[_Cand]:
        d = self.cfg.discovery
        source = f"{SOURCE_WEB}:{cat.slug}"
        out: list[_Cand] = []
        seen: set[str] = set()
        reads = 0
        capped = False
        for term in terms:
            query = build_query(term)
            self.res.queries.append(query)
            try:
                results = await self.search(query, self.api_key)
            except Exception as e:  # noqa: BLE001 - one bad query never sinks the category
                self.error(source, e)
                continue
            for r in results or []:
                url = safe_external_url(str(r.get("url") or ""))
                title = str(r.get("title") or "")[:200]
                if not url or not looks_like_category_posting(
                    url, title, d.title_keep_terms, d.title_drop_terms
                ):
                    continue
                canonical = canonical_url(url)
                if canonical in seen:
                    continue
                seen.add(canonical)
                known, archived = _existing_job(self.conn, canonical, url)
                if archived:
                    continue
                if known:
                    out.append(_Cand(None, known, False))
                    continue
                if reads >= MAX_READS_PER_CATEGORY or self.reads_total >= MAX_READS_TOTAL:
                    capped = True
                    continue
                reads += 1
                self.reads_total += 1
                out.append(_Cand(await self._posting(url, canonical, title, r), 0, True))
        if capped:
            self.note(
                cat,
                f"page-read cap hit ({MAX_READS_PER_CATEGORY} per category, "
                f"{MAX_READS_TOTAL} per run); the rest wait for next week",
            )
        return out

    async def _posting(self, url: str, canonical: str, title: str, hit: dict) -> RawPosting:
        d = self.cfg.discovery
        self.res.reads.append(url)
        try:
            text = await self.read(url, self.api_key)
        except Exception as e:  # noqa: BLE001 - fail soft; the search snippet stays
            log.warning("category page read failed for %s: %s", url, type(e).__name__)
            text = ""
        description = text or str(hit.get("description") or "")
        places = d.search_firm_places or d.search_firm_regions
        location_raw, region, remote = infer_geo(title, description, places, d.home_state)
        return RawPosting(
            source=SOURCE_WEB,
            external_id=canonical,
            company=_company_hint(url),
            title=title,
            url=url,
            description=description,
            location_raw=location_raw,
            region=region,
            remote=remote,
            extra={"from_search": True},
        )

    def _still_current(self, cat: CategoryRef) -> bool:
        row = self.conn.execute(
            "SELECT terms_version, enabled FROM category_searches WHERE id=?", (cat.id,)
        ).fetchone()
        return (
            row is not None
            and int(row["terms_version"]) == cat.terms_version
            and bool(row["enabled"]) == cat.enabled
        )

    def _save(self, cat: CategoryRef, cands: list[_Cand]) -> None:
        """Every result of one category in one transaction, after checking the category is the
        one the run started with. A change mid-run drops the results (and their job rows)."""
        if not cands:
            return
        conn = self.conn
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not self._still_current(cat):
                conn.rollback()
                self.res.discarded.append(cat.slug)
                self.note(cat, "edited or disabled while the run was searching; results dropped")
                return
            failures = self._save_all(cat, cands)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if failures:
            self.error(
                f"{SOURCE_WEB}:{cat.slug}",
                f"{len(failures)} result(s) not saved this run; first: {failures[0]}",
            )

    def _save_all(self, cat: CategoryRef, cands: list[_Cand]) -> list[str]:
        failures: list[str] = []
        new_saved = 0
        capped = False
        for cand in cands:
            if cand.is_new and new_saved >= MAX_NEW_PER_CATEGORY:
                capped = True
                continue
            self.conn.execute("SAVEPOINT cand")
            try:
                job_id = self._save_one(cat, cand)
            except Exception as e:  # noqa: BLE001 - one bad result never sinks the rest
                self.conn.execute("ROLLBACK TO cand")
                failures.append(f"{type(e).__name__}: {e}")
            else:
                if job_id:
                    self.res.hits += 1
                    if cand.is_new:
                        new_saved += 1
            finally:
                self.conn.execute("RELEASE cand")
        self.res.saved += new_saved
        if capped:
            self.note(cat, f"new-job cap hit ({MAX_NEW_PER_CATEGORY} per category per run)")
        return failures

    def _save_one(self, cat: CategoryRef, cand: _Cand) -> int:
        job_id = cand.existing_id
        if cand.posting is not None:
            job_id = self.ctx.save_job(cand.posting, commit=False)
        if job_id:
            category_store.attach_hit(self.conn, job_id, cat.id, cat.terms_version)
        return job_id


async def run_category_searches(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    *,
    today: date | None = None,
    search: SearchFn | None = None,
    read: ReadFn | None = None,
    adzuna_factory: AdzunaFactory | None = None,
) -> CategoryRunResult:
    """Run every enabled category's searches when today is the weekly day; else do nothing.
    `search`, `read` and `adzuna_factory` are injectable so tests never reach the network."""
    from blastjob.secrets import load_secrets_env

    res = CategoryRunResult()
    day = today or date.today()
    if not due_today(day, cfg.discovery.search_firm_weekday):
        return res
    res.ran = True
    categories = load_categories(conn)
    if not categories:
        return res
    load_secrets_env()
    started = dbc._now()
    run = _Run(cfg, conn, res, search=search, read=read, adzuna_factory=adzuna_factory)
    planned = plan_searches(categories, day)
    for cat in categories:
        terms = [t for c, t in planned if c.id == cat.id]
        try:
            await run.process(cat, terms)
        except Exception as e:  # noqa: BLE001 - record and go on to the next category
            conn.rollback()
            run.error(f"{SOURCE_WEB}:{cat.slug}", e)
    if res.saved:
        try:
            res.reclustered = _recluster_new(conn, cfg, started)
            conn.commit()
        except Exception as e:  # noqa: BLE001 - grouping is a nicety; the jobs are saved
            conn.rollback()
            log.warning("category recluster failed: %s", type(e).__name__)
    return res

"""Find, and later re-check, a job's posting on the employer's own ATS board.

LinkedIn alert emails, Adzuna, manual entries and search-firm sweeps cannot
prove a posting closed: their silence means nothing. An employer's own board
can. So for those jobs we look for the SAME posting on a board that can, and
check there. linkedin.com is never contacted -- nothing here ever requests a
job's own `url`; every request goes to a resolved ATS board.

A wrong link would falsely close a live job, so every step leans towards "no
link": the board must belong to the job's company, the title must match
exactly after normalization (or be the ONLY near-identical candidate), and two
plausible candidates mean we do not know.

Employers on no board we can list get a `page:<host>` link instead: their own
careers page, trusted only on schema.org JobPosting JSON-LD (core/page_links).
It is the last fallback, after the board path found nothing.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Any

import httpx

from blastjob.core.company import company_key
from blastjob.core.page_fetch import MAX_HOPS as PAGE_MAX_HOPS
from blastjob.db import client as dbc
from blastjob.discovery import resolve
from blastjob.discovery.ashby import AshbyAdapter
from blastjob.discovery.base import RawPosting
from blastjob.discovery.dayforce import MAX_PAGES as DAYFORCE_MAX_PAGES
from blastjob.discovery.dayforce import PAGE_SIZE as DAYFORCE_PAGE_SIZE
from blastjob.discovery.dayforce import DayforceAdapter
from blastjob.discovery.dayforce import parse_posting as dayforce_posting
from blastjob.discovery.greenhouse import GreenhouseAdapter
from blastjob.discovery.http import USER_AGENT
from blastjob.discovery.lever import LeverAdapter
from blastjob.discovery.resolve import BoardHit
from blastjob.discovery.textutil import html_to_text, split_location
from blastjob.discovery.workday import CLOSED_STATUS, WorkdayAdapter, presence_stub
from blastjob.models.config import DiscoveryConfig

# Sources whose own sync can prove a posting closed: a full board listing, or a
# windowed board whose adapter confirms closure against the detail endpoint.
AUTHORITATIVE_PREFIXES: tuple[str, ...] = (
    "workday:",
    "greenhouse:",
    "lever:",
    "ashby:",
    "dayforce:",
)

# Near-identical titles only. Uncalibrated: chosen so a pluralization or a
# dropped comma passes ("Information Technologies") while a broader or narrower
# role does not ("Director of IT" vs "Director of IT Infrastructure" is 0.65).
TITLE_MATCH_MIN_RATIO = 0.92

# Only single-word, unambiguous title abbreviations. "IT" is deliberately left
# alone: expanding it would also rewrite the pronoun in a long title.
_ABBREVIATIONS: dict[str, str] = {
    "vp": "vice president",
    "svp": "senior vice president",
    "evp": "executive vice president",
    "avp": "assistant vice president",
    "sr": "senior",
    "dir": "director",
    "mgr": "manager",
    "cio": "chief information officer",
    "cto": "chief technology officer",
}
_PARENS = re.compile(r"\([^)]*\)")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_STATE_CODE_LEN = 2
_US_STATES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}  # fmt: skip
_FullBoard = type[GreenhouseAdapter] | type[LeverAdapter] | type[AshbyAdapter]
_FULL_BOARD_ATS: dict[str, _FullBoard] = {
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "ashby": AshbyAdapter,
}
_PROBE_TIMEOUT_S = 15
# A Workday detail answer is read through this cap, like page_fetch's page bodies.
MAX_DETAIL_BYTES = 2 * 1024 * 1024
# Same-metro radius for cluster-sibling proof when the caller passes none.
DEFAULT_RADIUS_MILES = DiscoveryConfig().commute_radius_miles


@dataclass(frozen=True)
class Link:
    """The matched posting on the employer's board, as stored on the job row."""

    source: str
    external_id: str
    url: str = ""


def employer_jd_ref(link: Link) -> tuple[str, str]:
    """The (jd_source, jd_source_ref) provenance a description adopted over
    `link` carries, so a revert can target exactly that adoption."""
    return dbc.EMPLOYER_JD_PREFIX + link.source, link.external_id


def is_authoritative(source: str | None) -> bool:
    return (source or "").startswith(AUTHORITATIVE_PREFIXES)


def normalize_title(title: str | None) -> str:
    text = _PARENS.sub(" ", (title or "").lower()).replace("&", " and ")
    words = _NON_ALNUM.sub(" ", text).split()
    return " ".join(_ABBREVIATIONS.get(w, w) for w in words)


def _state_code(region: str) -> str:
    """'Ohio' -> 'OH'; a code or anything unrecognized comes back upper-cased."""
    text = " ".join((region or "").split())
    return _US_STATES.get(text.lower(), text.upper())


def _region_ok(job_region: str, posting_region: str) -> bool:
    """A mismatch only counts when both sides fold to plain state codes (the
    DB stores 'Ohio', boards say 'OH'); anything fuzzier ("", "Remote")
    cannot rule a candidate out."""
    a, b = _state_code(job_region), _state_code(posting_region)
    if len(a) != _STATE_CODE_LEN or len(b) != _STATE_CODE_LEN:
        return True
    return a == b


def match_posting(
    title: str, postings: Sequence[RawPosting], region: str = ""
) -> RawPosting | None:
    """The one posting that is confidently this title, or None."""
    target = normalize_title(title)
    if not target:
        return None
    pool = [p for p in postings if _region_ok(region, p.region)]
    exact = [p for p in pool if normalize_title(p.title) == target]
    if exact:
        return exact[0] if len(exact) == 1 else None
    close = [
        p
        for p in pool
        if SequenceMatcher(None, target, normalize_title(p.title)).ratio() >= TITLE_MATCH_MIN_RATIO
    ]
    return close[0] if len(close) == 1 else None


def _compact(text: str) -> str:
    return _NON_ALNUM.sub("", text.lower())


def board_matches_company(ats: str, token: str, company: str) -> bool:
    """The board's own identifier must BE the company's name. The resolver is
    noisy (a slug can belong to a different company of the same name root, a
    Jina hit can be any tenant), so an abbreviation tenant like "mcw" is
    rejected on purpose: no link beats a wrong one."""
    ident = (token or "").split("/")[0] if ats in ("workday", "dayforce") else (token or "")
    key = _compact(company_key(company or ""))
    return bool(ident) and bool(key) and _compact(ident) == key


def board_token(link: Link) -> tuple[str, str] | None:
    """(ats, board token) for a stored link, or None if it is not a board we
    know how to check. Workday/Dayforce sources drop the site/board part, so
    it is recovered from the posting URL."""
    prefix, _, rest = link.source.partition(":")
    if prefix in _FULL_BOARD_ATS and rest:
        return prefix, rest
    if prefix == "workday":
        refs = [r for r in resolve.workday_candidates(link.url) if r.tenant == rest]
        return ("workday", refs[0].token) if refs else None
    if prefix == "dayforce":
        found = [r for r in resolve.dayforce_candidates(link.url) if r.client == rest]
        return ("dayforce", found[0].token) if found else None
    return None


# --- DB links: a posting we already hold from an authoritative board ---------


def authoritative_index(conn: sqlite3.Connection) -> dict[str, list[RawPosting]]:
    """Every authoritative-source job, keyed by company_key, as RawPostings
    carrying `last_seen` and `source_state` in `extra`."""
    where = " OR ".join("source LIKE ?" for _ in AUTHORITATIVE_PREFIXES)
    rows = conn.execute(
        "SELECT id, source, external_id, company, title, url, geo_region, last_seen, source_state "
        f"FROM jobs WHERE {where}",
        tuple(f"{p}%" for p in AUTHORITATIVE_PREFIXES),
    ).fetchall()
    index: dict[str, list[RawPosting]] = {}
    for r in rows:
        index.setdefault(company_key(r["company"] or ""), []).append(_as_posting(r))
    return index


def _as_posting(r: sqlite3.Row) -> RawPosting:
    return RawPosting(
        source=r["source"],
        external_id=r["external_id"],
        title=r["title"] or "",
        url=r["url"] or "",
        region=r["geo_region"] or "",
        extra={
            "last_seen": r["last_seen"] or "",
            "source_state": r["source_state"] or "",
            "job_id": r["id"],
        },
    )


posting_of_row = _as_posting  # public: employer_jd re-checks a held row's title


def _overlaps(job: sqlite3.Row, posting: RawPosting) -> bool:
    """A candidate last seen before this job first appeared is an earlier
    posting of the same role, not this one; linking to it would close a live
    re-post on day one."""
    return str(posting.extra.get("last_seen", ""))[:10] >= (job["first_seen"] or "")[:10]


def db_link(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
    index: dict[str, list[RawPosting]],
    radius_miles: float = DEFAULT_RADIUS_MILES,
    geo: DiscoveryConfig | None = None,
) -> tuple[Link, str] | None:
    """(link, its source_state) from postings already in the DB: a cluster
    sibling first, then a confident company + title match.

    Either way a posting counts only when it is strictly the same metro, both
    remote, or the user confirmed the pair the same role
    (`role_clusters.sibling_is_proof`). Clusters are display groupings, and the
    DB holds only the board postings discovery happened to fetch, so neither may
    close a live job on title alone. An unplaceable job falls through to the
    network path, which reads the live board and refuses an ambiguous title."""
    from blastjob.core.role_clusters import sibling_is_proof

    geo = geo or DiscoveryConfig()
    if job["cluster_id"]:
        rows = conn.execute(
            "SELECT id, source, external_id, title, url, geo_region, last_seen, source_state "
            "FROM jobs WHERE cluster_id=? AND id<>?",
            (job["cluster_id"], job["id"]),
        ).fetchall()
        siblings = [
            _as_posting(r)
            for r in rows
            if is_authoritative(r["source"])
            and _overlaps(job, _as_posting(r))
            and sibling_is_proof(conn, job["id"], r["id"], radius_miles, geo)
        ]
        if len(siblings) == 1:
            return _link_of(siblings[0])
    candidates = [
        p
        for p in index.get(company_key(job["company"] or ""), [])
        if _overlaps(job, p)
        and sibling_is_proof(conn, job["id"], int(p.extra["job_id"]), radius_miles, geo)
    ]
    match = match_posting(job["title"] or "", candidates, job["geo_region"] or "")
    return _link_of(match) if match else None


def _link_of(p: RawPosting) -> tuple[Link, str]:
    return Link(p.source, p.external_id, p.url), str(p.extra.get("source_state", ""))


# --- stored links: write, veto, justify (the caller owns the transaction) ------------


def save_link(conn: sqlite3.Connection, job_id: int, link: Link) -> int:
    """Store `link` on the job; rows changed (0 or 1). Never overwrites the
    user's manual link (see web/apply_link.MANUAL_LINK) or a job with a manual
    posting verdict. Does not commit."""
    from blastjob.web.apply_link import MANUAL_LINK  # late: it imports this module

    cur = conn.execute(
        "UPDATE jobs SET employer_link_source=?, employer_link_external_id=?, "
        "employer_link_url=? WHERE id=? AND posting_check_manual=0 "
        f"AND COALESCE(employer_link_source,'') <> '{MANUAL_LINK}'",
        (link.source, link.external_id, link.url, job_id),
    )
    return cur.rowcount


def rejected_link(conn: sqlite3.Connection, job_id: int, link: Link) -> bool:
    """Is `link` a posting the user marked 'not the same role' for this job?"""
    from blastjob.core import role_clusters as rc

    other = conn.execute(
        "SELECT id FROM jobs WHERE source=? AND external_id=?", (link.source, link.external_id)
    ).fetchone()
    if other is None:
        return False
    a, b = rc.pair_of(job_id, other["id"])
    decided = conn.execute(
        "SELECT decision FROM same_role_decisions WHERE job_a=? AND job_b=?", (a, b)
    ).fetchone()
    return decided is not None and decided["decision"] == rc.DIFFERENT


def link_still_proof(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    held_id: int,
    radius_miles: float,
    geo: DiscoveryConfig,
) -> bool:
    """A stored link to a posting we hold must still be justified. A link made
    through a cluster sibling stands only while `sibling_is_proof` does: the user
    may have said 'not the same role' since (or the cluster moved). A link to a
    posting outside the cluster with no decision between them was not a sibling
    link (it came from a board match) and is judged by the network path alone.
    `row` needs `id` and `cluster_id`."""
    from blastjob.core import role_clusters as rc

    held = conn.execute("SELECT cluster_id FROM jobs WHERE id=?", (held_id,)).fetchone()
    if held is None:
        return True
    a, b = rc.pair_of(row["id"], held_id)
    decided = conn.execute(
        "SELECT 1 FROM same_role_decisions WHERE job_a=? AND job_b=?", (a, b)
    ).fetchone()
    sibling = row["cluster_id"] is not None and row["cluster_id"] == held["cluster_id"]
    if decided is None and not sibling:
        return True
    return rc.sibling_is_proof(conn, row["id"], held_id, radius_miles, geo)


# --- network: resolve a board, fetch it, check a linked posting --------------


async def fetch_board(ats: str, token: str, title: str) -> list[RawPosting]:
    """Postings on one board, without touching the user's discovery config.
    Workday is searched for the title (its listing is windowed); Dayforce is
    listed unfiltered so the scope prefilter cannot hide the posting."""
    if ats in _FULL_BOARD_ATS:
        return await _FULL_BOARD_ATS[ats]("", token).fetch()
    if ats == "workday":
        wd = WorkdayAdapter("", token, queries=[title], max_pages=1)
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S, follow_redirects=True) as client:
            items = await wd._list_all(client)
        return [presence_stub("", wd.ref, i) for i in items]
    if ats == "dayforce":
        return await _dayforce_all(token)
    return []


async def _dayforce_all(token: str) -> list[RawPosting]:
    df = DayforceAdapter("", token)
    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S, follow_redirects=True) as client:
        csrf = await df._csrf_token(client)
        items = await df._list_all(client, csrf)
    return [dayforce_posting("", df.ref, i) for i in items]


async def check_link(link: Link) -> bool | None:
    """True = still posted, False = gone from a listing that proves absence,
    None = cannot tell. Raises on network failure; the caller treats that as
    no evidence at all."""
    bt = board_token(link)
    if bt is None:
        return None
    ats, token = bt
    if ats == "workday":
        still_open = await WorkdayAdapter("", token, queries=[""]).confirm_open(
            [(link.external_id, link.url)]
        )
        return link.external_id in still_open
    if ats == "dayforce":
        postings = await _dayforce_all(token)
        ids = {p.external_id for p in postings}
        page_cap = DayforceAdapter("", token).max_pages * DAYFORCE_PAGE_SIZE
        if link.external_id not in ids and len(postings) >= page_cap:
            return None  # listing hit the page cap: absence proves nothing
        return link.external_id in ids
    postings = await fetch_board(ats, token, "")
    return link.external_id in {p.external_id for p in postings}


async def find_page(company: str, title: str, region: str, jina_api_key: str) -> Link | None:
    """core/page_links.find_page_link (imported late: it imports this module)."""
    from blastjob.core import page_links

    return await page_links.find_page_link(company, title, region, jina_api_key)


async def check_page(link: Link, company: str, title: str, region: str) -> bool | None:
    """core/page_links.check_page: re-verifies company, title and region."""
    from blastjob.core import page_links

    return await page_links.check_page(link, company, title, region)


@dataclass(frozen=True)
class PageCheck:
    """A careers-page probe: the verdict (True open, False gone, None no
    evidence) and, for a limited page (#23) whose row lacks it, the ATS tenant
    root read off that page ("" = nothing to store)."""

    verdict: bool | None
    tenant_root: str = ""


async def probe_page(link: Link, company: str, title: str, region: str) -> PageCheck:
    """core/page_links.probe_page: check_page plus the tenant root it saw."""
    from blastjob.core import page_links

    return await page_links.probe_page(link, company, title, region)


async def page_description(
    link: Link, company: str, title: str, region: str
) -> tuple[str | None, str]:
    """core/page_links.page_description: the trusted posting's own text."""
    from blastjob.core import page_links

    return await page_links.page_description(link, company, title, region)


async def fetch_workday_detail(
    token: str, url: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> RawPosting | None:
    """The CURRENT Workday detail for a posting URL (title, region, text), read
    from the board token like discovery/workday_locations._fix_for. None = the
    posting answers 404/410. Raises on an unmappable URL or any other failure."""
    wd = WorkdayAdapter("", token, queries=[""])
    detail_url = wd.detail_url_from_public(url)
    if not detail_url:
        raise ValueError("unmappable workday url")
    async with httpx.AsyncClient(
        timeout=_PROBE_TIMEOUT_S, follow_redirects=True, transport=transport
    ) as client:
        payload = await _capped_json(client, detail_url)
    if payload is None:
        return None
    info = (payload or {}).get("jobPostingInfo") or {}
    _city, region = split_location(info.get("location") or "")
    return RawPosting(
        source=wd.source,
        external_id=str(info.get("jobReqId") or ""),
        title=(info.get("title") or "").strip(),
        url=info.get("externalUrl") or url,
        description=html_to_text(info.get("jobDescription") or ""),
        region=region,
    )


async def _capped_json(client: httpx.AsyncClient, url: str) -> Any:
    """GET `url` as JSON through `MAX_DETAIL_BYTES`. None = 404/410; any other
    non-2xx status raises, and so does a body past the cap (a truncated JSON
    document is worse than an error)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    async with client.stream("GET", url, headers=headers) as resp:
        if resp.status_code in CLOSED_STATUS:
            return None
        resp.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > MAX_DETAIL_BYTES:
                raise ValueError(f"workday detail too large (> {MAX_DETAIL_BYTES} bytes)")
            chunks.append(chunk)
    return json.loads(b"".join(chunks))


@dataclass(frozen=True)
class LinkDeps:
    """Every network touchpoint of the closure pass, injectable for tests."""

    jina_api_key: str = ""
    probe_company: Callable[[str], list[BoardHit]] = resolve.probe_company
    probe_workday: Callable[[str, str], list[BoardHit]] = resolve.probe_workday
    probe_dayforce: Callable[[str, str], list[BoardHit]] = resolve.probe_dayforce
    fetch_board: Callable[[str, str, str], Awaitable[list[RawPosting]]] = fetch_board
    check_link: Callable[[Link], Awaitable[bool | None]] = check_link
    find_page: Callable[..., Awaitable[Link | None]] = find_page
    check_page: Callable[[Link, str, str, str], Awaitable[bool | None]] = check_page
    # The closure pass's page probe (verdict + tenant root, #23). None = derive
    # it from `check_page` with no root, which is what a test fake gets.
    probe_page: Callable[[Link, str, str, str], Awaitable[PageCheck]] | None = None
    fetch_workday_detail: Callable[[str, str], Awaitable[RawPosting | None]] = fetch_workday_detail
    page_description: Callable[[Link, str, str, str], Awaitable[tuple[str | None, str]]] = (
        page_description
    )


# --- request budget ----------------------------------------------------------------
#
# The budget counts REQUEST UNITS: each metered call is charged its worst-case
# number of HTTP requests, read from the code it fans out to. Retries inside one
# request (discovery.http backoff) are NOT counted, and a board-cache hit in
# `default_deps` is still charged its full weight, so the figure over-counts a
# cached run rather than under-counting an uncached one.

_BOARD_SLUGS = 3  # resolve.slug_variants: compact, hyphen, spaced
_BOARD_APIS = 3  # greenhouse, lever, ashby per slug
UNITS_PROBE_COMPANY = _BOARD_SLUGS * _BOARD_APIS
_PROBE_CANDIDATES = 6  # resolve.probe_*: `candidates[:6]` confirmed
UNITS_PROBE_WORKDAY = 1 + _PROBE_CANDIDATES  # one Jina search + a count POST each
UNITS_PROBE_DAYFORCE = 2 + 2 * _PROBE_CANDIDATES  # two searches + (csrf, search) each
UNITS_DAYFORCE_LISTING = 1 + DAYFORCE_MAX_PAGES  # csrf + one POST per page
_PAGE_HOPS = PAGE_MAX_HOPS + 1  # page_fetch.safe_get: the request and every redirect
_PAGE_CANDIDATES = 3  # page_links.MAX_CANDIDATES
UNITS_FIND_PAGE = 1 + _PAGE_CANDIDATES * _PAGE_HOPS  # one Jina search + candidate pages
UNITS_PAGE_FETCH = _PAGE_HOPS  # check_page / page_description: one safe_get


class BudgetExhausted(Exception):
    """The request budget is spent; stop resolving."""


class RequestBudget:
    """A count of request units. `take` is all-or-nothing and thread-safe
    (the probes run in worker threads)."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    @property
    def exhausted(self) -> bool:
        return self.remaining == 0

    def take(self, n: int = 1) -> bool:
        with self._lock:
            if self.used + n > self.limit:
                return False
            self.used += n
            return True


Weight = int | Callable[..., int]


def _spend(budget: RequestBudget, units: int) -> None:
    if not budget.take(units):
        raise BudgetExhausted(f"request budget of {budget.limit} units cannot cover {units}")


def _units(weight: Weight, args: tuple[Any, ...], kwargs: dict[str, Any]) -> int:
    return weight if isinstance(weight, int) else weight(*args, **kwargs)


def _metered_sync(fn: Callable[..., Any], budget: RequestBudget, weight: Weight) -> Any:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        _spend(budget, _units(weight, args, kwargs))
        return fn(*args, **kwargs)

    return wrapper


def _metered_async(fn: Callable[..., Awaitable[Any]], budget: RequestBudget, weight: Weight) -> Any:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        _spend(budget, _units(weight, args, kwargs))
        return await fn(*args, **kwargs)

    return wrapper


def _board_units(ats: str, *_rest: Any) -> int:
    return UNITS_DAYFORCE_LISTING if ats == "dayforce" else 1


def _link_units(link: Link) -> int:
    return UNITS_DAYFORCE_LISTING if link.source.startswith("dayforce:") else 1


def budgeted(deps: LinkDeps, budget: RequestBudget) -> LinkDeps:
    """`deps` with EVERY outbound callable metered: each call takes its worst-case
    request units from `budget` (see the weights above) and raises BudgetExhausted
    when they do not fit, spending nothing."""
    return replace(
        deps,
        probe_company=_metered_sync(deps.probe_company, budget, UNITS_PROBE_COMPANY),
        probe_workday=_metered_sync(deps.probe_workday, budget, UNITS_PROBE_WORKDAY),
        probe_dayforce=_metered_sync(deps.probe_dayforce, budget, UNITS_PROBE_DAYFORCE),
        fetch_board=_metered_async(deps.fetch_board, budget, _board_units),
        check_link=_metered_async(deps.check_link, budget, _link_units),
        find_page=_metered_async(deps.find_page, budget, UNITS_FIND_PAGE),
        check_page=_metered_async(deps.check_page, budget, UNITS_PAGE_FETCH),
        probe_page=(
            _metered_async(deps.probe_page, budget, UNITS_PAGE_FETCH) if deps.probe_page else None
        ),
        fetch_workday_detail=_metered_async(deps.fetch_workday_detail, budget, 1),
        page_description=_metered_async(deps.page_description, budget, UNITS_PAGE_FETCH),
    )


def default_deps() -> LinkDeps:
    """Live deps with a per-run board cache, so five jobs linked to one
    Greenhouse board cost one listing fetch."""
    cache: dict[tuple[str, str, str], list[RawPosting]] = {}

    async def cached_fetch(ats: str, token: str, title: str) -> list[RawPosting]:
        key = (ats, token, title if ats == "workday" else "")
        if key not in cache:
            cache[key] = await fetch_board(ats, token, title)
        return cache[key]

    async def cached_check(link: Link) -> bool | None:
        bt = board_token(link)
        if bt is None or bt[0] not in _FULL_BOARD_ATS:
            return await check_link(link)
        postings = await cached_fetch(bt[0], bt[1], "")
        return link.external_id in {p.external_id for p in postings}

    return LinkDeps(
        jina_api_key=os.environ.get("JINA_API_KEY", ""),
        fetch_board=cached_fetch,
        check_link=cached_check,
        probe_page=probe_page,
    )


@dataclass(frozen=True)
class Match:
    """A resolved employer posting: the link to store and, for a board match,
    the posting as the board listed it (a page link carries none)."""

    link: Link
    posting: RawPosting | None


async def resolve_match(company: str, title: str, region: str, deps: LinkDeps) -> Match | None:
    """Resolve the company's boards, fetch each one that is really this
    company's, and match only if exactly one posting across them matches.
    Only when the boards found NOTHING (not when they were ambiguous) and a
    Jina key is set, fall back to the employer's own careers page."""
    hits = list(await asyncio.to_thread(deps.probe_company, company))
    if deps.jina_api_key:
        hits += await asyncio.to_thread(deps.probe_workday, company, deps.jina_api_key)
        hits += await asyncio.to_thread(deps.probe_dayforce, company, deps.jina_api_key)
    found: dict[tuple[str, str], RawPosting] = {}
    ambiguous = False
    for hit in hits:
        if not board_matches_company(hit.ats, hit.token, company):
            continue
        postings = await deps.fetch_board(hit.ats, hit.token, title)
        match = match_posting(title, postings, region)
        if match is not None:
            found[(match.source, match.external_id)] = match
        else:
            ambiguous = ambiguous or any(match_posting(title, [p], region) for p in postings)
    if not found and not ambiguous and deps.jina_api_key:
        page = await deps.find_page(company, title, region, deps.jina_api_key)
        return Match(page, None) if page is not None else None
    if len(found) != 1:
        return None
    (m,) = found.values()
    return Match(Link(m.source, m.external_id, m.url), m)


async def resolve_link(company: str, title: str, region: str, deps: LinkDeps) -> Link | None:
    """`resolve_match`, link only."""
    match = await resolve_match(company, title, region, deps)
    return match.link if match is not None else None


# --- the employer's description, revalidated -----------------------------------------


async def verified_description(
    link: Link, company: str, title: str, region: str, deps: LinkDeps
) -> tuple[str | None, str]:
    """(text, reason) for an employer link, after FRESH trust revalidation: the
    board still belongs to the company and exactly this posting is still the one
    confident match for the title and region on CURRENT data. Reasons: 'ok',
    'title_drift' (the id now names another role, or the match is no longer
    unique), 'gone', 'company_mismatch', 'manual', 'no_text', 'error:<Type>'.
    A manual link is never fetched. BudgetExhausted propagates. The text is NOT
    normalized; the caller uses `normalize_employer_jd`."""
    from blastjob.web.apply_link import MANUAL_LINK  # late: it imports this module

    if link.source == MANUAL_LINK:
        return None, "manual"
    try:
        if link.source.startswith("page:"):
            from blastjob.core import page_links  # late: it imports this module

            if not page_links.full_trust(page_links.page_url(link), company):
                return None, "company_mismatch"  # limited trust (#23); spend nothing
            return await deps.page_description(link, company, title, region)
        return await _board_description(link, company, title, region, deps)
    except BudgetExhausted:
        raise
    except Exception as e:  # noqa: BLE001 -- a failed fetch is no evidence either way
        return None, f"error:{type(e).__name__}"


async def _board_description(
    link: Link, company: str, title: str, region: str, deps: LinkDeps
) -> tuple[str | None, str]:
    bt = board_token(link)
    if bt is None:
        return None, "no_text"
    ats, token = bt
    if not board_matches_company(ats, token, company):
        return None, "company_mismatch"
    if ats == "workday":
        posting = await deps.fetch_workday_detail(token, link.url)
        if posting is None:
            return None, "gone"
        if match_posting(title, [posting], region) is None:
            return None, "title_drift"
        return _text_of(posting)
    postings = await deps.fetch_board(ats, token, title)
    if not any(p.external_id == link.external_id for p in postings):
        return None, "gone"
    match = match_posting(title, postings, region)
    if match is None or match.external_id != link.external_id:
        return None, "title_drift"
    return _text_of(match)


def _text_of(posting: RawPosting) -> tuple[str | None, str]:
    text = posting.description or ""
    return (text, "ok") if text.strip() else (None, "no_text")

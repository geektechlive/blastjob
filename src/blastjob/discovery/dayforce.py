"""Dayforce (Ceridian) candidate-portal adapter (no key).

A good share of regional credit unions, hospitals and
mid-market employers post on Dayforce, which neither Adzuna nor the Workday
adapter sees. The public candidate portal is a JavaScript SPA with no job text in
its HTML, but it is backed by an unauthenticated JSON endpoint (probed and
verified live 2026-09-05, see docs/how-it-works/discovery.md):

  POST https://jobs.dayforcehcm.com/api/geo/{clientNamespace}/jobposting/search
       {"clientNamespace": ..., "jobBoardCode": ..., "cultureCode": "en-US",
        "paginationStart": 0}
    -> {"jobPostings": [{jobPostingId, jobReqId, jobTitle,
                         jobDescription (HTML), postingLocations: [...],
                         postingStartTimestampUTC, hasVirtualLocation}],
        "maxCount": N, "offset": M, "count": K}

Two things differ from Workday and shape this adapter:

1. **The JD ships with the listing.** There is no detail call — one POST per page
   of 25 returns everything. So `known_ids` is accepted for interface parity but
   never produces an empty-description stub: skipping a JD we already hold in hand
   would discard free data, not save a request.
2. **The endpoint wants an anonymous CSRF handshake.** It is not a login — the
   portal reports `"status":"unauthenticated"` and `/api/auth/session` returns
   `{}` — but a bare POST answers 403. `GET /api/auth/csrf` sets a cookie and
   returns a token that must be echoed in an `x-csrf-token` header. One extra GET
   per board per run.

`queries` is likewise accepted for parity and deliberately unused: the unfiltered
sweep returns the whole board in a handful of pages, which is both
cheaper and more complete than N `searchText`-filtered sweeps.

Titles from plainly individual-contributor job families are dropped with the same
rule the matcher applies, so a Teller-heavy board doesn't fill the DB with rows
that could never surface.

`source` is per board ("dayforce:{client}") so one board's outage can never
stale-mark another board's postings.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from blastjob.discovery.base import RawPosting
from blastjob.discovery.http import request_json
from blastjob.discovery.textutil import html_to_text
from blastjob.matching.prefilter import classify_scope

log = logging.getLogger(__name__)

SOURCE_PREFIX = "dayforce"
API_HOST = "https://jobs.dayforcehcm.com"
CSRF_URL = f"{API_HOST}/api/auth/csrf"
# Server-fixed: 25 comes back whether or not a size is asked for, and the portal
# hard-codes maxPageSize: 25.
PAGE_SIZE = 25
# Hard cap so a server-side change to maxCount can never spin an unbounded loop.
MAX_PAGES = 20
DEFAULT_CULTURE = "en-US"


@dataclass(frozen=True)
class BoardRef:
    client: str  # clientNamespace, e.g. "acmecu"
    board: str  # jobBoardCode, e.g. "CANDIDATEPORTAL"

    @property
    def search_url(self) -> str:
        return f"{API_HOST}/api/geo/{self.client}/jobposting/search"

    def public_url(self, job_posting_id: str) -> str:
        return f"{API_HOST}/{self.client}/{self.board}/jobs/{job_posting_id}"

    @property
    def token(self) -> str:
        return f"{self.client}/{self.board}"


def parse_board_token(token: str) -> BoardRef:
    """'acmecu/CANDIDATEPORTAL' -> BoardRef. Raises ValueError on any other
    shape so a typo in config fails loudly at adapter construction.

    Note the host is NOT part of the token: the API lives on the constant
    jobs.dayforcehcm.com, and the per-tenant usNNN.dayforcehcm.com hosts are only
    redirect origins. The board code is required (it goes in the request body)
    and is used as written — only the uppercase form is verified live.
    """
    parts = [p.strip() for p in (token or "").strip().strip("/").split("/")]
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"dayforce token must be 'clientNamespace/jobBoardCode', got {token!r}")
    client, board = parts
    if "." in client:
        raise ValueError(
            f"dayforce token takes no host — use 'clientNamespace/jobBoardCode', got {token!r}"
        )
    return BoardRef(client=client, board=board)


def source_for(ref: BoardRef) -> str:
    return f"{SOURCE_PREFIX}:{ref.client}"


def search_body(ref: BoardRef, pagination_start: int, culture: str = DEFAULT_CULTURE) -> dict:
    """The request body is the UI hook's params object verbatim; these four
    fields are the ones the server actually requires."""
    return {
        "clientNamespace": ref.client,
        "jobBoardCode": ref.board,
        "cultureCode": culture,
        "paginationStart": pagination_start,
    }


def parse_listing(payload: dict) -> list[dict]:
    """jobPostings -> raw posting dicts, dropping anything without an id."""
    out: list[dict] = []
    for j in payload.get("jobPostings") or []:
        if not j.get("jobPostingId"):
            continue
        out.append(j)
    return out


def total_count(payload: dict) -> int:
    try:
        return int(payload.get("maxCount") or 0)
    except (TypeError, ValueError):
        return 0


def external_id_for(item: dict) -> str:
    """The posting id, which is the identity in the public URL. `jobReqId` can
    repeat across several postings of one requisition, so it is not identity."""
    return str(item.get("jobPostingId") or "")


def title_wanted(
    title: str,
    keep_terms: Sequence[str] = (),
    drop_terms: Sequence[str] = (),
) -> bool:
    """Drop titles the matcher would drop anyway (configured drop families)
    before they reach the DB. Same rule as the scope prefilter (`[discovery]
    title_keep_terms`/`title_drop_terms`), so nothing surfaceable is lost —
    a live board sweep keeps most of its postings and discards the dropped
    bulk. Both empty (an unconfigured install) gates nothing."""
    keep, _ = classify_scope(title, "", keep_terms, drop_terms)
    return keep


def _location(item: dict) -> tuple[str, str, str, str]:
    """(location_raw, city, region, country) from the first posting location.

    Dayforce gives structured fields, so this never guesses by splitting a
    string the way `split_location` has to for free-form sources.
    """
    locs = item.get("postingLocations") or []
    if not isinstance(locs, list) or not locs:
        return "", "", "", ""
    first = locs[0] if isinstance(locs[0], dict) else {}
    return (
        str(first.get("formattedAddress") or ""),
        str(first.get("cityName") or ""),
        str(first.get("stateCode") or ""),
        str(first.get("isoCountryCode") or ""),
    )


def parse_posting(company: str, ref: BoardRef, item: dict) -> RawPosting:
    location_raw, city, region, country = _location(item)
    virtual = bool(item.get("hasVirtualLocation"))
    return RawPosting(
        source=source_for(ref),
        external_id=external_id_for(item),
        company=company,
        title=str(item.get("jobTitle") or "").strip(),
        url=ref.public_url(external_id_for(item)),
        description=html_to_text(str(item.get("jobDescription") or "")),
        location_raw=location_raw,
        city=city,
        region=region,
        country=country,
        remote=True if virtual else None,
        workplace_type="Remote" if virtual else "",
        posted_at=str(item.get("postingStartTimestampUTC") or ""),
        extra={
            "req_id": str(item.get("jobReqId") or ""),
            "is_evergreen": bool(item.get("isEvergreen")),
            "board": ref.board,
        },
    )


async def count_postings(
    ref: BoardRef,
    *,
    timeout_s: int = 15,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int | None:
    """Confirm a candidate board by fetching its total posting count (page 0
    only). Returns None on any failure (bad token, 403, network error, missing
    csrfToken) so a resolver probe can treat it as "not a real board" without
    raising. 0 is a valid, confirmed-empty board."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout_s, follow_redirects=True, transport=transport
        ) as client:
            csrf_payload = await request_json(client, "GET", CSRF_URL)
            token = str((csrf_payload or {}).get("csrfToken") or "")
            if not token:
                return None
            payload = await request_json(
                client,
                "POST",
                ref.search_url,
                json=search_body(ref, 0),
                headers={"x-csrf-token": token},
            )
            return total_count(payload)
    except Exception:
        return None


class DayforceAdapter:
    def __init__(
        self,
        company: str,
        token: str,
        *,
        queries: list[str] | None = None,
        timeout_s: int = 15,
        max_retries: int = 3,
        known_ids: set[str] | None = None,
        max_pages: int = MAX_PAGES,
        culture: str = DEFAULT_CULTURE,
        keep_terms: Sequence[str] = (),
        drop_terms: Sequence[str] = (),
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.company = company
        self.ref = parse_board_token(token)
        self.source = source_for(self.ref)
        # Accepted for parity with the other board adapters; see module docstring
        # for why the unfiltered sweep is used instead.
        self.queries = [q for q in (queries or []) if q.strip()]
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.known_ids = set(known_ids or ())
        self.max_pages = max_pages
        self.culture = culture
        self.keep_terms = list(keep_terms)
        self.drop_terms = list(drop_terms)
        self._transport = transport  # tests inject httpx.MockTransport
        # Set when the page cap cut the listing short: the items are real but the
        # pipeline must not stale-mark this source on a partial read.
        self.partial_listing_note = ""

    async def _csrf_token(self, client: httpx.AsyncClient) -> str:
        """Anonymous double-submit token. Not a login: it authenticates nothing,
        it just satisfies the portal's own anti-CSRF check on the public search."""
        payload = await request_json(client, "GET", CSRF_URL, max_retries=self.max_retries)
        token = str((payload or {}).get("csrfToken") or "")
        if not token:
            raise ValueError("dayforce: no csrfToken in /api/auth/csrf response")
        return token

    async def _list_all(self, client: httpx.AsyncClient, csrf: str) -> list[dict]:
        seen: dict[str, dict] = {}
        start = 0
        expected = 0
        ended_empty = capped = False
        for page in range(self.max_pages):
            payload = await request_json(
                client,
                "POST",
                self.ref.search_url,
                json=search_body(self.ref, start, self.culture),
                headers={"x-csrf-token": csrf},
                max_retries=self.max_retries,
            )
            items = parse_listing(payload)
            expected = max(expected, total_count(payload))
            for it in items:
                seen.setdefault(external_id_for(it), it)
            start += PAGE_SIZE
            # Stop on the server's own total, or on an empty page if that total
            # ever lies high. MAX_PAGES is the backstop either way.
            if not items:
                ended_empty = True
                break
            if start >= total_count(payload):
                break
            capped = page == self.max_pages - 1
        # An empty page short of the server's own total is a truncated snapshot,
        # and the pipeline would stale-mark the unread tail as closed. Fail the
        # source instead; nothing is marked. A board bigger than the page cap is
        # not a failure: return what was read and flag it so nothing is marked.
        if ended_empty and len(seen) < expected:
            raise ValueError(
                f"dayforce {self.ref.token}: listing is short ({len(seen)} of {expected} "
                f"postings); not treating it as a snapshot"
            )
        self.partial_listing_note = (
            f"dayforce {self.ref.token}: page cap hit ({len(seen)} of {expected} postings read)"
            if capped and len(seen) < expected
            else ""
        )
        return list(seen.values())

    async def fetch(self) -> list[RawPosting]:
        async with httpx.AsyncClient(
            timeout=self.timeout_s, follow_redirects=True, transport=self._transport
        ) as client:
            csrf = await self._csrf_token(client)
            items = await self._list_all(client, csrf)
        wanted = [
            i
            for i in items
            if title_wanted(str(i.get("jobTitle") or ""), self.keep_terms, self.drop_terms)
        ]
        log.info(
            "dayforce %s: %d postings, %d after scope prefilter",
            self.ref.token,
            len(items),
            len(wanted),
        )
        return [parse_posting(self.company, self.ref, i) for i in wanted]

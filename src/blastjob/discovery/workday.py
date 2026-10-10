"""Workday career-site adapter (no key).

Most traditional employers post on Workday, which Adzuna only partially
mirrors. One real, public example board: Workday's own careers site, token
`workday/wd5/Workday` (verified live: POST
https://workday.wd5.myworkdayjobs.com/wday/cxs/workday/Workday/jobs). Workday
exposes an unauthenticated JSON API behind every public career site:

  POST {base}/wday/cxs/{tenant}/{site}/jobs
       {"appliedFacets":{}, "limit":20, "offset":0, "searchText":"director"}
    -> {"total": N, "jobPostings": [{title, externalPath, locationsText,
                                    postedOn, bulletFields:[reqId]}]}
  GET  {base}/wday/cxs/{tenant}/{site}{externalPath}
    -> {"jobPostingInfo": {title, jobDescription(HTML), location, remoteType,
                           jobReqId, externalUrl, startDate, ...}}

where base = https://{tenant}.{wdN}.myworkdayjobs.com. A board is configured as
`ats = "workday"`, `token = "tenant/wdN/site"` (shape verified live 2026-09-04).

Cost control: the listing is cheap but the JD needs one GET per posting, so the
pipeline passes the ids it already holds a JD for and those are emitted as
PRESENCE stubs (`presence_stub`) — the upsert then refreshes last_seen without
touching the stored JD, the stored location, or the stored remote flag. A
presence stub deliberately carries NO location and NO remote hint: the listing
only knows a building name or a count ("5 Locations"), and writing that over
the detail-derived city/region is how live rows lost their real location.
`stub_posting` (the detail-fetch-FAILED path) is different — nothing was stored
from a detail fetch there, so the listing location is better than nothing.

Titles that are plainly individual-contributor families are dropped before the
detail fetch using the same rule the matcher applies, so nothing that could
surface is lost.

Sync mode: WINDOWED, not a full snapshot. Each `searchText` is capped at
MAX_PAGES_PER_QUERY x page_size = 100 relevance-ranked hits, so the set a run
sees ROTATES. Measured on the live DB (2026-09-12): workday:aah oscillated
58->58->61->64->69->62 fetched across consecutive days while producing ~17
"new" and ~15 "gone" rows a day and accumulating 66 false-stale rows against 62
active. Hence `stale_after_days` — a posting pushed out of the window
reappears within days and its last_seen moves; only a genuinely closed one goes
quiet for STALE_AFTER_DAYS, and `confirm_open` then checks the detail endpoint
before anything is marked gone.

`source` is per board ("workday:{tenant}") so a single board's outage can
never stale-mark another board's postings.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from blastjob.discovery.base import RawPosting
from blastjob.discovery.http import request_json
from blastjob.discovery.textutil import html_to_text, split_location
from blastjob.matching.prefilter import classify_scope

log = logging.getLogger(__name__)

SOURCE_PREFIX = "workday"
DEFAULT_PAGE_SIZE = 20
MAX_PAGES_PER_QUERY = 5
DETAIL_CONCURRENCY = 4

# Days a posting may go unseen before it is a closure candidate. Load-bearing
# and measured, not a guess: the live window rotates day to day (see module
# docstring), so this must comfortably exceed the rotation period. Two weeks of
# total silence on a board we poll daily is not a rotation artefact.
STALE_AFTER_DAYS = 14

# A closure probe costs one GET per candidate; cap it per board per run so 30
# configured boards can't turn one daily run into thousands of requests.
CLOSED_STATUS = (404, 410)
_LOCALE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")


@dataclass(frozen=True)
class BoardRef:
    tenant: str
    wd: str
    site: str

    @property
    def base(self) -> str:
        return f"https://{self.tenant}.{self.wd}.myworkdayjobs.com"

    @property
    def jobs_url(self) -> str:
        return f"{self.base}/wday/cxs/{self.tenant}/{self.site}/jobs"

    def detail_url(self, external_path: str) -> str:
        return f"{self.base}/wday/cxs/{self.tenant}/{self.site}{external_path}"

    def public_url(self, external_path: str) -> str:
        return f"{self.base}/{self.site}{external_path}"

    @property
    def token(self) -> str:
        return f"{self.tenant}/{self.wd}/{self.site}"


def parse_board_token(token: str) -> BoardRef:
    """'tenant/wd5/site' -> BoardRef. Raises ValueError on any other shape so a
    typo in config fails loudly at adapter construction, not silently at 3am."""
    parts = [p.strip() for p in (token or "").strip().strip("/").split("/")]
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"workday token must be 'tenant/wdN/site', got {token!r}")
    tenant, wd, site = parts
    if not wd.startswith("wd") or not wd[2:].isdigit():
        raise ValueError(f"workday token middle part must look like 'wd5', got {wd!r}")
    return BoardRef(tenant=tenant, wd=wd, site=site)


def source_for(ref: BoardRef) -> str:
    return f"{SOURCE_PREFIX}:{ref.tenant}"


def parse_listing(payload: dict) -> list[dict]:
    """jobPostings -> [{title, external_path, locations_text, posted_on, req_id}]."""
    out: list[dict] = []
    for j in payload.get("jobPostings") or []:
        path = j.get("externalPath") or ""
        if not path:
            continue
        bullets = j.get("bulletFields") or []
        out.append(
            {
                "title": (j.get("title") or "").strip(),
                "external_path": path,
                "locations_text": j.get("locationsText") or "",
                "posted_on": j.get("postedOn") or "",
                "req_id": str(bullets[0]).strip() if bullets else "",
            }
        )
    return out


def external_id_for(item: dict) -> str:
    """Requisition id when the board publishes one (stable across re-posts),
    else the posting path."""
    return item.get("req_id") or item.get("external_path") or ""


def title_wanted(
    title: str,
    keep_terms: Sequence[str] = (),
    drop_terms: Sequence[str] = (),
) -> bool:
    """Skip the detail fetch for titles the matcher would drop anyway (configured
    drop families). Same rule as the scope prefilter (`[discovery]
    title_keep_terms`/`title_drop_terms`), so nothing surfaceable is lost. Both
    empty (an unconfigured install) means nothing is gated here either."""
    keep, _ = classify_scope(title, "", keep_terms, drop_terms)
    return keep


def _workplace(remote_type: str) -> str:
    rt = (remote_type or "").strip().lower()
    if rt == "remote":
        return "Remote"
    if rt == "hybrid":
        return "Hybrid"
    if rt in ("on-site", "onsite", "on site"):
        return "OnSite"
    return ""


def presence_stub(company: str, ref: BoardRef, item: dict) -> RawPosting:
    """A posting we already hold a JD for: refresh presence and NOTHING else.

    No description, no location, no remote flag — the listing does not actually
    know any of them (its `locationsText` is "5 Locations" or a building name),
    and `normalize.to_job_columns` omits absent fields so the upsert leaves the
    stored, detail-derived values alone.
    """
    return RawPosting(
        source=source_for(ref),
        external_id=external_id_for(item),
        company=company,
        title=item.get("title", ""),
        url=ref.public_url(item["external_path"]),
        description="",
        extra={"detail_skipped": True},
    )


def stub_posting(company: str, ref: BoardRef, item: dict) -> RawPosting:
    """The detail fetch was attempted and FAILED. There is no stored detail to
    protect (this path is only reached for postings not in `known_ids`), so the
    listing's location is better than nothing; the JD is retried next run."""
    city, region = split_location(item.get("locations_text", ""))
    return RawPosting(
        source=source_for(ref),
        external_id=external_id_for(item),
        company=company,
        title=item.get("title", ""),
        url=ref.public_url(item["external_path"]),
        description="",
        location_raw=item.get("locations_text", ""),
        city=city,
        region=region,
        extra={"detail_skipped": True, "detail_failed": True},
    )


def parse_detail(company: str, ref: BoardRef, item: dict, detail: dict) -> RawPosting:
    info = detail.get("jobPostingInfo") or {}
    loc = info.get("location") or item.get("locations_text", "")
    city, region = split_location(loc)
    remote_type = info.get("remoteType") or ""
    workplace = _workplace(remote_type)
    country = info.get("country")
    if isinstance(country, dict):
        country_name = str(country.get("descriptor", ""))
    else:
        country_name = str(country or "")
    return RawPosting(
        source=source_for(ref),
        external_id=external_id_for(item),
        company=company,
        title=(info.get("title") or item.get("title") or "").strip(),
        url=info.get("externalUrl") or ref.public_url(item["external_path"]),
        description=html_to_text(info.get("jobDescription") or ""),
        location_raw=loc,
        city=city,
        region=region,
        country=country_name,
        remote=True if workplace == "Remote" else None,
        workplace_type=workplace,
        posted_at=str(info.get("startDate") or ""),
        extra={
            "req_id": info.get("jobReqId") or item.get("req_id", ""),
            "time_type": info.get("timeType") or "",
            "posted_on": item.get("posted_on", ""),
        },
    )


class WorkdayAdapter:
    def __init__(
        self,
        company: str,
        token: str,
        *,
        queries: list[str],
        timeout_s: int = 15,
        max_retries: int = 3,
        known_ids: set[str] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = MAX_PAGES_PER_QUERY,
        keep_terms: Sequence[str] = (),
        drop_terms: Sequence[str] = (),
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.company = company
        self.ref = parse_board_token(token)
        self.source = source_for(self.ref)
        self.queries = [q for q in queries if q.strip()] or [""]
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.known_ids = set(known_ids or ())
        self.page_size = page_size
        self.max_pages = max_pages
        self.keep_terms = list(keep_terms)
        self.drop_terms = list(drop_terms)
        self._transport = transport  # tests inject httpx.MockTransport

    # Windowed snapshot: absence from one run is rotation, not closure.
    snapshot = True
    stale_after_days = STALE_AFTER_DAYS

    def detail_url_from_public(self, url: str) -> str:
        """Public posting URL -> its cxs detail URL, for the closure probe.

        The stored url is either `{base}/{site}{externalPath}` or Workday's own
        `externalUrl` (which can carry a locale segment); both end in the
        `/job/...` external path, which is all the cxs endpoint needs.
        """
        parsed = urlparse(url or "")
        host = (parsed.netloc or "").lower()
        if not host.endswith("myworkdayjobs.com"):
            return ""
        parts = [seg for seg in (parsed.path or "").strip("/").split("/") if seg]
        if parts and _LOCALE.match(parts[0]):
            parts = parts[1:]
        if "job" not in parts:
            return ""
        external_path = "/" + "/".join(parts[parts.index("job") :])
        return self.ref.detail_url(external_path)

    async def confirm_open(self, candidates: list[tuple[str, str]]) -> set[str]:
        """Given (external_id, url) pairs, return the ids Workday still serves.

        Only an explicit 404/410 counts as closed. A timeout, a 5xx, a URL we
        can't map, or anything else comes back as OPEN — the pipeline stale-marks
        on this answer, and a network blip is not evidence a role closed.
        """
        if not candidates:
            return set()
        still_open: set[str] = set()
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def probe(client: httpx.AsyncClient, external_id: str, url: str) -> None:
            detail = self.detail_url_from_public(url)
            if not detail:
                still_open.add(external_id)  # unmappable: never our excuse to bin it
                return
            async with sem:
                try:
                    resp = await client.get(detail, headers={"Accept": "application/json"})
                except Exception as e:  # noqa: BLE001 — fail-soft, treat as open
                    log.warning(
                        "workday %s: closure probe failed for %s (%s)",
                        self.ref.token,
                        external_id,
                        type(e).__name__,
                    )
                    still_open.add(external_id)
                    return
            if resp.status_code not in CLOSED_STATUS:
                still_open.add(external_id)

        async with httpx.AsyncClient(
            timeout=self.timeout_s, follow_redirects=True, transport=self._transport
        ) as client:
            await asyncio.gather(*(probe(client, i, u) for i, u in candidates))
        return still_open

    async def _list_all(self, client: httpx.AsyncClient) -> list[dict]:
        seen: dict[str, dict] = {}
        for q in self.queries:
            offset = 0
            for _ in range(self.max_pages):
                payload = await request_json(
                    client,
                    "POST",
                    self.ref.jobs_url,
                    json={
                        "appliedFacets": {},
                        "limit": self.page_size,
                        "offset": offset,
                        "searchText": q,
                    },
                    max_retries=self.max_retries,
                )
                items = parse_listing(payload)
                for it in items:
                    seen.setdefault(it["external_path"], it)
                total = int(payload.get("total") or 0)
                offset += self.page_size
                if len(items) < self.page_size or offset >= total:
                    break
        return list(seen.values())

    async def _one(
        self, client: httpx.AsyncClient, sem: asyncio.Semaphore, item: dict
    ) -> RawPosting:
        if external_id_for(item) in self.known_ids:
            return presence_stub(self.company, self.ref, item)
        async with sem:
            try:
                detail = await request_json(
                    client,
                    "GET",
                    self.ref.detail_url(item["external_path"]),
                    max_retries=self.max_retries,
                )
            except Exception as e:  # noqa: BLE001 — keep the posting alive, retry JD next run
                log.warning(
                    "workday %s: detail fetch failed for %s (%s); keeping stub",
                    self.ref.token,
                    item["external_path"],
                    type(e).__name__,
                )
                return stub_posting(self.company, self.ref, item)
        return parse_detail(self.company, self.ref, item, detail)

    async def fetch(self) -> list[RawPosting]:
        async with httpx.AsyncClient(
            timeout=self.timeout_s, follow_redirects=True, transport=self._transport
        ) as client:
            items = await self._list_all(client)
            wanted = [
                i for i in items if title_wanted(i["title"], self.keep_terms, self.drop_terms)
            ]
            sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
            return list(await asyncio.gather(*(self._one(client, sem, i) for i in wanted)))

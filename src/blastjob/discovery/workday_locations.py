"""Backfill for issue #3: Workday rows stored before `additionalLocations` was read.

A Workday row that already holds a JD is refreshed by a presence stub, which by
design never touches the stored location (see `workday.presence_stub`). So a
req whose primary location is out of state, but which also lists a home-state
location, stays geo-rejected forever even after the parser was fixed. This
re-reads the detail for exactly those rows: Workday, active, undecided
(new/surfaced/queued), and rejected as an affirmative other place (verified,
not remote, no distance). It stores the full "A; B; C" location list and
nothing else; the caller re-runs matching on the rows it changed.

`plan` makes the requests and writes nothing; `apply` writes. A probe that
fails is skipped (a network blip is not evidence of anything).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass

import httpx

from blastjob.discovery import workday
from blastjob.discovery.http import request_json
from blastjob.matching.geo import assess_geo
from blastjob.models.config import BlastJobConfig

log = logging.getLogger(__name__)

_CANDIDATES = (
    "SELECT id, source, company, title, url, geo_location_raw, geo_city, geo_region "
    "FROM jobs WHERE source LIKE 'workday:%' AND source_state='active' "
    "AND review_state IN ('new','surfaced','queued') AND geo_status='verified' "
    "AND COALESCE(geo_remote,0)=0 AND geo_distance_mi IS NULL ORDER BY id"
)


@dataclass(frozen=True)
class LocationFix:
    job_id: int
    company: str
    title: str
    old_location: str
    new_location: str
    now_passes: bool


def _adapters(cfg: BlastJobConfig, transport: httpx.AsyncBaseTransport | None) -> dict:
    """source -> a WorkdayAdapter for each configured Workday board."""
    out: dict[str, workday.WorkdayAdapter] = {}
    for b in cfg.discovery.target_boards:
        if b.ats.lower() == "workday" and b.token:
            a = workday.WorkdayAdapter(b.company, b.token, queries=[], transport=transport)
            out[a.source] = a
    return out


async def _fix_for(
    client: httpx.AsyncClient, adapter: workday.WorkdayAdapter, row: sqlite3.Row, cfg
) -> LocationFix | None:
    detail_url = adapter.detail_url_from_public(row["url"] or "")
    if not detail_url:
        return None
    try:
        payload = await request_json(client, "GET", detail_url, max_retries=1)
    except Exception as e:  # noqa: BLE001 -- fail-soft, see module docstring
        log.warning("workday location refetch failed for job %s (%s)", row["id"], type(e).__name__)
        return None
    info = (payload or {}).get("jobPostingInfo") or {}
    if not workday.additional_locations(info):
        return None
    new = workday.listed_locations(info, info.get("location") or row["geo_location_raw"] or "")
    if new == (row["geo_location_raw"] or ""):
        return None
    _s, _d, passes, _c = assess_geo(
        remote=False,
        city=row["geo_city"] or "",
        region=row["geo_region"] or "",
        location_raw=new,
        cfg=cfg.discovery,
    )
    return LocationFix(
        int(row["id"]),
        row["company"] or "",
        row["title"] or "",
        row["geo_location_raw"] or "",
        new,
        passes,
    )


async def plan(
    conn: sqlite3.Connection,
    cfg: BlastJobConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[LocationFix]:
    """The location changes the backfill would make. Writes nothing."""
    adapters = _adapters(cfg, transport)
    rows = [r for r in conn.execute(_CANDIDATES).fetchall() if r["source"] in adapters]
    if not rows:
        return []
    sem = asyncio.Semaphore(workday.DETAIL_CONCURRENCY)

    async def one(client: httpx.AsyncClient, row: sqlite3.Row) -> LocationFix | None:
        async with sem:
            return await _fix_for(client, adapters[row["source"]], row, cfg)

    timeout = cfg.discovery.provider_timeout_s
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        found = await asyncio.gather(*(one(client, r) for r in rows))
    return [f for f in found if f is not None]


def apply(conn: sqlite3.Connection, fixes: list[LocationFix]) -> list[int]:
    """Store each fix's full location list; returns the ids written. Guarded on
    the old value, so a row a discovery run changed meanwhile is left alone."""
    changed: list[int] = []
    for f in fixes:
        cur = conn.execute(
            "UPDATE jobs SET geo_location_raw=? WHERE id=? AND geo_location_raw=?",
            (f.new_location, f.job_id, f.old_location),
        )
        if cur.rowcount == 1:
            changed.append(f.job_id)
    conn.commit()
    return changed


def format_report(fixes: list[LocationFix], *, applied: list[int] | None) -> str:
    if not fixes:
        return "No geo-rejected Workday rows list additional locations."
    lines = [
        f"#{f.job_id} {f.company} | {f.title}\n    {f.old_location} -> {f.new_location}"
        f"  ({'now passes geo' if f.now_passes else 'still out of area'})"
        for f in fixes
    ]
    if applied is None:
        lines.append(f"{len(fixes)} row(s) would change (report only; nothing written).")
    else:
        lines.append(f"{len(applied)} row(s) updated; re-scoring them now.")
    return "\n".join(lines)

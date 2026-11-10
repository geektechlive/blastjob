"""Reconcile stored search-firm rows with what their own page says.

Why: `SearchFirmAdapter.fetch` skips the page read for a posting we already hold a JD
for, so a stored row is never looked at again by discovery. Anything learned later would
otherwise reach only new rows: a place read from the page text (#113) and a search the
firm has since marked filled (#115). This pass runs over the stored `searchfirm:` rows.
Rules: [discovery](../../../docs/how-it-works/discovery.md#retained-search-firms), ADR 0072/0073.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from blastjob.core import closure
from blastjob.discovery.searchfirms import SOURCE_PREFIX, ReaderFn, filled_sentence, infer_geo
from blastjob.models.config import BlastJobConfig

log = logging.getLogger(__name__)

# Stored, still-open rows whose page is re-read per run. Each Jina read costs; the rotation
# (oldest `firm_page_checked_at` first, blanks first) gives every row a turn.
RECHECK_CAP = 5

_UNPLACED = (
    "SELECT id, title, raw_jd FROM jobs "
    "WHERE source LIKE ? AND TRIM(COALESCE(geo_location_raw,'')) = '' "
    "AND TRIM(COALESCE(raw_jd,'')) <> '' ORDER BY id"
)
# Open = not pinned by the user, not already closed, not archived. A pinned row's verdict
# is the user's; a closed one needs no more proof.
_OPEN_QUEUE = (
    "SELECT id, url FROM jobs WHERE source LIKE ? AND posting_check_manual=0 "
    "AND COALESCE(posting_check,'') <> 'closed' AND COALESCE(posting_closed_at,'')='' "
    "AND review_state <> 'archived' AND TRIM(COALESCE(url,'')) <> '' "
    "ORDER BY COALESCE(firm_page_checked_at,''), id LIMIT ?"
)


@dataclass
class ReconcileResult:
    placed: list[int] = field(default_factory=list)
    read: int = 0
    closed: list[int] = field(default_factory=list)


def backfill_search_firm_places(cfg: BlastJobConfig, conn: sqlite3.Connection) -> list[int]:
    """Give every `searchfirm:` row with no place the place its stored page text names.

    Uses the same inference as a fresh read (earliest configured city, else state) and never
    touches `remote`. A row that already has a place is left alone. Guarded on the empty
    value so a row changed meanwhile is not overwritten. Returns the ids written."""
    d = cfg.discovery
    places = d.search_firm_places or d.search_firm_regions
    changed: list[int] = []
    for row in conn.execute(_UNPLACED, (f"{SOURCE_PREFIX}:%",)).fetchall():
        location_raw, region, _remote = infer_geo(
            row["title"] or "", row["raw_jd"] or "", places, d.home_state
        )
        if not location_raw:
            continue
        cur = conn.execute(
            "UPDATE jobs SET geo_location_raw=?, "
            "geo_region=CASE WHEN TRIM(COALESCE(geo_region,''))='' THEN ? ELSE geo_region END "
            "WHERE id=? AND TRIM(COALESCE(geo_location_raw,''))=''",
            (location_raw, region, int(row["id"])),
        )
        if cur.rowcount == 1:
            changed.append(int(row["id"]))
    conn.commit()
    return changed


async def _reread_one(
    cfg: BlastJobConfig, conn: sqlite3.Connection, row: sqlite3.Row, read: ReaderFn, now: datetime
) -> tuple[bool, bool]:
    """(read_ok, closed) for one stored row. A failed or empty read proves nothing: no stamp,
    no change. A successful one stamps `firm_page_checked_at` and closes only on the filled
    sentence."""
    try:
        text = await read(str(row["url"]))
    except Exception as e:  # noqa: BLE001 -- fail-soft: a failed read is no evidence either way
        log.warning("searchfirm re-read failed for job %s: %s", row["id"], type(e).__name__)
        return False, False
    if not text:
        return False, False
    conn.execute(
        "UPDATE jobs SET firm_page_checked_at=? WHERE id=?",
        (now.isoformat(timespec="seconds"), row["id"]),
    )
    conn.commit()
    if not filled_sentence(text):
        return True, False
    closed = closure.close_from_page_evidence(
        conn, int(row["id"]), now=now, auto=cfg.discovery.closure_auto, page_text=text
    )
    return True, closed


async def reconcile_search_firm_rows(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    *,
    read: ReaderFn | None,
    now: datetime | None = None,
) -> ReconcileResult:
    """Backfill places from stored text (no network), then re-read up to `RECHECK_CAP` open
    rows and close any whose page carries the filled sentence. With `read=None` (no reader
    and no Jina key) only the backfill runs; the skipped re-read is not an all-clear."""
    result = ReconcileResult(placed=backfill_search_firm_places(cfg, conn))
    if read is None:
        return result
    stamp = now or datetime.now()
    queue = conn.execute(_OPEN_QUEUE, (f"{SOURCE_PREFIX}:%", RECHECK_CAP)).fetchall()
    for row in queue:
        read_ok, closed = await _reread_one(cfg, conn, row, read, stamp)
        result.read += int(read_ok)
        if closed:
            result.closed.append(int(row["id"]))
    return result

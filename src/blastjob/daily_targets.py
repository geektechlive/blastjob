"""The target-company news step of the daily run (#85).

`targets_step` scans the core targets that are due (weekly, at most
`target_store.MAX_COMPANIES_PER_RUN` a run) and records the outcome on the run result.
`pending_news` reads what the digest should carry: events no sent digest has carried yet.
Kept out of `daily.py`, which is past the file-size budget. See
docs/how-it-works/daily.md.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from typing import TYPE_CHECKING

from blastjob import logging as applog
from blastjob.core import network as net
from blastjob.core import target_store as ts
from blastjob.core import target_watch
from blastjob.core.page_fetch import host_of
from blastjob.daily_digest import TargetNewsItem
from blastjob.llm.cost import CostTracker
from blastjob.models.config import BlastJobConfig

if TYPE_CHECKING:
    from blastjob.daily import DailyResult

log = logging.getLogger(__name__)

NOTE_KEY = "target_watch"


def _run_time(today: dt.date | None) -> dt.datetime:
    """Now, on `today`'s date when the run was given one (tests and replays)."""
    now = dt.datetime.now()
    return now if today is None else dt.datetime.combine(today, now.time())


async def targets_step(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    res: DailyResult,
    *,
    dry_run: bool = False,
    today: dt.date | None = None,
) -> None:
    """Scan what is due. Never raises: a failure lands in `res.errors`. With no targets
    this does nothing at all (no key lookup, no network)."""
    tracker = CostTracker()
    try:
        summary = await target_watch.scan_due_targets(
            cfg, conn, dry_run=dry_run, now=_run_time(today), cost_tracker=tracker
        )
    except Exception as e:  # noqa: BLE001 - the digest must still go out
        conn.rollback()
        log.exception("target watch failed")
        res.errors["target_watch"] = applog.redact(f"{type(e).__name__}: {e}")
        return
    res.target_cost = tracker.total_call_cost
    res.target_failed = summary.failed
    if summary.not_run:
        res.skipped_steps.append(f"target scans ({summary.not_run} still due)")
    if summary.unscanned_no_key:
        res.notes[NOTE_KEY] = (
            f"JINA_API_KEY is not set, so {summary.unscanned_no_key} target "
            "companies were not scanned."
        )
    log.info(
        "target watch: scanned %d failed %d not run %d new events %d locked %d",
        summary.scanned,
        summary.failed,
        summary.not_run,
        summary.new_events,
        summary.skipped_locked,
    )


def pending_news(conn: sqlite3.Connection) -> list[TargetNewsItem]:
    """Unnotified, undismissed events of current targets, newest first, with the number of
    people the owner knows at each company."""
    rows = ts.unnotified_events(conn)
    contacts = net.warm_counts(conn, {r["company_key"] for r in rows})
    return [
        TargetNewsItem(
            id=int(r["id"]),
            company_key=r["company_key"],
            company=r["company"],
            event_type=r["event_type"],
            published_on=r["published_on"],
            span=r["source_span"],
            host=host_of(r["source_url"]),
            basis=r["source_basis"],
            contacts=contacts.get(r["company_key"], 0),
        )
        for r in rows
    ]

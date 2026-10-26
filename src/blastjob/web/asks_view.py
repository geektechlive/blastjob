"""Template context for the outreach-ask partials on the job page (#97).

Shared by the job page, the contacts partial and the ask routes so each renders the same
asks list and the same per-person "latest ask" badge. See docs/how-it-works/network.md.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import uuid

from blastjob.core import outreach_asks as oa


def new_request_token() -> str:
    """A one-use token for an ask form; a replayed submit returns the existing row."""
    return uuid.uuid4().hex


def ask_context(conn: sqlite3.Connection, job_id: int, error: str = "") -> dict[str, object]:
    rows = oa.asks_for_job(conn, job_id)
    latest = oa.latest_by_recipient(rows)
    return {
        "asks": rows,
        "ask_latest_net": latest.network,
        "ask_latest_contact": latest.contact,
        "ask_kinds": oa.KINDS,
        "ask_kind_labels": oa.KIND_LABELS,
        "ask_channels": oa.CHANNELS,
        "ask_statuses": oa.STATUSES,
        "ask_today": dt.date.today().isoformat(),
        "ask_error": error,
    }

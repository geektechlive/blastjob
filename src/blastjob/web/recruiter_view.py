"""Template context for the recruiter desk rows (#98).

Shared by the desk page, the recruiter routes and the job-less ask update so each renders the
same row. See docs/how-it-works/network.md.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

from blastjob.core import outreach_asks as oa
from blastjob.core import recruiter_status as rs


def row_options() -> dict[str, object]:
    return {
        "status_choices": ("auto", *rs.STATUSES),
        "status_labels": {"auto": "automatic", **rs.STATUS_LABELS},
        "reason_max": rs.MAX_REASON_CHARS,
        "ask_channels": oa.CHANNELS,
        "ask_statuses": oa.STATUSES,
        "ask_today": dt.date.today().isoformat(),
    }


def desk_context(conn: sqlite3.Connection) -> dict[str, object]:
    return {"recruiters": rs.status_desk(conn), **row_options()}


def row_context(conn: sqlite3.Connection, contact_id: int) -> dict[str, object] | None:
    """The context for one desk row, or None when the contact is not on the desk."""
    row = next((r for r in rs.status_desk(conn) if r["id"] == contact_id), None)
    if row is None:
        return None
    return {"r": row, **row_options()}

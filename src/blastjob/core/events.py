"""Application events: what happened in an application, and when (migration 42).

One row per thing that happened -- applied, a screen, a thank-you note, a
recruiter reply, the posting closing. Writers:

- `web/service.set_application_state` (source 'state'), inside the same
  transaction as the move, so a state and its event commit together or not at
  all;
- the closure lifecycle in `core/closure.py` (source 'closure');
- the reply scanner in `discovery/replies.py` (source 'replies'; ref is the
  Message-ID);
- the history backfill in `core/backfill.py` (source 'backfill');
- the user, from the job page (source 'manual' -- the only events a delete may
  remove, and only on the job they belong to).

Newest first is `ORDER BY substr(at, 1, 10) DESC, id DESC` everywhere (calendar
day, then insertion order), so events on the same day have one answer and a
later-recorded event for an earlier moment never outranks a newer day.
`insert_event` never commits; `add_event` does.
"""

from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Iterable

EVENT_KINDS: tuple[str, ...] = (
    "applied",
    "screen",
    "interview",
    "thank_you",
    "follow_up",
    "recruiter_reply",
    "offer",
    "rejected",
    "withdrawn",
    "ghosted",
    "posting_closed",
    "filled",
    "note",
)

EVENT_SOURCES: tuple[str, ...] = ("manual", "state", "closure", "replies", "backfill")

KIND_LABELS: dict[str, str] = {
    "applied": "Applied",
    "screen": "Screen",
    "interview": "Interview",
    "thank_you": "Thank-you sent",
    "follow_up": "Follow-up sent",
    "recruiter_reply": "Recruiter reply",
    "offer": "Offer",
    "rejected": "Rejected",
    "withdrawn": "Withdrawn",
    "ghosted": "Ghosted",
    "posting_closed": "Posting closed",
    "filled": "Filled",
    "note": "Note",
}

# Application states that never become an event: '' is "no application yet"
# and 'drafted' is bookkeeping about the packet, not something that happened.
_SILENT_STATES = frozenset({"", "drafted"})

# By DAY, then insertion order. Manual entries carry a bare date and automated
# ones a timestamp; comparing them as strings would put a thank-you logged
# today below this morning's automated event.
_NEWEST_FIRST = "ORDER BY substr(at, 1, 10) DESC, id DESC"


def normalize_at(at: str) -> str:
    """An ISO date or datetime, returned stripped. Anything else raises."""
    text = (at or "").strip()
    if not text:
        raise ValueError("an event needs a date")
    try:
        datetime.date.fromisoformat(text)
        return text
    except ValueError:
        pass
    try:
        datetime.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"not an ISO date or datetime: {text!r}") from exc
    return text


def state_event_kind(new_state: str) -> str | None:
    """The event a move INTO `new_state` records, or None for a silent state."""
    state = new_state or ""
    if state in _SILENT_STATES or state not in EVENT_KINDS:
        return None
    return state


def insert_event(
    conn: sqlite3.Connection,
    job_id: int,
    kind: str,
    at: str,
    note: str = "",
    ref: str = "",
    source: str = "manual",
) -> int:
    """Insert one event inside the caller's transaction. Never commits."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind: {kind!r}")
    if source not in EVENT_SOURCES:
        raise ValueError(f"unknown event source: {source!r}")
    stamp = normalize_at(at)
    created = datetime.datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO application_events (job_id, kind, at, note, ref, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, kind, stamp, note or "", ref or "", source, created),
    )
    return int(cur.lastrowid or 0)


def add_event(
    conn: sqlite3.Connection,
    job_id: int,
    kind: str,
    at: str,
    note: str = "",
    ref: str = "",
    source: str = "manual",
) -> int:
    """`insert_event` + commit."""
    event_id = insert_event(conn, job_id, kind, at, note=note, ref=ref, source=source)
    conn.commit()
    return event_id


def has_event(conn: sqlite3.Connection, job_id: int, kind: str, ref: str | None = None) -> bool:
    """True if the job already has an event of `kind` (and `ref`, when given)."""
    sql = "SELECT 1 FROM application_events WHERE job_id=? AND kind=?"
    params: tuple = (job_id, kind)
    if ref is not None:
        sql += " AND ref=?"
        params = (*params, ref)
    return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def events_for(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    """Every event for one job, newest first."""
    return conn.execute(
        f"SELECT * FROM application_events WHERE job_id=? {_NEWEST_FIRST}", (job_id,)
    ).fetchall()


def latest_event(conn: sqlite3.Connection, job_ids: Iterable[int]) -> dict[int, sqlite3.Row]:
    """The newest event per job, for a whole list in ONE query. Jobs with no
    event are absent from the result."""
    ids = list(dict.fromkeys(int(j) for j in job_ids))
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT * FROM ("
        "  SELECT e.*, ROW_NUMBER() OVER ("
        f"    PARTITION BY job_id {_NEWEST_FIRST}"
        "  ) AS rn FROM application_events e"
        f"  WHERE job_id IN ({marks})"
        ") WHERE rn = 1",
        ids,
    ).fetchall()
    return {int(r["job_id"]): r for r in rows}


def delete_manual_event(conn: sqlite3.Connection, job_id: int, event_id: int) -> bool:
    """Delete one of the user's own events. Refuses (False) anything automated and
    anything belonging to another job."""
    cur = conn.execute(
        "DELETE FROM application_events WHERE id=? AND job_id=? AND source='manual'",
        (event_id, job_id),
    )
    conn.commit()
    return cur.rowcount == 1

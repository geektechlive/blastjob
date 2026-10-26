"""Recruiter relationship status, last touch and the touch-again reminder (#98, ADR 0062).

A recruiter's relationship status is separate from the outcome of any one ask. It comes from
confirmed interaction facts: an ask they answered, or LinkedIn message history. `active` and
`do_not_contact` are only ever set by the owner. The last touch is the later of the newest
`asked_on` and the date of `last_message_at`; the page says which one it came from.

The reminder selects `spoke` and `active` recruiters whose last touch is old enough, and the
digest stamps `recruiter_reminded_for` with exactly the last-touch date it rendered. Nothing
here sends or contacts anyone (draft-only). See docs/how-it-works/network.md and
docs/how-it-works/daily.md.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from blastjob.core import network as net
from blastjob.core import network_views as nv

STATUSES = ("not_contacted", "reached_out", "spoke", "active", "do_not_contact")
STATUS_LABELS = {
    "not_contacted": "not contacted",
    "reached_out": "reached out",
    "spoke": "spoke",
    "active": "active",
    "do_not_contact": "do not contact",
}
# Only the owner sets these; the automatic status never yields them.
OWNER_ONLY = frozenset({"active", "do_not_contact"})
REMINDER_STATUSES = frozenset({"spoke", "active"})
ANSWERED_ASK_STATUSES = ("replied", "helped", "declined")

MAX_REASON_CHARS = 120
MAX_DIGEST_RECRUITERS = 5
MAX_DESK_ASKS = 3

SOURCE_ASK = "ask"
SOURCE_MESSAGE = "LinkedIn message"


class StatusError(ValueError):
    """A status or reason the rules reject; the message is safe to show the owner."""


@dataclass(frozen=True)
class Standing:
    status: str
    source: str  # "set" (the owner chose it) or "auto"
    reason: str
    last_touch: str  # ISO date, "" when there has been no touch
    touch_source: str  # SOURCE_ASK, SOURCE_MESSAGE or ""
    reminded_for: str


@dataclass(frozen=True)
class Reminder:
    contact_id: int
    name: str
    company: str
    last_touch: str
    days_since: int


def _date_part(value: str | None) -> dt.date | None:
    """The date of an ISO date or datetime; None when blank or unparseable (never raises)."""
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def auto_status(asks: int, answered: int, message_count: int) -> str:
    """Interaction facts only: they answered an ask or there is message history means `spoke`;
    asks with no answer and no messages means `reached_out`."""
    if answered > 0 or message_count > 0:
        return "spoke"
    return "reached_out" if asks > 0 else "not_contacted"


def last_touch(last_ask_on: str | None, last_message_at: str | None) -> tuple[str, str]:
    """(ISO date, source) of the later touch. A tie reads as the ask. ("", "") for none."""
    ask, msg = _date_part(last_ask_on), _date_part(last_message_at)
    if ask is None and msg is None:
        return "", ""
    if msg is not None and (ask is None or msg > ask):
        return msg.isoformat(), SOURCE_MESSAGE
    assert ask is not None
    return ask.isoformat(), SOURCE_ASK


def _standing(row: sqlite3.Row) -> Standing:
    stored = row["recruiter_status"]
    if stored in STATUSES:
        status, source = stored, "set"
    else:
        status = auto_status(row["asks"], row["answered"], row["message_count"] or 0)
        source = "auto"
    touch, touch_source = last_touch(row["last_ask_on"], row["last_message_at"])
    return Standing(
        status=status,
        source=source,
        reason=(row["recruiter_status_reason"] or "") if status == "do_not_contact" else "",
        last_touch=touch,
        touch_source=touch_source,
        reminded_for=row["recruiter_reminded_for"] or "",
    )


_STANDING_SQL = (
    "SELECT nc.id, nc.recruiter_status, nc.recruiter_status_reason, nc.recruiter_reminded_for, "
    "nc.last_message_at, nc.message_count, "
    "COALESCE(a.asks, 0) AS asks, COALESCE(a.answered, 0) AS answered, a.last_ask_on "
    "FROM network_contacts nc LEFT JOIN ("
    "SELECT network_contact_id AS cid, COUNT(*) AS asks, "
    "SUM(status IN ('replied','helped','declined')) AS answered, MAX(asked_on) AS last_ask_on "
    "FROM outreach_asks WHERE network_contact_id IS NOT NULL GROUP BY network_contact_id"
    ") a ON a.cid = nc.id "
    f"WHERE {net.EFFECTIVE_RECRUITER_SQL}"
)


def standings(conn: sqlite3.Connection) -> dict[int, Standing]:
    """The standing of every effective recruiter, in one query."""
    return {int(r["id"]): _standing(r) for r in conn.execute(_STANDING_SQL).fetchall()}


def is_effective_recruiter(conn: sqlite3.Connection, contact_id: int) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM network_contacts WHERE id=? AND {net.EFFECTIVE_RECRUITER_SQL}",
        (contact_id,),
    ).fetchone()
    return row is not None


def set_status(conn: sqlite3.Connection, contact_id: int, status: str, reason: str = "") -> bool:
    """Store the owner's status ("auto" clears it). A reason is required for do-not-contact and
    kept only there. False for an unknown contact; StatusError for a rejected value."""
    text = (reason or "").strip()
    if len(text) > MAX_REASON_CHARS:
        raise StatusError(f"reason is at most {MAX_REASON_CHARS} characters")
    if status != "auto" and status not in STATUSES:
        raise StatusError("status must be one of: auto, " + ", ".join(STATUSES))
    if status == "do_not_contact" and not text:
        raise StatusError("do not contact needs a reason")
    stored = None if status == "auto" else status
    kept = text if status == "do_not_contact" else ""
    cur = conn.execute(
        "UPDATE network_contacts SET recruiter_status=?, recruiter_status_reason=? WHERE id=?",
        (stored, kept, contact_id),
    )
    conn.commit()
    return cur.rowcount == 1


def recent_asks(conn: sqlite3.Connection, contact_ids: Iterable[int]) -> dict[int, list]:
    """Each contact's newest job-less asks (at most MAX_DESK_ASKS), by date asked then id."""
    ids = [int(i) for i in contact_ids]
    out: dict[int, list] = {i: [] for i in ids}
    if not ids:
        return out
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM outreach_asks WHERE job_id IS NULL AND network_contact_id IN ({marks}) "
        "ORDER BY asked_on DESC, id DESC",
        ids,
    ).fetchall()
    for r in rows:
        bucket = out[int(r["network_contact_id"])]
        if len(bucket) < MAX_DESK_ASKS:
            bucket.append(r)
    return out


def status_desk(conn: sqlite3.Connection, today: dt.date | None = None) -> list[dict]:
    """The recruiter desk (group, then warmth) with each row's standing and recent notes."""
    day = today or dt.date.today()
    desk = nv.recruiter_desk(conn, today=day.isoformat())
    stand = standings(conn)
    asks = recent_asks(conn, (d["id"] for d in desk))
    rows = []
    for d in desk:
        s = stand.get(d["id"])
        if s is None:
            continue
        rows.append(
            {
                **d,
                "status": s.status,
                "status_source": s.source,
                "status_label": STATUS_LABELS[s.status],
                "status_reason": s.reason,
                "last_touch": s.last_touch,
                "touch_source": s.touch_source,
                "reminded_for": s.reminded_for,
                "asks": asks.get(d["id"], []),
            }
        )
    return rows


def due_reminders(
    conn: sqlite3.Connection,
    today: dt.date,
    *,
    touch_days: int,
    lookback_days: int,
) -> list[Reminder]:
    """Recruiters due a touch, warmest first, at most MAX_DIGEST_RECRUITERS.

    `spoke` or `active` (stored or automatic); the last touch at least `touch_days` ago and
    not already reminded for; a `spoke` also within `lookback_days` (an `active` is always
    eligible). `do_not_contact` and `not_contacted` never; no touch date, never.
    """
    picked: list[tuple[float, Reminder]] = []
    for r in status_desk(conn, today):
        if r["status"] not in REMINDER_STATUSES or not r["last_touch"]:
            continue
        touched = dt.date.fromisoformat(r["last_touch"])
        days = (today - touched).days
        if days < touch_days or r["reminded_for"] == r["last_touch"]:
            continue
        if r["status"] == "spoke" and days > lookback_days:
            continue
        picked.append(
            (
                r["warmth_score"],
                Reminder(r["id"], r["name"], r["company"] or "", r["last_touch"], days),
            )
        )
    picked.sort(key=lambda p: (-p[0], p[1].contact_id))
    return [p[1] for p in picked[:MAX_DIGEST_RECRUITERS]]


def mark_reminded(conn: sqlite3.Connection, items: Iterable[tuple[int, str]]) -> None:
    """Stamp each (contact id, last-touch date rendered). A touch logged since selection has a
    newer date, which no longer equals the stamp, so its reminder re-arms on its own."""
    conn.executemany(
        "UPDATE network_contacts SET recruiter_reminded_for=? WHERE id=? "
        "AND COALESCE(recruiter_reminded_for,'') != ?",
        [(date, int(i), date) for i, date in items],
    )
    conn.commit()

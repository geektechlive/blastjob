"""Outreach asks: a record per intro, referral, recruiter note or advice ask (#97, ADR 0061).

An ask is a record, not an application stage. It names who was asked (a network contact or a
per-job contact), when, over which channel, what they said and when to follow up. A waiting ask
whose follow-up date has come is announced once in the daily digest ("Asks to follow up").

Pure functions on a connection; the routes and the digest call them. Nothing here sends or
contacts anyone (draft-only). See docs/how-it-works/network.md and docs/how-it-works/daily.md.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from blastjob.core import network_views as nv

KINDS = ("intro", "referral", "recruiter", "advice")
CHANNELS = ("email", "linkedin", "phone", "in_person", "other")
STATUSES = ("waiting", "replied", "helped", "declined", "no_reply")
CLOSING_STATUSES = frozenset({"helped", "declined", "no_reply"})
# A job-less ask (not about one job) is a note to a recruiter or a request for advice.
JOBLESS_KINDS = frozenset({"recruiter", "advice"})
KIND_LABELS = {
    "intro": "intro",
    "referral": "referral",
    "recruiter": "recruiter note",
    "advice": "advice",
}

MAX_NOTE_CHARS = 300
DEFAULT_FOLLOW_UP_DAYS = 7
MAX_DIGEST_ASKS = 10

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AskError(ValueError):
    """A field or recipient the ask rules reject; the message is safe to show the owner."""


@dataclass(frozen=True)
class Recipient:
    name: str
    network_contact_id: int | None
    contact_id: int | None
    company: str = ""


@dataclass(frozen=True)
class LatestAsks:
    """The newest ask per person, keyed by the person's id in each table."""

    network: dict[int, sqlite3.Row]
    contact: dict[int, sqlite3.Row]


def _iso(value: str, field: str) -> dt.date:
    if not _ISO_DATE.match(value or ""):
        raise AskError(f"{field} must be a date like 2026-10-26")
    try:
        return dt.date.fromisoformat(value)
    except ValueError as e:
        raise AskError(f"{field} must be a real date") from e


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def validate_kind(kind: str) -> str:
    if kind not in KINDS:
        raise AskError("kind must be one of: " + ", ".join(KINDS))
    return kind


def validate_recipient(
    conn: sqlite3.Connection,
    *,
    job_id: int | None,
    network_contact_id: int | None,
    contact_id: int | None,
) -> Recipient:
    """The one recipient check for `log_ask` and the draft route.

    A network contact tied to a job must be on that job's current warm path; a per-job
    contact must belong to the job. Exactly one of the two ids is given.
    """
    if (network_contact_id is None) == (contact_id is None):
        raise AskError("name exactly one person")
    if (
        job_id is not None
        and conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is None
    ):
        raise AskError("job not found")
    if network_contact_id is not None:
        return _network_recipient(conn, job_id, network_contact_id)
    assert contact_id is not None
    if job_id is None:
        raise AskError("an ask not tied to a job needs a person from your network")
    row = conn.execute("SELECT name, job_id FROM contacts WHERE id=?", (contact_id,)).fetchone()
    if row is None or int(row["job_id"]) != job_id:
        raise AskError("contact not found for this job")
    return Recipient(row["name"], None, contact_id)


def _network_recipient(
    conn: sqlite3.Connection, job_id: int | None, network_contact_id: int
) -> Recipient:
    if job_id is not None:
        on_path = {p["id"] for p in nv.warm_paths_for_job(conn, job_id)}
        if network_contact_id not in on_path:
            raise AskError("that person is not on this job's warm path")
    row = conn.execute(
        "SELECT full_name, company FROM network_contacts WHERE id=?", (network_contact_id,)
    ).fetchone()
    if row is None:
        raise AskError("network contact not found")
    return Recipient(row["full_name"], network_contact_id, None, row["company"] or "")


def _clean_note(note: str) -> str:
    text = (note or "").strip()
    if len(text) > MAX_NOTE_CHARS:
        raise AskError(f"note is at most {MAX_NOTE_CHARS} characters")
    return text


def _check_follow_up(follow_up_on: str, asked_on: dt.date) -> str:
    if not follow_up_on:
        return ""
    if _iso(follow_up_on, "follow-up date") < asked_on:
        raise AskError("the follow-up date cannot be before the ask date")
    return follow_up_on


def _token_row(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    if not token:
        return None
    return conn.execute(
        "SELECT id, job_id, network_contact_id, contact_id FROM outreach_asks "
        "WHERE request_token=?",
        (token,),
    ).fetchone()


def _replayed(
    row: sqlite3.Row, job_id: int | None, network_contact_id: int | None, contact_id: int | None
) -> int:
    """A replayed token returns its own row only when the request names the same job and
    person; otherwise it is rejected, so another job's row is never rendered."""
    if (row["job_id"], row["network_contact_id"], row["contact_id"]) != (
        job_id,
        network_contact_id,
        contact_id,
    ):
        raise AskError("that request token was already used for a different ask")
    return int(row["id"])


def log_ask(
    conn: sqlite3.Connection,
    *,
    job_id: int | None,
    network_contact_id: int | None,
    contact_id: int | None,
    kind: str,
    channel: str,
    asked_on: str,
    follow_up_on: str | None = None,
    note: str = "",
    request_token: str = "",
    today: dt.date | None = None,
) -> int:
    """Record an ask and return its id. `follow_up_on=None` means asked_on + 7 days; '' means
    none. A replayed `request_token` returns the existing row and writes nothing."""
    existing = _token_row(conn, request_token)
    if existing is not None:
        return _replayed(existing, job_id, network_contact_id, contact_id)
    validate_kind(kind)
    if channel not in CHANNELS:
        raise AskError("channel must be one of: " + ", ".join(CHANNELS))
    asked = _iso(asked_on, "ask date")
    if asked > (today or dt.date.today()):
        raise AskError("the ask date cannot be in the future")
    recipient = validate_recipient(
        conn, job_id=job_id, network_contact_id=network_contact_id, contact_id=contact_id
    )
    if job_id is None and kind not in JOBLESS_KINDS:
        raise AskError("an ask not tied to a job is a recruiter note or advice")
    clean = _clean_note(note)
    if follow_up_on is None:
        follow_up_on = (asked + dt.timedelta(days=DEFAULT_FOLLOW_UP_DAYS)).isoformat()
    follow = _check_follow_up(follow_up_on, asked)
    now = _now()
    try:
        cur = conn.execute(
            "INSERT INTO outreach_asks (job_id, network_contact_id, contact_id, recipient_name, "
            "kind, channel, asked_on, follow_up_on, note, request_token, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                recipient.network_contact_id,
                recipient.contact_id,
                recipient.name,
                kind,
                channel,
                asked_on,
                follow,
                clean,
                request_token or None,
                now,
                now,
            ),
        )
        if recipient.contact_id is not None:
            conn.execute(
                "UPDATE contacts SET contacted_at=? WHERE id=? AND COALESCE(contacted_at,'')=''",
                (now, recipient.contact_id),
            )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        raced = _token_row(conn, request_token)  # a concurrent double submit
        if raced is None:
            # A table CHECK the field checks above missed: a plain rejection, not a 500.
            raise AskError("that ask could not be saved; check the fields") from e
        return _replayed(raced, job_id, network_contact_id, contact_id)
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def update_ask(
    conn: sqlite3.Connection, ask_id: int, *, status: str, follow_up_on: str, note: str
) -> None:
    """Change status, follow-up date and note. A closing status clears the follow-up date.

    Not scoped to a job, deliberately: this is a single-owner app and the id is the handle."""
    row = conn.execute("SELECT asked_on FROM outreach_asks WHERE id=?", (ask_id,)).fetchone()
    if row is None:
        raise AskError("ask not found")
    if status not in STATUSES:
        raise AskError("status must be one of: " + ", ".join(STATUSES))
    clean = _clean_note(note)
    follow = (
        ""
        if status in CLOSING_STATUSES
        else _check_follow_up(follow_up_on.strip(), _iso(row["asked_on"], "ask date"))
    )
    conn.execute(
        "UPDATE outreach_asks SET status=?, follow_up_on=?, note=?, updated_at=? WHERE id=?",
        (status, follow, clean, _now(), ask_id),
    )
    conn.commit()


def get_ask(conn: sqlite3.Connection, ask_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM outreach_asks WHERE id=?", (ask_id,)).fetchone()


def asks_for_job(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    """Newest first: by the date asked, then by id, so a backfilled old ask is never 'newest'."""
    return conn.execute(
        "SELECT * FROM outreach_asks WHERE job_id=? ORDER BY asked_on DESC, id DESC", (job_id,)
    ).fetchall()


def latest_by_recipient(asks: Iterable[sqlite3.Row]) -> LatestAsks:
    """The newest ask per person from rows already ordered newest first."""
    network: dict[int, sqlite3.Row] = {}
    contact: dict[int, sqlite3.Row] = {}
    for a in asks:
        if a["network_contact_id"] is not None:
            network.setdefault(int(a["network_contact_id"]), a)
        if a["contact_id"] is not None:
            contact.setdefault(int(a["contact_id"]), a)
    return LatestAsks(network, contact)


def due_followups(conn: sqlite3.Connection, today: dt.date) -> list[sqlite3.Row]:
    """Waiting asks whose follow-up date has come and that no sent digest announced for that
    date. Most overdue first, then job score (NULL last), then id."""
    return conn.execute(
        "SELECT a.id, a.job_id, a.network_contact_id, a.recipient_name, a.kind, a.follow_up_on, "
        "a.note, j.title, j.company, j.alignment_score, nc.company AS network_company, "
        "CAST(julianday(:today) - julianday(a.follow_up_on) AS INTEGER) AS days_overdue "
        "FROM outreach_asks a LEFT JOIN jobs j ON j.id=a.job_id "
        "LEFT JOIN network_contacts nc ON nc.id=a.network_contact_id "
        "WHERE a.status='waiting' AND a.follow_up_on<>'' AND a.follow_up_on<=:today "
        "AND COALESCE(a.follow_up_notified_on,'')<>a.follow_up_on "
        "ORDER BY a.follow_up_on ASC, j.alignment_score IS NULL, j.alignment_score DESC, a.id ASC",
        {"today": today.isoformat()},
    ).fetchall()


def rendered_due_followups(conn: sqlite3.Connection, today: dt.date) -> list[sqlite3.Row]:
    """The asks the digest renders; the same rows are stamped after a send, the rest lead next
    time."""
    return due_followups(conn, today)[:MAX_DIGEST_ASKS]


def mark_followups_notified(conn: sqlite3.Connection, items: Iterable[tuple[int, str]]) -> None:
    """Stamp each (ask id, follow_up_on seen at selection). Compare-and-set: a date moved since
    selection no longer matches, so the new date stays armed."""
    conn.executemany(
        "UPDATE outreach_asks SET follow_up_notified_on=? WHERE id=? AND follow_up_on=?",
        [(date, int(i), date) for i, date in items],
    )
    conn.commit()

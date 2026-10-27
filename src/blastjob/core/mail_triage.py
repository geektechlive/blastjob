"""Recruiter-mail triage storage and decisions (#83).

`mail_triage` rows come from `discovery/label_mail.py`. Nothing here contacts anyone or touches
the mailbox: the owner reads the row on the Recruiter mail page and links it to a kept job, a
recruiter in the network, or dismisses it. See docs/how-it-works/discovery.md.

Rules worth knowing:

- Identity is (mailbox, uidvalidity, uid); the Message-ID is stored but not unique. After a
  UIDVALIDITY change the server renumbers mail, so a message whose Message-ID is already
  recorded under another validity is not recorded again (the owner has triaged it already),
  and the watermark still moves past it.
- A decision is a compare-and-set on `version`: it carries the version the page showed and
  updates `WHERE id=? AND version=?`; a miss is `StaleDecision` (409). Deciding a row that is
  already decided also needs the explicit `change` flag.
- Linking to a job records one `recruiter_reply` event (source `mail_label`, ref
  `label:<uidvalidity>:<uid>`) at the message's receive time. Changing or dismissing a link
  removes the event this feature made. The decision and the event change in one transaction.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Sequence
from typing import TYPE_CHECKING

from blastjob.core import events
from blastjob.core.network import EFFECTIVE_RECRUITER_SQL

if TYPE_CHECKING:
    from blastjob.discovery.label_mail import LabelBatch

EVENT_SOURCE = "mail_label"
DECIDED_WINDOW_DAYS = 60
ACTIONS = ("job", "contact", "dismiss")
MAX_ROW = 2**62  # SQLite integers are 63-bit; larger ids are out of range, not a 500
_NOTE_CHARS = 300


class TriageError(ValueError):
    """A decision the input does not allow (bad action or target); the message is shown."""


class TriageNotFound(LookupError):
    """No such triage row."""


class StaleDecision(RuntimeError):
    """The row changed since the page was rendered."""


def event_ref(uidvalidity: int, uid: int) -> str:
    return f"label:{uidvalidity}:{uid}"


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# --- recording ---------------------------------------------------------------------------


def cursors(conn: sqlite3.Connection, mailbox: str) -> dict[int, int]:
    """Highest passed UID per UIDVALIDITY for `mailbox`."""
    rows = conn.execute(
        "SELECT uidvalidity, last_uid FROM mail_triage_cursor WHERE mailbox=?", (mailbox,)
    ).fetchall()
    return {int(r["uidvalidity"]): int(r["last_uid"]) for r in rows}


_INSERT = (
    "INSERT OR IGNORE INTO mail_triage (mailbox, uidvalidity, uid, message_id, received_at,"
    " header_date, sender_name, sender_address, subject, snippet, auth_spf, auth_dkim,"
    " auth_dmarc, received_estimated, created_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def record_batch(conn: sqlite3.Connection, batch: LabelBatch) -> int:
    """Insert the batch's new rows and move the watermark, in one transaction. Returns the
    number of rows added."""
    added = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for m in batch.messages:
            if m.message_id and _seen_under_another_validity(conn, batch, m.message_id):
                continue
            cur = conn.execute(
                _INSERT,
                (
                    batch.mailbox,
                    batch.uidvalidity,
                    m.uid,
                    m.message_id,
                    m.received_at,
                    m.header_date,
                    m.sender_name,
                    m.sender_address,
                    m.subject,
                    m.snippet,
                    m.auth.spf,
                    m.auth.dkim,
                    m.auth.dmarc,
                    int(m.received_estimated),
                    _now(),
                ),
            )
            added += max(cur.rowcount, 0)
            _clear_failure(conn, batch, m.uid)
        _count_failures(conn, batch)
        if batch.last_uid > 0:
            conn.execute(
                "INSERT INTO mail_triage_cursor (mailbox, uidvalidity, last_uid)"
                " VALUES (?, ?, ?) ON CONFLICT(mailbox, uidvalidity)"
                " DO UPDATE SET last_uid=MAX(last_uid, excluded.last_uid)",
                (batch.mailbox, batch.uidvalidity, batch.last_uid),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return added


def failure_counts(conn: sqlite3.Connection, mailbox: str) -> dict[tuple[int, int], int]:
    """Consecutive failed runs per (uidvalidity, uid) for messages that would not fetch."""
    rows = conn.execute(
        "SELECT uidvalidity, uid, runs FROM mail_triage_failures WHERE mailbox=?", (mailbox,)
    ).fetchall()
    return {(int(r["uidvalidity"]), int(r["uid"])): int(r["runs"]) for r in rows}


def _clear_failure(conn: sqlite3.Connection, batch: LabelBatch, uid: int) -> None:
    conn.execute(
        "DELETE FROM mail_triage_failures WHERE mailbox=? AND uidvalidity=? AND uid=?"
        " AND given_up=0",
        (batch.mailbox, batch.uidvalidity, uid),
    )


def _count_failures(conn: sqlite3.Connection, batch: LabelBatch) -> None:
    """One more failed run for the message that stopped the sweep; the ones given up on are
    marked so the digest can say so once."""
    if batch.failed_uid:
        conn.execute(
            "INSERT INTO mail_triage_failures (mailbox, uidvalidity, uid, runs)"
            " VALUES (?, ?, ?, 1) ON CONFLICT(mailbox, uidvalidity, uid)"
            " DO UPDATE SET runs=runs+1",
            (batch.mailbox, batch.uidvalidity, batch.failed_uid),
        )
    conn.executemany(
        "UPDATE mail_triage_failures SET given_up=1 WHERE mailbox=? AND uidvalidity=? AND uid=?",
        [(batch.mailbox, batch.uidvalidity, uid) for uid in batch.given_up],
    )


def _seen_under_another_validity(
    conn: sqlite3.Connection, batch: LabelBatch, message_id: str
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM mail_triage WHERE mailbox=? AND message_id=? AND uidvalidity<>? LIMIT 1",
        (batch.mailbox, message_id, batch.uidvalidity),
    ).fetchone()
    return row is not None


# --- the digest's view ---------------------------------------------------------------------


def pending_ids(conn: sqlite3.Connection) -> list[int]:
    """New rows no sent digest has announced yet."""
    rows = conn.execute(
        "SELECT id FROM mail_triage WHERE status='new' AND notified_at IS NULL ORDER BY id"
    ).fetchall()
    return [int(r["id"]) for r in rows]


def mark_notified(conn: sqlite3.Connection, ids: Sequence[int]) -> None:
    """Stamp rows a SENT digest announced. Compare-and-set: a row already stamped keeps its
    first stamp."""
    if not ids:
        return
    now = _now()
    conn.executemany(
        "UPDATE mail_triage SET notified_at=? WHERE id=? AND notified_at IS NULL",
        [(now, i) for i in ids],
    )
    conn.commit()


def unreadable_pending(conn: sqlite3.Connection) -> list[int]:
    """Messages the reader gave up on that no sent digest has mentioned yet (row ids)."""
    rows = conn.execute(
        "SELECT rowid FROM mail_triage_failures WHERE given_up=1 AND notified_at IS NULL"
    ).fetchall()
    return [int(r[0]) for r in rows]


def mark_unreadable_notified(conn: sqlite3.Connection, ids: Sequence[int]) -> None:
    if not ids:
        return
    now = _now()
    conn.executemany(
        "UPDATE mail_triage_failures SET notified_at=? WHERE rowid=? AND notified_at IS NULL",
        [(now, i) for i in ids],
    )
    conn.commit()


def unreadable_note(count: int) -> str:
    if count == 1:
        return "1 recruiter-label message couldn't be read; open it in your mail"
    return f"{count} recruiter-label messages couldn't be read; open them in your mail"


# --- the page's view -------------------------------------------------------------------------


def list_rows(
    conn: sqlite3.Connection, today: dt.date | None = None
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """(new rows newest first, rows decided in the last 60 days newest decision first)."""
    since = ((today or dt.date.today()) - dt.timedelta(days=DECIDED_WINDOW_DAYS)).isoformat()
    new = conn.execute(
        "SELECT * FROM mail_triage WHERE status='new' ORDER BY received_at DESC, id DESC"
    ).fetchall()
    decided = conn.execute(
        "SELECT * FROM mail_triage WHERE status<>'new' AND substr(decided_at, 1, 10) >= ?"
        " ORDER BY decided_at DESC, id DESC",
        (since,),
    ).fetchall()
    return new, decided


# --- decisions ---------------------------------------------------------------------------------


def _check_target(conn: sqlite3.Connection, action: str, target_id: int | None) -> None:
    if action == "dismiss":
        return
    if target_id is None or not 0 < target_id < MAX_ROW:
        raise TriageError("Choose what to link this message to.")
    if action == "job":
        ok = conn.execute(
            "SELECT 1 FROM jobs WHERE id=? AND review_state='kept'", (target_id,)
        ).fetchone()
        if ok is None:
            raise TriageError("That job is not on your kept list.")
        return
    ok = conn.execute(
        f"SELECT 1 FROM network_contacts WHERE id=? AND {EFFECTIVE_RECRUITER_SQL}", (target_id,)
    ).fetchone()
    if ok is None:
        raise TriageError("That person is not on the recruiter desk.")


def _link_columns(action: str, target_id: int | None) -> tuple[str, int | None, int | None]:
    if action == "job":
        return "linked", target_id, None
    if action == "contact":
        return "linked", None, target_id
    return "dismissed", None, None


def decide(
    conn: sqlite3.Connection,
    triage_id: int,
    *,
    version: int,
    action: str,
    target_id: int | None = None,
    change: bool = False,
) -> None:
    """Link a row to a kept job or a recruiter, or dismiss it. One transaction covering the
    row, the event this feature previously made for it (removed) and the new one (added);
    any failure rolls all of it back."""
    if action not in ACTIONS:
        raise TriageError("Unknown action.")
    if not 0 <= version < MAX_ROW:
        raise TriageError("Bad version.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM mail_triage WHERE id=?", (triage_id,)).fetchone()
        if row is None:
            raise TriageNotFound(str(triage_id))
        if int(row["version"]) != version or (row["status"] != "new" and not change):
            raise StaleDecision("This message changed since the page was loaded.")
        _check_target(conn, action, target_id)
        ref = event_ref(int(row["uidvalidity"]), int(row["uid"]))
        if row["job_id"] is not None:
            conn.execute(
                "DELETE FROM application_events WHERE job_id=? AND source=? AND ref=?",
                (row["job_id"], EVENT_SOURCE, ref),
            )
        status, job_id, contact_id = _link_columns(action, target_id)
        cur = conn.execute(
            "UPDATE mail_triage SET status=?, job_id=?, network_contact_id=?, decided_at=?,"
            " version=version+1 WHERE id=? AND version=?",
            (status, job_id, contact_id, _now(), triage_id, version),
        )
        if cur.rowcount != 1:
            raise StaleDecision("This message changed since the page was loaded.")
        if job_id is not None:
            who = row["sender_name"] or row["sender_address"]
            events.insert_event(
                conn,
                job_id,
                "recruiter_reply",
                row["received_at"],
                note=f"{who}: {row['subject']}"[:_NOTE_CHARS],
                ref=ref,
                source=EVENT_SOURCE,
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

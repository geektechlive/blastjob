"""Recruiter submissions: a recruiter put the owner forward for a role (#100, ADR 0063).

The record exists so a direct application to the same role can be warned about BEFORE it
happens, and so a submission logged after a direct application says so. Draft-only and local:
nothing is sent, and `jobs.source` and the application stage never change from here.

"The same role" for this purpose ignores what the owner applied to: the job itself, every job
in its cluster, every job `same_role` suggests for any of those (agency or aggregator pair, or
the same employer's repost), and the clusters of those. See docs/how-it-works/same-role.md.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from blastjob.core import same_role as sr
from blastjob.core.recruiter_status import is_effective_recruiter
from blastjob.models.config import DiscoveryConfig

MAX_NAME_CHARS = 80
MAX_FIRM_CHARS = 80
MAX_NOTE_CHARS = 300
APPLY_STATE = "applied"
ACK_FIELD = "submission_ack"
LOG_ANYWAY_FIELD = "log_anyway"

_COLS = "id, job_id, network_contact_id, recruiter_name, firm, submitted_on, note, created_at"


class SubmissionError(ValueError):
    """A submission the owner's input cannot make; the message says why."""


class AckRequired(Exception):
    """A first direct application to a role a recruiter submitted him for, not acknowledged."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class PriorApplication:
    """A same-role job the owner applied through (any applied state, closed included)."""

    job_id: int
    applied_at: str
    application_state: str
    active: bool


# --- the role relation ---------------------------------------------------------------


def _cluster_members(index: sr.SameRoleIndex) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for jid, row in index.rows.items():
        if row["cluster_id"] is not None:
            out.setdefault(int(row["cluster_id"]), []).append(jid)
    return out


def _expand(index: sr.SameRoleIndex, members: dict[int, list[int]], ids: Iterable[int]) -> set[int]:
    out: set[int] = set()
    for jid in ids:
        out.add(jid)
        row = index.rows.get(jid)
        if row is not None and row["cluster_id"] is not None:
            out.update(members.get(int(row["cluster_id"]), []))
    return out


def _closure(index: sr.SameRoleIndex, members: dict[int, list[int]], job_id: int) -> set[int]:
    """The job's cluster, the peers of every member, and each peer's cluster."""
    own = _expand(index, members, [job_id])
    peers = {p for m in own for p in index.peer_ids(m)}
    return own | _expand(index, members, peers)


def role_job_ids(
    conn: sqlite3.Connection, job_id: int, radius_miles: float, geo: DiscoveryConfig
) -> set[int]:
    """Every job id that may be the same role as `job_id` (itself included)."""
    index = sr.SameRoleIndex(conn, radius_miles, geo)
    return {job_id} | _closure(index, _cluster_members(index), job_id)


def _in(ids: Iterable[int]) -> tuple[str, list[int]]:
    listed = sorted(ids)
    return ",".join("?" for _ in listed), listed


def submissions_for_role(
    conn: sqlite3.Connection, job_id: int, radius_miles: float, geo: DiscoveryConfig
) -> list[sqlite3.Row]:
    """Submissions on any job that is the same role, newest first."""
    marks, ids = _in(role_job_ids(conn, job_id, radius_miles, geo))
    return conn.execute(
        f"SELECT {_COLS} FROM recruiter_submissions WHERE job_id IN ({marks}) "
        "ORDER BY submitted_on DESC, id DESC",
        ids,
    ).fetchall()


def prior_applications(
    conn: sqlite3.Connection, job_id: int, radius_miles: float, geo: DiscoveryConfig
) -> list[PriorApplication]:
    """Same-role jobs the owner applied through: `applied_at` set or a state in
    `APPLIED_STATES`, closed ones included."""
    marks, ids = _in(role_job_ids(conn, job_id, radius_miles, geo))
    rows = conn.execute(
        "SELECT id, review_state, COALESCE(application_state,'') AS application_state, "
        f"COALESCE(applied_at,'') AS applied_at FROM jobs WHERE id IN ({marks}) ORDER BY id",
        ids,
    ).fetchall()
    return [
        PriorApplication(
            job_id=int(r["id"]),
            applied_at=r["applied_at"],
            application_state=r["application_state"],
            active=r["review_state"] == "kept"
            and r["application_state"] in ("applied", "screen", "interview", "offer"),
        )
        for r in rows
        if r["applied_at"].strip() or r["application_state"] in sr.APPLIED_STATES
    ]


def prior_application_warning(prior: Iterable[PriorApplication]) -> str:
    """The sentence(s) shown before logging a submission after a direct application."""
    lines: list[str] = []
    for p in prior:
        date = p.applied_at[:10]
        if p.active:
            when = f" on {date}" if date else ""
            lines.append(
                f"You applied directly to this role{when}; a recruiter submission now can "
                "sink both."
            )
        else:
            when = f" on {date}" if date else ""
            lines.append(
                f"You applied to this role{when} (now {p.application_state or 'closed'}). "
                "Employers often reject a second submission."
            )
    return " ".join(dict.fromkeys(lines))


# --- the first-apply warning and its server-side enforcement ---------------------------


def _submitted_sentence(rows: Iterable[sqlite3.Row]) -> str:
    who = "; ".join(
        f"{r['recruiter_name']}" + (f" ({r['firm']})" if r["firm"] else "") + f" submitted you to "
        f"this role on {r['submitted_on']}"
        for r in rows
    )
    return f"{who}. Applying directly as well can sink both. Apply anyway?"


def ack_token(rows: Iterable[sqlite3.Row]) -> str:
    """A short fingerprint of the submissions a warning showed. The page sends it back as
    `submission_ack`; a submission logged (or removed) since changes it, so the server refuses
    an acknowledgement of a warning that is no longer the whole truth."""
    ids = ",".join(str(i) for i in sorted(int(r["id"]) for r in rows))
    return hashlib.sha256(ids.encode()).hexdigest()[:16]


def first_apply_warning(
    conn: sqlite3.Connection, job_id: int, radius_miles: float, geo: DiscoveryConfig
) -> tuple[str, str]:
    """(hx-confirm text, acknowledgement token) for the FIRST direct application, or ("", "")
    when `applied_at` is set (a move on an applied job is not a new application) or no
    recruiter submitted the role."""
    row = conn.execute(
        "SELECT COALESCE(applied_at,'') AS a FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    if row is None or row["a"].strip():
        return "", ""
    found = submissions_for_role(conn, job_id, radius_miles, geo)
    return (_submitted_sentence(found), ack_token(found)) if found else ("", "")


def first_apply_confirm(
    conn: sqlite3.Connection, job_id: int, radius_miles: float, geo: DiscoveryConfig
) -> str:
    return first_apply_warning(conn, job_id, radius_miles, geo)[0]


def first_apply_confirms(
    conn: sqlite3.Connection, job_ids: Iterable[int], radius_miles: float, geo: DiscoveryConfig
) -> dict[int, tuple[str, str]]:
    """`first_apply_warning` for many unapplied jobs from one index and one query; jobs with
    nothing to warn about are left out. Free when no submission has ever been logged."""
    ids = list(job_ids)
    if not ids or conn.execute("SELECT 1 FROM recruiter_submissions LIMIT 1").fetchone() is None:
        return {}
    index = sr.SameRoleIndex(conn, radius_miles, geo)
    members = _cluster_members(index)
    by_job: dict[int, list[sqlite3.Row]] = {}
    for r in conn.execute(
        f"SELECT {_COLS} FROM recruiter_submissions ORDER BY submitted_on DESC, id DESC"
    ):
        by_job.setdefault(int(r["job_id"]), []).append(r)
    out: dict[int, tuple[str, str]] = {}
    for jid in ids:
        row = index.rows.get(jid)
        # The same rule as `ack_guard`: a job with applied_at is not a first application.
        if row is None or (row["applied_at"] or "").strip():
            continue
        related = _closure(index, members, jid) | {jid}
        found = sorted(
            (r for j in related for r in by_job.get(j, [])),
            key=lambda r: (r["submitted_on"], r["id"]),
            reverse=True,
        )
        if found:
            out[jid] = (_submitted_sentence(found), ack_token(found))
    return out


Guard = Callable[[sqlite3.Connection, str, sqlite3.Row], None]


def ack_guard(job_id: int, radius_miles: float, geo: DiscoveryConfig, ack: str) -> Guard:
    """A check `service.set_application_state` runs inside its write transaction, after it
    has read the prior row: raises `AckRequired` for a first move into applied on a role a
    recruiter submitted, unless `ack` is the token of exactly the submissions that exist now
    (recomputed here, inside the transaction)."""

    def check(conn: sqlite3.Connection, new_state: str, prior: sqlite3.Row) -> None:
        if new_state != APPLY_STATE or (prior["applied_at"] or "").strip():
            return
        if conn.execute("SELECT 1 FROM recruiter_submissions LIMIT 1").fetchone() is None:
            return
        found = submissions_for_role(conn, job_id, radius_miles, geo)
        if found and ack.strip() != ack_token(found):
            raise AckRequired(_submitted_sentence(found))

    return check


# --- writes ------------------------------------------------------------------------------


def _clean(value: str, limit: int, label: str) -> str:
    text = (value or "").strip()
    if len(text) > limit:
        raise SubmissionError(f"{label} is at most {limit} characters")
    return text


def _resolve_recipient(
    conn: sqlite3.Connection, network_contact_id: int | None, recruiter_name: str, firm: str
) -> tuple[str, str]:
    name = _clean(recruiter_name, MAX_NAME_CHARS, "name")
    firm_text = _clean(firm, MAX_FIRM_CHARS, "firm")
    if network_contact_id is None:
        if not name:
            raise SubmissionError("Pick a recruiter or type a name.")
        return name, firm_text
    if name or firm_text:
        raise SubmissionError("Pick a recruiter or type a name, not both.")
    if not is_effective_recruiter(conn, network_contact_id):
        raise SubmissionError("That contact is not a recruiter.")
    row = conn.execute(
        "SELECT full_name, COALESCE(company,'') AS company FROM network_contacts WHERE id=?",
        (network_contact_id,),
    ).fetchone()
    return (row["full_name"] or "")[:MAX_NAME_CHARS], row["company"][:MAX_FIRM_CHARS]


def _valid_date(text: str) -> str:
    try:
        day = dt.date.fromisoformat((text or "").strip())
    except ValueError as e:
        raise SubmissionError("The date must look like 2026-10-26.") from e
    if day > dt.date.today():
        raise SubmissionError("The date cannot be in the future.")
    return day.isoformat()


def check_input(
    conn: sqlite3.Connection,
    network_contact_id: int | None,
    recruiter_name: str,
    firm: str,
    submitted_on: str,
    note: str,
) -> tuple[str, str, str, str]:
    """Validate a form; returns (name, firm, day, note) as they would be stored."""
    day = _valid_date(submitted_on)
    text = _clean(note, MAX_NOTE_CHARS, "note")
    name, firm_text = _resolve_recipient(conn, network_contact_id, recruiter_name, firm)
    return name, firm_text, day, text


def add_submission(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    submitted_on: str,
    recruiter_name: str = "",
    firm: str = "",
    network_contact_id: int | None = None,
    note: str = "",
) -> int:
    """Record one submission and commit. With `network_contact_id` the name and firm come
    only from that effective recruiter (typed ones are refused); without it a typed name is
    required. Raises SubmissionError with the reason."""
    name, firm_text, day, text = check_input(
        conn, network_contact_id, recruiter_name, firm, submitted_on, note
    )
    cur = conn.execute(
        "INSERT INTO recruiter_submissions (job_id, network_contact_id, recruiter_name, firm, "
        "submitted_on, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, network_contact_id, name, firm_text, day, text, dt.datetime.now().isoformat()),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def get_submission(conn: sqlite3.Connection, submission_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_COLS} FROM recruiter_submissions WHERE id=?", (submission_id,)
    ).fetchone()


def delete_submission(conn: sqlite3.Connection, submission_id: int) -> bool:
    """Undo a record. It is the owner's own note and nothing else references it."""
    cur = conn.execute("DELETE FROM recruiter_submissions WHERE id=?", (submission_id,))
    conn.commit()
    return cur.rowcount == 1

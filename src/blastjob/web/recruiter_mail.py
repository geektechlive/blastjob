"""View model for the Recruiter mail page (#83): sender-check badge, Known / New / Automated
marks, and the link choices. Everything here is derived from stored rows; nothing contacts
the mailbox. The badge checks the SENDING DOMAIN, not the person, and is advisory: it never
hides a message. See docs/how-it-works/discovery.md."""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

from blastjob.core import mail_triage as mt
from blastjob.core import network_views as nv
from blastjob.discovery.replies import _AUTOMATED_SENDER

MAX_JOB_CHOICES = 200
MAX_CONTACT_CHOICES = 40

BADGE_PASSED = ("DMARC passed", "comp")
BADGE_FAILED = ("Sender checks failed", "dream")
BADGE_NONE = ("No DMARC result", "src")


def sender_badge(spf: str, dkim: str, dmarc: str) -> tuple[str, str]:
    """(wording, css class). A passed DMARC check wins; otherwise any failed check warns."""
    if dmarc == "pass":
        return BADGE_PASSED
    if "fail" in (spf, dkim, dmarc):
        return BADGE_FAILED
    return BADGE_NONE


def _known_emails(conn: sqlite3.Connection) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for r in conn.execute("SELECT id, email FROM network_contacts WHERE email IS NOT NULL"):
        addr = str(r["email"]).strip().lower()
        if addr:
            out.setdefault(addr, []).append(int(r["id"]))
    return out


def _job_choices(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, company, title, alignment_score FROM jobs WHERE review_state='kept'"
        " ORDER BY (alignment_score IS NULL), alignment_score DESC, id DESC LIMIT ?",
        (MAX_JOB_CHOICES,),
    ).fetchall()
    return [dict(r) for r in rows]


def _contact_choices(
    desk: list[dict[str, Any]], matches: list[int], sender_name: str
) -> list[dict[str, Any]]:
    """Recruiter-desk contacts, best match first: an email match, then the same name, then
    the desk's own order (warmest first)."""
    name = sender_name.strip().casefold()

    def rank(c: dict[str, Any]) -> int:
        if c["id"] in matches:
            return 0
        return 1 if name and str(c["name"]).strip().casefold() == name else 2

    ordered = sorted(desk, key=rank)  # stable: ties keep the desk's order
    return [{"id": c["id"], "name": c["name"], "company": c["company"]} for c in ordered][
        :MAX_CONTACT_CHOICES
    ]


def _decorate(
    row: sqlite3.Row,
    known: dict[str, list[int]],
    desk: list[dict[str, Any]],
    targets: dict[str, dict[int, str]],
) -> dict[str, Any]:
    address = str(row["sender_address"])
    matches = known.get(address, []) if address else []
    wording, css = sender_badge(row["auth_spf"], row["auth_dkim"], row["auth_dmarc"])
    return {
        **dict(row),
        "badge": wording,
        "badge_class": css,
        "is_known": bool(matches),
        "is_automated": bool(address and _AUTOMATED_SENDER.search(address)),
        "contact_choices": _contact_choices(desk, matches, str(row["sender_name"])),
        "linked_job": targets["jobs"].get(row["job_id"]) if row["job_id"] else "",
        "linked_contact": (
            targets["contacts"].get(row["network_contact_id"]) if row["network_contact_id"] else ""
        ),
    }


def _target_names(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, dict[int, str]]:
    jobs = {int(r["job_id"]) for r in rows if r["job_id"]}
    contacts = {int(r["network_contact_id"]) for r in rows if r["network_contact_id"]}
    out: dict[str, dict[int, str]] = {"jobs": {}, "contacts": {}}
    for jid in jobs:
        j = conn.execute("SELECT company, title FROM jobs WHERE id=?", (jid,)).fetchone()
        if j:
            out["jobs"][jid] = f"{j['company']} - {j['title']}"
    for cid in contacts:
        c = conn.execute("SELECT full_name FROM network_contacts WHERE id=?", (cid,)).fetchone()
        if c:
            out["contacts"][cid] = str(c["full_name"])
    return out


def page_context(conn: sqlite3.Connection, label: str) -> dict[str, Any]:
    """Template inputs for /recruiter-mail."""
    new_rows, decided_rows = mt.list_rows(conn)
    known = _known_emails(conn)
    desk = nv.recruiter_desk(conn)
    targets = _target_names(conn, [*new_rows, *decided_rows])
    return {
        "label": label,
        "new_rows": [_decorate(r, known, desk, targets) for r in new_rows],
        "decided_rows": [_decorate(r, known, desk, targets) for r in decided_rows],
        "job_choices": _job_choices(conn),
        "today": dt.date.today().isoformat(),
    }

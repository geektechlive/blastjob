"""Moves between the pipeline stages, each a compare-and-set on stored state.

Every move re-checks the job's current stage in its UPDATE, so a stale page
can never push a job somewhere it no longer belongs (for example: removing a
job from the shortlist after it was marked applied in another tab).
"""

from __future__ import annotations

import logging
import sqlite3

from blastjob.web import stages as st
from blastjob.web.service import set_application_state

log = logging.getLogger(__name__)

# Row actions a list page may post, by name.
ACTIONS: tuple[str, ...] = ("apply", "advance", "withdraw", "reopen")


def _state(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, review_state, source_state, alignment_score, "
        "COALESCE(application_state,'') AS application_state, applied_at "
        "FROM jobs WHERE id=?",
        (job_id,),
    ).fetchone()


def mark_applied(conn: sqlite3.Connection, job_id: int) -> bool:
    """Shortlist -> Applied. applied_at is stamped today if it was empty.

    Also snapshots WHICH documents went out (`core/packet_sends`). This is the
    only moment the answer is knowable: packets get rebuilt, so hashing the
    files later would attribute a future screen to a document that was never
    sent.
    """
    moved = set_application_state(conn, job_id, "applied", list(st.SHORTLIST_STATES))
    if not moved:
        return False
    # One transaction for both writes. `set_application_state` has already
    # committed the move, so a failure here is rolled back to exactly that
    # point: the job stays applied and the note explains the missing snapshot.
    # What must never happen is the reverse -- a send recorded against a move
    # that did not take.
    note = record_send_for_job(conn, job_id)
    if note:
        log.info("job %s applied without an as-sent snapshot: %s", job_id, note)
    return True


def record_send_for_job(conn: sqlite3.Connection, job_id: int) -> str:
    """Snapshot which documents went out. Returns "" when a send was recorded,
    otherwise ONE SENTENCE saying why nothing was.

    The silence was the defect. A quarantined packet has no resume in the
    folder, `role_documents` found none, and marking the job applied recorded
    nothing with no sign of it on screen. Still best-effort: bookkeeping never
    blocks the state change.
    """
    from pathlib import Path

    from blastjob.core import packet_sends as ps
    from blastjob.web import packets as pk

    try:
        packet = pk.packet_for_job(conn, job_id)
        if not packet or not packet.get("exists"):
            return "No as-sent snapshot: this job has no tailored packet on disk."
        row = conn.execute("SELECT title FROM jobs WHERE id=?", (job_id,)).fetchone()
        docs = pk.role_documents(packet["files"], row["title"] if row else "")
        if "resume" not in docs:
            if packet.get("quarantined"):
                return (
                    "No as-sent snapshot: this packet's documents are in quarantine. "
                    "Release it if you sent it."
                )
            return "No as-sent snapshot: no resume was found in the packet folder."
        packet_dir = Path(packet["dir"]).expanduser()
        cover = docs.get("cover")
        report = _packet_report(packet_dir)
        ps.record_send(
            conn,
            job_id,
            packet["dir"],
            packet_dir / docs["resume"],
            cover_letter_path=(packet_dir / cover) if cover else None,
            groundedness=report.get("groundedness"),
            jd_phrasing=report.get("jd_phrasing"),
            cover_letter_groundedness=report.get("cover_letter_groundedness"),
            # A packet that shipped with a known fatal must say so forever.
            # Until now this column stored "[]" on every row.
            fatal_findings=ps.fatal_findings_from_report(report),
        )
        return ""
    except Exception:  # noqa: BLE001 — never block the state change
        log.exception("recording the as-sent snapshot failed for job %s", job_id)
        return "No as-sent snapshot: recording it failed (see the log)."


def _packet_report(packet_dir) -> dict:
    """The scores this packet carried, from the report the build wrote beside
    it. Empty for a packet built before packet reports existed.

    Uses the shared `web.sidecars.find_sidecars` (consolidated 2026-09-17) with
    no role slug, so this keeps its original behaviour: try each report in the
    folder in filename order, first one that parses wins. Which role that
    report belongs to is not this call's concern — `record_send_for_job`
    already resolved the role's own resume/cover-letter files separately.
    """
    import json

    from blastjob.web.sidecars import find_sidecars

    for pick in find_sidecars(packet_dir, ".packet_report.json", None):
        try:
            return json.loads(pick.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def _record_outcome(conn: sqlite3.Connection, job_id: int, outcome: str) -> None:
    """Best effort, exactly like `_record_send`: appends one outcome event to
    the job's most recent send, if it has one. Bookkeeping must never stop a
    state change, and a job with no recorded send simply records nothing
    (`packet_sends.record_outcome_for_job` already returns None for that
    case; the try/except here is for anything else -- a locked DB, a bad
    row -- that must not block the move the user asked for)."""
    from blastjob.core import packet_sends as ps

    try:
        ps.record_outcome_for_job(conn, job_id, outcome)
    except Exception:  # noqa: BLE001 — never block the state change
        return


def advance(conn: sqlite3.Connection, job_id: int) -> bool:
    """applied -> screen -> interview -> offer, one step."""
    row = _state(conn, job_id)
    if row is None:
        return False
    target = st.next_stage(row["application_state"])
    if not target:
        return False
    moved = set_application_state(conn, job_id, target, [row["application_state"]])
    if moved:
        _record_outcome(conn, job_id, target)
    return moved


def withdraw(conn: sqlite3.Connection, job_id: int) -> bool:
    """Applied -> Closed, as withdrawn."""
    moved = set_application_state(conn, job_id, st.WITHDRAWN, list(st.IN_PROCESS_STATES))
    if moved:
        _record_outcome(conn, job_id, st.WITHDRAWN)
    return moved


def reopen(conn: sqlite3.Connection, job_id: int) -> bool:
    """Closed -> Applied if the user had applied, else back to the Shortlist. Leaving
    posting_closed/filled pins the posting verdict (see set_application_state),
    so the closure lifecycle does not close it again.

    Records a fresh 'applied' outcome on the job's existing send when it
    moves back to Applied -- this is the user saying the same attempt is still
    alive, not starting a new one (see the reopen note on
    `packet_sends.record_send`). Moving back to the Shortlist instead (no
    prior applied_at) records nothing: that job never had a send to begin
    with.
    """
    row = _state(conn, job_id)
    if row is None or row["application_state"] not in st.CLOSED_STATES:
        return False
    target = "applied" if (row["applied_at"] or "").strip() else ""
    moved = set_application_state(conn, job_id, target, [row["application_state"]])
    if moved and target:
        _record_outcome(conn, job_id, target)
    return moved


def unkeep(conn: sqlite3.Connection, job_id: int, threshold: int) -> bool:
    """Remove a job from the Shortlist, returning it where the gate would place
    it by its stored score (see stages.unkeep_target). Refused for a job in a
    hiring process or closed: that is an application, not an accidental Keep.

    Touches ONLY review_state, so application notes and dates come back if it
    is kept again. build_state is reset by the route."""
    row = _state(conn, job_id)
    if row is None or st.stage_of(row) != st.SHORTLIST:
        return False
    target = st.unkeep_target(
        source_state=row["source_state"],
        alignment_score=row["alignment_score"],
        threshold=threshold,
    )
    cur = conn.execute(
        f"UPDATE jobs SET review_state=? WHERE id=? AND {st.SHORTLIST_WHERE}",
        (target, job_id),
    )
    conn.commit()
    return cur.rowcount == 1


def run_action(conn: sqlite3.Connection, job_id: int, action: str) -> bool:
    """Dispatch a list-row action by name; False for an unknown one."""
    moves = {"apply": mark_applied, "advance": advance, "withdraw": withdraw, "reopen": reopen}
    move = moves.get(action)
    return move(conn, job_id) if move else False

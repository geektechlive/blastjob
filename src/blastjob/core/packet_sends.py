"""As-sent attribution: which exact documents went out for an application.

WHY THIS EXISTS (see docs/truth-guarantees.md): a
packet gets rebuilt after it was already sent -- three were rebuilt the same
day this module was written. If outcomes were attached by re-hashing whatever
happens to be on disk when the screen or rejection arrives, a later rebuild
would silently reattribute that outcome to a document the user never sent. So a
send is a snapshot taken AT THE MOMENT they mark a job applied: the resume,
cover letter and job description are hashed right then, written once, and
never touched again. `packet_outcomes` is a separate, append-only event log
so the history of what happened to that specific send is never overwritten
either.

HARD CONSTRAINT -- READ BEFORE ADDING ANYTHING HERE: this module does
descriptive bookkeeping only. It NEVER ranks packet styles, NEVER computes a
win rate or success rate that compares one send against another, and NEVER
grows a "once we have N sends, start ranking" threshold. The user asked for the
app to "learn from" what worked, and the honest first step of that is
recording what was actually sent well enough that a human can read the
history -- not manufacturing a comparison. Seniority, referral status,
timing and company dominate any effect a resume style could have; at the
sample sizes this app will ever see, a style comparison is superstition
wearing a p-value. `outcome_report` returns per-send facts (what was sent,
what happened, how far it got) and nothing that aggregates across sends. If
a future contributor is tempted to add ranking here: don't. Revisit plan V9
first.

DECIDED 2026-09-17 (dead-code review): `outcome_report`, `job_event_log` and
`stage_durations` had no caller outside tests. WIRE, not delete — this is the
feature the user asked for by name. The signatures below are already route-ready
on purpose (`outcome_report(conn)` takes no filter/limit/order argument, so a
future contributor cannot quietly turn "read the history" into "rank the
history" by adding one) — see the INTEGRATION PATCH for the exact route and
what its template needs. The one thing that patch must get right and this
docstring calls out explicitly: sort the report page by `sent_at` (which the
query above already does), never by `furthest_stage` or any other
packet-vs-packet ordering — sorting by outcome quality IS a comparison across
sends even with no ranking function anywhere in the code.

Also DECIDED, same review: `record_send`'s `fatal_findings` parameter was
always called with `()` — `fatal_findings_json` stored `"[]"` forever, so the
as-sent snapshot could never say a packet shipped with a known fatal. WIRE:
`fatal_findings_from_report`, below, converts the plain-dict findings a
sidecar holds on disk into the `Finding` objects `record_send` expects; see
the INTEGRATION PATCH for the one-line call-site change in
`web/stage_moves.py`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from blastjob.core.packet_report import Artifact, Finding, Severity
from blastjob.core.paths import canonical
from blastjob.models.tracking import STATUSES

# Outcomes packet_outcomes accepts -- every application_state value except
# 'drafted' (a send only exists once a packet was actually sent, i.e. the job
# reached at least 'applied').
OUTCOMES: tuple[str, ...] = tuple(s for s in STATUSES if s != "drafted")

# Outcomes that end an application attempt. Reaching one of these closes the
# "same attempt" window used by record_send's idempotency check (see there).
TERMINAL_OUTCOMES: frozenset[str] = frozenset(
    {"rejected", "ghosted", "withdrawn", "posting_closed", "filled"}
)

# Outcomes that represent forward progress, in order. 'applied' is not a
# packet_outcomes row -- it is implied by the send itself (sent_at) -- but it
# anchors furthest_stage() as the baseline every send has already reached.
_PROGRESS_ORDER: tuple[str, ...] = ("applied", "screen", "interview", "offer")
_PROGRESS_RANK: dict[str, int] = {name: i for i, name in enumerate(_PROGRESS_ORDER)}


def _now_iso() -> str:
    return datetime.now().isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str | Path) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"packet_sends: file to hash does not exist: {p}")
    return _sha256_bytes(p.read_bytes())


def _finding_to_dict(f: Finding) -> dict[str, Any]:
    d = asdict(f)
    # Severity/Artifact are StrEnum (str subclasses) so json.dumps would
    # serialize them fine as-is; stringify explicitly anyway so the stored
    # JSON never depends on enum identity surviving a future refactor.
    d["severity"] = str(f.severity)
    d["artifact"] = str(f.artifact)
    return d


def fatal_findings_from_report(report: dict[str, Any]) -> tuple[Finding, ...]:
    """Convert a packet-report sidecar's `findings` list back into `Finding`
    objects, FATAL severity only. This is `record_send`'s `fatal_findings`
    converting helper -- see the module docstring's 2026-09-17 decision.

    `record_send` is typed for real `Finding` objects, but the only place that
    reads a report back (`web.stage_moves._packet_report`) has just
    `json.loads`'d plain dicts: `severity`/`artifact` are bare strings there,
    not the StrEnum members `PacketReport.fatal()` compares by IDENTITY
    (`f.severity is Severity.FATAL`). Constructing `Finding(**item)` straight
    from such a dict would silently make that identity check always fail --
    this is the re-hydration step, and it keeps only what the column
    (`fatal_findings_json`) is named for.

    Never raises. `record_send_for_job`'s own `except Exception` exists to
    protect the state change, not to absorb bugs in this helper's input
    handling -- a malformed finding costs one dropped entry, never the whole
    as-sent snapshot. A report with no `findings` key (a `verification_failed`
    sidecar, or one written before this field existed) yields `()`. An entry
    with an unrecognised `severity`/`artifact` string, or missing required
    text, is skipped rather than raising; optional fields (`line`, `evidence`,
    `source`) default exactly as `Finding` itself defaults them.
    """
    out: list[Finding] = []
    for item in report.get("findings") or ():
        if not isinstance(item, dict):
            continue
        severity_raw = item.get("severity")
        if not isinstance(severity_raw, str):
            continue
        try:
            severity = Severity(severity_raw)
        except ValueError:
            continue
        if severity is not Severity.FATAL:
            continue
        code = item.get("code")
        message = item.get("message")
        if not isinstance(code, str) or not isinstance(message, str):
            continue
        try:
            artifact = Artifact(item.get("artifact", Artifact.RESUME.value))
        except ValueError:
            continue
        line = item.get("line")
        evidence = item.get("evidence")
        source = item.get("source")
        out.append(
            Finding(
                severity=severity,
                code=code,
                message=message,
                artifact=artifact,
                line=line if isinstance(line, int) else None,
                evidence=evidence if isinstance(evidence, str) else "",
                source=source if isinstance(source, str) else "",
            )
        )
    return tuple(out)


def record_send(
    conn: sqlite3.Connection,
    job_id: int,
    artifact_dir: str,
    resume_path: str | Path,
    *,
    cover_letter_path: str | Path | None = None,
    prompt_version: str = "",
    model: str = "",
    groundedness: int | None = None,
    jd_phrasing: int | None = None,
    cover_letter_groundedness: int | None = None,
    fatal_findings: tuple[Finding, ...] | list[Finding] = (),
    now: datetime | None = None,
    output_root: str | Path | None = None,
) -> int:
    """Snapshot one application-send. Immutable once written.

    With `output_root`, the three paths are stored in ROOT form
    (`paths.canonical`): the files are hashed where they are, but a filed
    packet's row must not name a bucket folder (issue #20).

    Hashes `resume_path` (and `cover_letter_path`, if given) and the job's
    stored `raw_jd` AT THIS MOMENT -- never re-read later, so a subsequent
    packet rebuild cannot change what a past send is attributed to.

    Idempotency rule (documented, not just implemented): a row with the same
    (job_id, resume_sha256, cover_letter_sha256) already exists and its most
    recent packet_outcomes entry is NOT a terminal outcome (or it has no
    outcomes yet) -> that existing send IS this attempt; its id is returned
    and no new row is written. This makes "mark applied" idempotent against
    double-clicks, retries, and re-marking a job that never got a fresh
    build. If the matching prior send's last outcome IS terminal
    (rejected/ghosted/withdrawn/posting_closed/filled), the attempt it
    represented is over -- a new send with the identical files is treated as
    a genuinely new application (e.g. reapplying, unchanged, after a prior
    rejection) and gets its own row. Distinguishing "still open" from
    "already resolved" is the only signal available for telling a retry from
    a real second attempt, since nothing else here knows about intent.

    Reopen interaction (decided when outcomes started actually being
    recorded, S5): `web.stage_moves.reopen` records a fresh 'applied' outcome
    on the existing send when a closed job (rejected/withdrawn/
    posting_closed/...) moves back to Applied. That un-terminalizes the
    send's last outcome on purpose -- a reopen means "this is still the same
    live attempt", not "start a new one". The practical effect: if the user then
    re-marks the job applied with byte-identical files, this rule coalesces
    into that same send rather than minting a duplicate, which is correct --
    there is no new packet, so there is no new send. A genuinely new
    application after reopening (a rebuilt resume, even by one byte) still
    gets its own row, because the hash simply won't match. Only an actual
    terminal resolution (closing the row again, or reapplying unchanged
    without ever reopening) starts a new attempt.
    """
    resume_sha = _sha256_file(resume_path)
    cover_sha = _sha256_file(cover_letter_path) if cover_letter_path else ""

    job_row = conn.execute("SELECT raw_jd FROM jobs WHERE id=?", (job_id,)).fetchone()
    raw_jd = (job_row["raw_jd"] if job_row is not None else "") or ""
    jd_sha = _sha256_bytes(raw_jd.encode("utf-8")) if raw_jd else ""

    existing = conn.execute(
        "SELECT id FROM packet_sends WHERE job_id=? AND resume_sha256=? "
        "AND cover_letter_sha256=? ORDER BY id DESC LIMIT 1",
        (job_id, resume_sha, cover_sha),
    ).fetchone()
    if existing is not None:
        last_outcome = conn.execute(
            "SELECT outcome FROM packet_outcomes WHERE send_id=? ORDER BY id DESC LIMIT 1",
            (existing["id"],),
        ).fetchone()
        if last_outcome is None or last_outcome["outcome"] not in TERMINAL_OUTCOMES:
            return int(existing["id"])
        # else: the earlier attempt with these exact files already resolved
        # (terminal outcome) -- fall through and record a new attempt.

    if output_root is not None:
        artifact_dir = str(canonical(artifact_dir, output_root))
        resume_path = canonical(resume_path, output_root)
        if cover_letter_path:
            cover_letter_path = canonical(cover_letter_path, output_root)
    findings_json = json.dumps([_finding_to_dict(f) for f in fatal_findings])
    sent_at = (now or datetime.now()).isoformat()

    cur = conn.execute(
        "INSERT INTO packet_sends "
        "(job_id, artifact_dir, resume_path, resume_sha256, cover_letter_path, "
        "cover_letter_sha256, jd_sha256, prompt_version, model, groundedness, "
        "jd_phrasing, cover_letter_groundedness, fatal_findings_json, sent_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            job_id,
            str(artifact_dir),
            str(resume_path),
            resume_sha,
            str(cover_letter_path) if cover_letter_path else "",
            cover_sha,
            jd_sha,
            prompt_version,
            model,
            groundedness,
            jd_phrasing,
            cover_letter_groundedness,
            findings_json,
            sent_at,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def record_outcome(conn: sqlite3.Connection, send_id: int, outcome: str, note: str = "") -> int:
    """Append one outcome event for a send. Append-only -- never updates or
    deletes a prior row, so the full history of what happened to a specific
    packet is always intact."""
    if outcome not in OUTCOMES:
        raise ValueError(f"packet_sends: outcome must be one of {OUTCOMES}, got {outcome!r}")
    cur = conn.execute(
        "INSERT INTO packet_outcomes (send_id, outcome, observed_at, note) VALUES (?,?,?,?)",
        (send_id, outcome, _now_iso(), note),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def latest_send_id(conn: sqlite3.Connection, job_id: int) -> int | None:
    """The most recently recorded send for a job, or None if it never had
    one. A job can have more than one send (a rejection followed by a fresh
    application later); every subsequent event belongs to the newest one."""
    row = conn.execute(
        "SELECT id FROM packet_sends WHERE job_id=? ORDER BY sent_at DESC, id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def record_outcome_for_job(
    conn: sqlite3.Connection, job_id: int, outcome: str, note: str = ""
) -> int | None:
    """Convenience wrapper for every stage-move call site: record `outcome`
    against the job's most recent send. Returns None -- and writes nothing --
    when the job has no recorded send (it moved through a stage before this
    module existed, or the packet-existence check in `web.stage_moves`
    declined to snapshot one). Callers that must never let bookkeeping block a
    state change (stage_moves, the closure lifecycle, the manual status
    route) wrap this in their own best-effort try/except, exactly like
    `record_send`'s callers do -- this function itself still raises on a
    genuinely invalid outcome (see `record_outcome`), so a real bug is not
    swallowed silently at this layer.
    """
    send_id = latest_send_id(conn, job_id)
    if send_id is None:
        return None
    return record_outcome(conn, send_id, outcome, note=note)


def furthest_stage(outcome_names: list[str]) -> str:
    """The furthest point of forward progress an application reached.

    'applied' is always the floor (a send implies the application happened).
    A terminal outcome (rejected/ghosted/...) does not, by itself, count as
    progress beyond whatever progress outcome preceded it -- dying at
    'rejected' after reaching 'interview' is reported as having reached
    'interview', which is the actionable fact; dying at 'rejected' with
    nothing in between is reported as 'applied' ("no response, then a
    rejection" vs "got to a screen" are different stories at n=4).
    """
    best = 0
    for name in outcome_names:
        rank = _PROGRESS_RANK.get(name)
        if rank is not None and rank > best:
            best = rank
    return _PROGRESS_ORDER[best]


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def stage_durations(
    events: list[dict[str, Any]], *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """How long one application sat at each stage it passed through, in whole
    days. `events` is an ordered list of {"outcome"/"stage", "at": iso ts,
    ...} -- the shape `outcome_report` and `job_event_log` both produce. The
    last stage's clock keeps running (it has not ended yet), so its duration
    is measured to `now` (default: this call's time).

    This is the part with value at n=4: four applications that died with no
    response mean something different from four that reached a screen and
    sat there. It reports one application's own timeline only -- never a
    comparison to another's (see the module docstring's hard constraint).
    """
    if not events:
        return []
    clock = now or datetime.now()
    out: list[dict[str, Any]] = []
    for i, ev in enumerate(events):
        stage = ev.get("stage", ev.get("outcome"))
        start = _parse_ts(ev["at"])
        end = _parse_ts(events[i + 1]["at"]) if i + 1 < len(events) else clock
        days = max((end - start).total_seconds() / 86400.0, 0.0)
        out.append({"stage": stage, "entered_at": ev["at"], "days": round(days, 1)})
    return out


def _send_events(conn: sqlite3.Connection, send_id: int, sent_at: str) -> list[dict[str, Any]]:
    """One send's own timeline: the implicit 'applied' event at `sent_at`,
    followed by its recorded outcomes in order."""
    outcomes = conn.execute(
        "SELECT outcome, observed_at, note FROM packet_outcomes "
        "WHERE send_id=? ORDER BY observed_at, id",
        (send_id,),
    ).fetchall()
    events: list[dict[str, Any]] = [{"stage": "applied", "at": sent_at, "note": ""}]
    events.extend(
        {"stage": o["outcome"], "at": o["observed_at"], "note": o["note"]} for o in outcomes
    )
    return events


def job_event_log(conn: sqlite3.Connection, job_id: int) -> list[dict[str, Any]]:
    """The ordered event log for one job, for the cockpit to render as a
    timeline: every send it ever produced (as an 'applied' event at
    sent_at), interleaved with every outcome recorded against any of them,
    oldest first. A job normally has one send; it has more than one only
    after a prior attempt resolved and the user genuinely reapplied (see
    `record_send`'s idempotency rule). Descriptive only.
    """
    sends = conn.execute(
        "SELECT id, sent_at FROM packet_sends WHERE job_id=? ORDER BY sent_at, id",
        (job_id,),
    ).fetchall()

    events: list[dict[str, Any]] = []
    for send in sends:
        for ev in _send_events(conn, send["id"], send["sent_at"]):
            events.append({"send_id": send["id"], **ev})
    events.sort(key=lambda e: (e["at"], e["send_id"]))
    return events


def outcome_report(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Descriptive report of what the user actually reads: per application, the
    documents sent, the scores they carried, the full outcome history, the
    furthest stage reached, and how long each stage was sat in. One row per
    send. No aggregation, no comparison across sends -- see the module
    docstring for why that is a hard line, not an oversight.
    """
    sends = conn.execute(
        "SELECT ps.id, ps.job_id, ps.artifact_dir, ps.resume_path, ps.resume_sha256, "
        "ps.cover_letter_path, ps.cover_letter_sha256, ps.jd_sha256, ps.prompt_version, "
        "ps.model, ps.groundedness, ps.jd_phrasing, ps.cover_letter_groundedness, "
        "ps.fatal_findings_json, ps.sent_at, j.company, j.title "
        "FROM packet_sends ps LEFT JOIN jobs j ON j.id = ps.job_id "
        "ORDER BY ps.sent_at, ps.id"
    ).fetchall()

    report: list[dict[str, Any]] = []
    for send in sends:
        events = _send_events(conn, send["id"], send["sent_at"])
        outcome_list = [
            {"outcome": e["stage"], "observed_at": e["at"], "note": e["note"]}
            for e in events[1:]  # drop the synthetic leading 'applied' event
        ]
        report.append(
            {
                "send_id": send["id"],
                "job_id": send["job_id"],
                "company": send["company"],
                "title": send["title"],
                "artifact_dir": send["artifact_dir"],
                "resume_path": send["resume_path"],
                "resume_sha256": send["resume_sha256"],
                "cover_letter_path": send["cover_letter_path"],
                "cover_letter_sha256": send["cover_letter_sha256"],
                "jd_sha256": send["jd_sha256"],
                "prompt_version": send["prompt_version"],
                "model": send["model"],
                "groundedness": send["groundedness"],
                "jd_phrasing": send["jd_phrasing"],
                "cover_letter_groundedness": send["cover_letter_groundedness"],
                "fatal_findings": json.loads(send["fatal_findings_json"] or "[]"),
                "sent_at": send["sent_at"],
                "outcomes": outcome_list,
                "furthest_stage": furthest_stage([o["outcome"] for o in outcome_list]),
                "latest_outcome": outcome_list[-1]["outcome"] if outcome_list else None,
                "stage_durations": stage_durations(events),
            }
        )
    return report

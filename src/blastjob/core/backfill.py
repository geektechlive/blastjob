"""`blastjob-daily --backfill-artifacts[-report]`: index what already exists.

Two backfills in one pass, both idempotent:

- **Artifacts.** For every successful `build_runs` row, the files that run
  wrote for its own role (`artifacts.role_files` -- the same rule the build
  hook uses, never a glob of a shared folder), newest run first so a
  re-tailored file is credited to the run that last wrote it; plus each
  `packet_sends` resume/cover-letter path. A missing or unreadable file is
  listed in its own section and skipped; the rest still register.
- **Events** (`core/events`, source 'backfill'). An `applied` event at
  `applied_at` for every kept job that has one, and for every other
  non-silent application_state an event at `stage_changed_at` (falling back
  to `posting_closed_at`). A state with no date on record is listed, not
  guessed. A job that already has an event of that kind is left alone.

Report mode writes nothing at all.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from blastjob.core import artifacts, events

_SOURCE = "backfill"


@dataclass
class _Tally:
    apply: bool
    seen: set[Path] = field(default_factory=set)
    new: int = 0
    indexed: int = 0
    missing: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)


def _register(
    conn: sqlite3.Connection,
    t: _Tally,
    path: Path,
    job_id: int,
    run_pk: int | None,
    label: str,
) -> None:
    if path in t.seen:
        return
    t.seen.add(path)
    if not path.is_file():
        t.missing.append(f"{label}: {path}")
        return
    if artifacts.is_registered(conn, path):
        t.indexed += 1
        if not t.apply:
            return
    else:
        t.new += 1
    if not t.apply:
        return
    try:
        artifacts.register(
            conn, path, artifacts.kind_for(path.name), job_id=job_id, build_run_id=run_pk
        )
    except (OSError, ValueError) as exc:
        t.missing.append(f"{label}: {path} ({type(exc).__name__})")


def _build_outputs(conn: sqlite3.Connection, t: _Tally) -> int:
    rows = conn.execute(
        "SELECT b.id, b.job_id, b.artifact_dir, j.company, j.title FROM build_runs b "
        "JOIN jobs j ON j.id = b.job_id WHERE b.status='done' AND b.artifact_dir != '' "
        "ORDER BY b.id DESC"
    ).fetchall()
    for r in rows:
        label = f"build run #{r['id']} (job #{r['job_id']})"
        folder = Path(r["artifact_dir"]).expanduser()
        if not folder.is_dir():
            t.missing.append(f"{label}: folder {folder}")
            continue
        files = artifacts.role_files(
            folder, company=r["company"] or "", role=r["title"] or "", job_id=r["job_id"]
        )
        if not files:
            # Usually the job's title or company changed after the build, so the
            # naming rule no longer finds its files. Said out loud, never guessed.
            t.unmatched.append(f"{label}: {folder} (as {r['title']!r} @ {r['company']!r})")
        for path in files:
            _register(conn, t, path, r["job_id"], r["id"], label)
    return len(rows)


def _sent_documents(conn: sqlite3.Connection, t: _Tally) -> int:
    rows = conn.execute(
        "SELECT id, job_id, resume_path, cover_letter_path FROM packet_sends ORDER BY id DESC"
    ).fetchall()
    count = 0
    for r in rows:
        for raw in (r["resume_path"], r["cover_letter_path"]):
            if not raw:
                continue
            count += 1
            path = Path(raw).expanduser().resolve()
            run_pk = artifacts.run_for_dir(conn, r["job_id"], path.parent)
            _register(conn, t, path, r["job_id"], run_pk, f"sent #{r['id']} (job #{r['job_id']})")
    return count


def _planned_events(conn: sqlite3.Connection) -> tuple[list[tuple[int, str, str]], list[str]]:
    """(job_id, kind, at) to add, and the states left undated."""
    plan: list[tuple[int, str, str]] = []
    undated: list[str] = []
    applied = conn.execute(
        "SELECT id, applied_at FROM jobs WHERE review_state='kept' "
        "AND COALESCE(applied_at,'') <> '' ORDER BY id"
    ).fetchall()
    for r in applied:
        if events.has_event(conn, r["id"], "applied"):
            continue
        try:
            plan.append((r["id"], "applied", events.normalize_at(r["applied_at"])))
        except ValueError:
            undated.append(f"#{r['id']} applied (unreadable date {r['applied_at']!r})")
    states = conn.execute(
        "SELECT id, application_state, stage_changed_at, posting_closed_at FROM jobs "
        "WHERE COALESCE(application_state,'') NOT IN ('', 'applied', 'drafted') ORDER BY id"
    ).fetchall()
    for r in states:
        kind = events.state_event_kind(r["application_state"])
        if kind is None or events.has_event(conn, r["id"], kind):
            continue
        at = _first_date(r["stage_changed_at"], r["posting_closed_at"])
        if at is None:
            undated.append(f"#{r['id']} {kind}")
            continue
        plan.append((r["id"], kind, at))
    return plan, undated


def _first_date(*candidates: str | None) -> str | None:
    for value in candidates:
        try:
            return events.normalize_at(value or "")
        except ValueError:
            continue
    return None


def run(conn: sqlite3.Connection, *, apply: bool) -> str:
    """The backfill report; the writes too when `apply`."""
    t = _Tally(apply=apply)
    runs = _build_outputs(conn, t)
    sent = _sent_documents(conn, t)
    plan, undated = _planned_events(conn)
    if apply:
        for job_id, kind, at in plan:
            events.insert_event(conn, job_id, kind, at, source=_SOURCE)
        conn.commit()

    verb = "Registered" if apply else "Would register"
    added = "Added" if apply else "Would add"
    lines = [
        f"{verb} {t.new} new file(s) from {runs} build run(s) and {sent} as-sent "
        f"document(s); {t.indexed} already indexed.",
        f"{added} {len(plan)} reconstructed event(s).",
    ]
    if t.missing:
        lines += ["", f"Missing or unreadable, skipped ({len(t.missing)}):"]
        lines += [f"  - {m}" for m in t.missing]
    if t.unmatched:
        lines += ["", f"Build runs where no files matched the job's role ({len(t.unmatched)}):"]
        lines += [f"  - {u}" for u in t.unmatched]
    if undated:
        lines += ["", f"States with no date on record, not reconstructed ({len(undated)}):"]
        lines += [f"  - {u}" for u in undated]
    return "\n".join(lines)

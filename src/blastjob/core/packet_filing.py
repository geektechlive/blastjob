"""File packets by stage inside the output folder (issue #20).

The top level of `paths.output_dir` holds only packets still to send; the
rest sit in `Submitted/`, `Closed before applying/` and `Archive/`
(`paths.BUCKETS`). The unit of filing is an ENTRY, a top-level folder such as
`acme-2026-10-16` or a legacy `2026-04-21/<company>/<role>` tree, moved whole
with ONE `os.rename` and never renamed. Nothing in the database is rewritten:
stored paths stay in root form and every reader resolves them with
`paths.locate`, so a crash mid-pass leaves nothing to repair (the earlier
journal/heal design was dropped for this; see the v3 decisions).

Where an entry belongs is a pure function of the jobs linked to it:

- links: a `build_runs` folder, an `artifacts` row or a `packet_sends` path
  inside it. Only for an entry with NONE of those (the workup/prep folders of a
  live application have no build row, and archiving one buried a live
  application in review), the name rules: a `*.followup-{id}.md` file names its
  job, and `<slugify(company)>-YYYY-MM-DD[-N]` names every KEPT job of exactly
  that company. Applied to a recorded entry, the company rule would let a later
  shortlisted role pull a filed application back to the root, or an applied
  role pull an archived sibling's packet into Submitted.
- a job's bucket (`bucket_for_job`): send evidence first, then review state.
  A state that claims an application with no evidence is a CONTRADICTION: it
  stays at the root and is reported, never filed as Submitted on say-so.
- an entry's bucket is the most visible of its jobs' (root > Submitted >
  Closed before applying > Archive), so one role still to send is never buried
  by a sibling role's state. No linked job at all: Archive.

Guards: the filing lock (`core/filing_lock.py`, exclusive, non-blocking; a
busy lock skips the pass with a logged warning), and while a build or refine
holds a live lease the entries it can write are skipped (those linked to its
job, and any entry named for today), each named in a warning. Directories only,
no symlinked entry or entry holding a symlink, and an existing target is a
conflict (reported, nothing moved, never merged). A skipped entry is filed by
the post-build hook or the next daily pass. Report mode writes nothing, not
even the lock file.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from blastjob.core import filing_lock
from blastjob.core.paths import (
    ARCHIVE,
    BUCKETS,
    CLOSED_BEFORE_APPLYING,
    SUBMITTED,
    entry_name,
    slugify,
)

log = logging.getLogger(__name__)

ROOT = ""
# Folders the filer never moves: the follow-up drafts with no packet go here,
# and moving it would strand the next draft (`core/followup.FALLBACK_DIRNAME`).
SYSTEM_ENTRIES = frozenset({"unfiled-followups"})
# Most visible first: an entry goes where its most visible job puts it.
_VISIBILITY: tuple[str, ...] = (ROOT, SUBMITTED, CLOSED_BEFORE_APPLYING, ARCHIVE)
# States that say an application happened; without evidence they contradict.
_CLAIMS_APPLICATION = frozenset(
    {"applied", "screen", "interview", "offer", "rejected", "ghosted", "withdrawn", "filled"}
)
_ENTRY_NAME = re.compile(r"^(?P<company>.+)-\d{4}-\d{2}-\d{2}(?:-\d+)?$")
_FOLLOWUP_ID = re.compile(r"\.followup-(\d+)\.md$")


def label(bucket: str) -> str:
    return bucket or "(root)"


# --- the bucket of one job -------------------------------------------------------


@dataclass(frozen=True)
class JobBucket:
    bucket: str
    contradiction: bool = False
    rule: str = ""


def bucket_for_job(job: Mapping[str, Any], *, has_send: bool, has_applied_event: bool) -> JobBucket:
    """Where one job's packets belong. Send evidence (a `packet_sends` row or
    an `applied` event) beats review_state: an archived merge duplicate that was
    sent is still a submitted packet."""
    if has_send or has_applied_event:
        return JobBucket(SUBMITTED, rule="as-sent record" if has_send else "applied event")
    review = job.get("review_state") or ""
    if review == "archived":
        return JobBucket(ARCHIVE, rule="archived")
    if review != "kept":
        return JobBucket(ROOT, rule=f"undecided ({review or 'new'})")
    state = job.get("application_state") or ""
    if (job.get("applied_at") or "").strip():
        return JobBucket(SUBMITTED, rule=f"applied_at, {state or 'no state'}")
    if state in _CLAIMS_APPLICATION:
        return JobBucket(ROOT, contradiction=True, rule=f"{state} with no application evidence")
    # Only the state the closure lifecycle moved the job to. A `posting_check`
    # verdict alone (shadow mode, likely_closed) never files a packet away.
    if state == "posting_closed":
        return JobBucket(CLOSED_BEFORE_APPLYING, rule="posting closed, never applied")
    return JobBucket(ROOT, rule=f"shortlist ({state or 'not drafted'})")


# --- results ---------------------------------------------------------------------


@dataclass(frozen=True)
class Move:
    entry: str
    src: str
    dst: str
    why: str = ""


@dataclass(frozen=True)
class Problem:
    entry: str
    why: str


@dataclass(frozen=True)
class Contradiction:
    job_id: int
    entry: str
    why: str


@dataclass(frozen=True)
class FilingResult:
    applied: bool
    busy: bool = False
    note: str = ""
    moves: tuple[Move, ...] = ()
    conflicts: tuple[Problem, ...] = ()
    skipped: tuple[Problem, ...] = ()
    failed: tuple[Problem, ...] = ()
    contradictions: tuple[Contradiction, ...] = ()
    unsent_submitted: tuple[str, ...] = ()
    at_root: tuple[Move, ...] = ()


# --- what is on disk and what links to it ---------------------------------------


@dataclass(frozen=True)
class _Entry:
    name: str
    bucket: str
    path: Path


def _holds_symlink(folder: Path) -> bool:
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        for name in (*dirnames, *filenames):
            if os.path.islink(os.path.join(dirpath, name)):
                return True
    return False


def _entries(root: Path) -> tuple[dict[str, list[_Entry]], list[Problem]]:
    found: dict[str, list[_Entry]] = {}
    skipped: list[Problem] = []
    places = [(ROOT, root)] + [(b, root / b) for b in BUCKETS]
    for bucket, folder in places:
        if folder.is_symlink() or not folder.is_dir():
            continue
        for child in sorted(folder.iterdir()):
            name = child.name
            if name.startswith(".") or (bucket == ROOT and name in (*BUCKETS, *SYSTEM_ENTRIES)):
                continue
            if child.is_symlink():
                skipped.append(Problem(name, "is a symlink"))
                continue
            if not child.is_dir():
                continue
            found.setdefault(name, []).append(_Entry(name, bucket, child))
    return found, skipped


def _add(links: dict[str, dict[int, set[str]]], entry: str | None, job_id: Any, rule: str) -> None:
    if entry and job_id is not None:
        links.setdefault(entry, {}).setdefault(int(job_id), set()).add(rule)


def _db_links(conn: sqlite3.Connection, root: Path) -> dict[str, dict[int, set[str]]]:
    links: dict[str, dict[int, set[str]]] = {}
    for r in conn.execute("SELECT job_id, artifact_dir FROM build_runs WHERE artifact_dir != ''"):
        _add(links, entry_name(r["artifact_dir"], root), r["job_id"], "build")
    for r in conn.execute("SELECT job_id, path FROM artifacts WHERE job_id IS NOT NULL"):
        _add(links, entry_name(r["path"], root), r["job_id"], "artifact")
    for r in conn.execute(
        "SELECT job_id, artifact_dir, resume_path, cover_letter_path FROM packet_sends"
    ):
        for raw in (r["artifact_dir"], r["resume_path"], r["cover_letter_path"]):
            if raw:
                _add(links, entry_name(raw, root), r["job_id"], "sent")
    return links


def _company_slug(company: str) -> str:
    return slugify(company or "") or "unknown-company"


def _name_company(name: str) -> str | None:
    m = _ENTRY_NAME.match(name)
    return m.group("company") if m else None


def _files_in(folder: Path) -> Iterator[str]:
    for _dirpath, _dirs, files in os.walk(folder, followlinks=False):
        yield from files


def _name_links(
    conn: sqlite3.Connection, entries: Iterable[_Entry], links: dict[str, dict[int, set[str]]]
) -> None:
    """The name rules, ONLY for entries no build, artifact or send recorded
    (workups, prep packs, follow-ups written beside a live application)."""
    unlinked = [e for e in entries if e.name not in links]
    if not unlinked:
        return
    known = {int(r["id"]) for r in conn.execute("SELECT id FROM jobs")}
    kept: dict[str, list[int]] = {}
    for r in conn.execute("SELECT id, company FROM jobs WHERE review_state='kept'"):
        kept.setdefault(_company_slug(r["company"]), []).append(int(r["id"]))
    for e in unlinked:
        for jid in kept.get(_name_company(e.name) or "", []):
            _add(links, e.name, jid, "company name")
        for f in _files_in(e.path):
            fm = _FOLLOWUP_ID.search(f)
            if fm and int(fm.group(1)) in known:
                _add(links, e.name, int(fm.group(1)), "follow-up name")


def _job_buckets(conn: sqlite3.Connection, ids: set[int]) -> dict[int, tuple[JobBucket, str]]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    params = tuple(ids)
    sent = {
        int(r[0])
        for r in conn.execute(f"SELECT job_id FROM packet_sends WHERE job_id IN ({marks})", params)
    }
    applied = {
        int(r[0])
        for r in conn.execute(
            f"SELECT job_id FROM application_events WHERE kind='applied' AND job_id IN ({marks})",
            params,
        )
    }
    out: dict[int, tuple[JobBucket, str]] = {}
    for r in conn.execute(
        "SELECT id, company, title, review_state, application_state, applied_at, posting_check "
        f"FROM jobs WHERE id IN ({marks})",
        params,
    ):
        jid = int(r["id"])
        b = bucket_for_job(dict(r), has_send=jid in sent, has_applied_event=jid in applied)
        out[jid] = (b, f"#{jid} {r['company'] or '?'} / {r['title'] or '?'}")
    return out


def _sent_entries(conn: sqlite3.Connection, root: Path) -> set[str]:
    out: set[str] = set()
    for r in conn.execute("SELECT artifact_dir, resume_path, cover_letter_path FROM packet_sends"):
        for raw in r:
            if raw and (name := entry_name(raw, root)):
                out.add(name)
    return out


def _running_jobs(conn: sqlite3.Connection, now: datetime) -> set[int]:
    """Jobs with a build or refine holding a live lease."""
    rows = conn.execute(
        "SELECT DISTINCT job_id FROM build_runs WHERE status='running' AND lease_expires_at >= ?",
        (now.isoformat(),),
    )
    return {int(r[0]) for r in rows}


def running_build(conn: sqlite3.Connection, now: datetime) -> bool:
    return bool(_running_jobs(conn, now))


def _todays(name: str, now: datetime) -> bool:
    """An entry a build running now may be writing: `<co>-<today>[-N]`."""
    return re.search(rf"-{now.date().isoformat()}(?:-\d+)?$", name) is not None


# --- planning --------------------------------------------------------------------


@dataclass
class _Plan:
    moves: list[Move] = field(default_factory=list)
    at_root: list[Move] = field(default_factory=list)
    conflicts: list[Problem] = field(default_factory=list)
    skipped: list[Problem] = field(default_factory=list)
    contradictions: list[Contradiction] = field(default_factory=list)
    unsent: list[str] = field(default_factory=list)


def _target(
    name: str, linked: dict[int, set[str]], facts: dict[int, tuple[JobBucket, str]], plan: _Plan
) -> tuple[str, str]:
    """(bucket, why) for one entry; records its contradictions on `plan`."""
    if not linked:
        return ARCHIVE, "no linked job"
    best, why = ARCHIVE, []
    for jid in sorted(linked):
        if jid not in facts:
            continue
        b, desc = facts[jid]
        if b.contradiction:
            plan.contradictions.append(Contradiction(jid, name, f"{desc}: {b.rule}"))
        rules = ", ".join(sorted(linked[jid]))
        why.append(f"{desc} -> {label(b.bucket)} ({b.rule}; via {rules})")
        if _VISIBILITY.index(b.bucket) < _VISIBILITY.index(best):
            best = b.bucket
    return best, "; ".join(why) or "no linked job"


def _scope(
    conn: sqlite3.Connection,
    found: dict[str, list[_Entry]],
    links: dict[str, dict[int, set[str]]],
    job_id: int,
) -> dict[str, list[_Entry]]:
    """A job-scoped pass looks only at the job's recorded entries and the
    unrecorded ones named for its company: a cockpit click never walks the
    whole output folder."""
    row = conn.execute("SELECT company FROM jobs WHERE id=?", (job_id,)).fetchone()
    slug = _company_slug(row["company"]) if row else None
    return {
        name: locs
        for name, locs in found.items()
        if job_id in links.get(name, {})
        or (name not in links and slug is not None and _name_company(name) == slug)
    }


def _plan(conn: sqlite3.Connection, root: Path, job_id: int | None, now: datetime) -> _Plan:
    plan = _Plan()
    found, skipped = _entries(root)
    links = _db_links(conn, root)
    if job_id is not None:
        found = _scope(conn, found, links, job_id)
        skipped = [p for p in skipped if p.entry in found]
    plan.skipped.extend(skipped)
    _name_links(conn, (e for locs in found.values() for e in locs), links)
    facts = _job_buckets(conn, {j for linked in links.values() for j in linked})
    sent = _sent_entries(conn, root)
    busy = _running_jobs(conn, now)
    for name in sorted(found):
        linked = links.get(name, {})
        if job_id is not None and job_id not in linked:
            continue
        if busy and (busy & set(linked) or _todays(name, now)):
            plan.skipped.append(Problem(name, "a build or refine is running that may write it"))
            continue
        locations = found[name]
        if len(locations) > 1:
            where = " and ".join(label(e.bucket) for e in locations)
            plan.conflicts.append(Problem(name, f"exists in both {where}; resolve by hand"))
            continue
        entry = locations[0]
        if _holds_symlink(entry.path):
            plan.skipped.append(Problem(name, "contains a symlink"))
            continue
        bucket, why = _target(name, linked, facts, plan)
        if bucket == SUBMITTED and name not in sent:
            plan.unsent.append(name)
        if bucket == entry.bucket:
            if bucket == ROOT:
                plan.at_root.append(Move(name, ROOT, ROOT, why))
            continue
        dst = root / bucket / name if bucket else root / name
        if os.path.lexists(dst):
            plan.conflicts.append(Problem(name, f"{label(bucket)}/{name} already exists"))
            continue
        plan.moves.append(Move(name, entry.bucket, bucket, why))
    return plan


def _result(plan: _Plan, *, applied: bool, note: str = "") -> FilingResult:
    return FilingResult(
        applied=applied,
        note=note,
        moves=tuple(plan.moves),
        conflicts=tuple(plan.conflicts),
        skipped=tuple(plan.skipped),
        contradictions=tuple(plan.contradictions),
        unsent_submitted=tuple(plan.unsent),
        at_root=tuple(plan.at_root),
    )


def _move(root: Path, m: Move) -> Problem | None:
    """One rename, re-checked at the moment it happens."""
    src = root / m.src / m.entry if m.src else root / m.entry
    dst = root / m.dst / m.entry if m.dst else root / m.entry
    if src.is_symlink() or not src.is_dir():
        return Problem(m.entry, "vanished before the move")
    if os.path.lexists(dst):
        return Problem(m.entry, f"{label(m.dst)}/{m.entry} appeared before the move")
    try:
        dst.parent.mkdir(exist_ok=True)
        os.rename(src, dst)
    except OSError as exc:
        return Problem(m.entry, f"rename failed: {exc}")
    return None


def file_packets(
    conn: sqlite3.Connection,
    output_root: str | Path,
    *,
    apply: bool,
    job_id: int | None = None,
    now: datetime | None = None,
) -> FilingResult:
    """Plan (and with `apply`, perform) the filing of every entry, or only the
    entries linked to `job_id`. Report mode takes no lock and writes nothing."""
    root = Path(os.path.expanduser(str(output_root)))
    now = now or datetime.now()
    if not root.is_dir():
        return FilingResult(applied=apply, note="output folder does not exist")
    if not apply:
        return _result(_plan(conn, root, job_id, now), applied=False)
    with filing_lock.exclusive(root) as ok:
        if not ok:
            scope = f"job #{job_id}'s entries" if job_id is not None else "every entry"
            log.warning(
                "packet filing skipped (%s): a writer holds the filing lock; "
                "the post-build hook or the next daily pass files them",
                scope,
            )
            return FilingResult(applied=True, busy=True, note="a writer holds the filing lock")
        plan = _plan(conn, root, job_id, now)
        done: list[Move] = []
        for m in plan.moves:
            problem = _move(root, m)
            if problem is None:
                done.append(m)
            else:
                plan.conflicts.append(problem)
        res = _result(replace(plan, moves=done), applied=True)
    for m in res.moves:
        log.info("filed %s: %s -> %s", m.entry, label(m.src), label(m.dst))
    for p in (*res.skipped, *res.conflicts):
        log.warning("packet filing left %s where it is: %s", p.entry, p.why)
    return res


def file_after_change(
    conn: sqlite3.Connection, output_root: str | Path, job_id: int
) -> FilingResult | None:
    """The best-effort hook after a stage move, build or refine: files this
    job's entries. A failure is a logged warning, never a refused move or a
    failed build."""
    try:
        return file_packets(conn, output_root, apply=True, job_id=job_id)
    except Exception:  # noqa: BLE001 -- filing never fails the action it follows
        log.warning("filing packets for job %s failed", job_id, exc_info=True)
        return None


# --- the report ------------------------------------------------------------------


def _section(title: str, items: list[str]) -> list[str]:
    return ["", f"{title} ({len(items)}):", *(f"  - {i}" for i in items)] if items else []


def format_report(res: FilingResult) -> str:
    verb = "Moved" if res.applied else "Would move"
    lines = [f"{verb} {len(res.moves)} entr{'y' if len(res.moves) == 1 else 'ies'}."]
    if res.busy:
        lines = [f"Filing skipped: {res.note}."]
    elif res.note:
        lines.append(f"Note: {res.note}.")
    lines += [f"  - {m.entry}: {label(m.src)} -> {label(m.dst)}  [{m.why}]" for m in res.moves]
    lines += _section("Conflicts, nothing moved", [f"{p.entry}: {p.why}" for p in res.conflicts])
    lines += _section("Skipped", [f"{p.entry}: {p.why}" for p in res.skipped])
    lines += _section(
        "Contradictions: state claims an application but no evidence (left at root)",
        [f"{c.entry}: {c.why}" for c in res.contradictions],
    )
    lines += _section(
        "Submitted, filed by job state, no as-sent record", list(res.unsent_submitted)
    )
    lines += _section(
        "Staying at root (still to send)", [f"{m.entry}: {m.why}" for m in res.at_root]
    )
    return "\n".join(lines)

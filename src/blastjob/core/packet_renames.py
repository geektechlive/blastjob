"""Rename a kept job's newest packet documents to the current capped token.

Part of `blastjob-daily --file-packets` (reported by `--file-packets-report`).
Indeed refuses an upload name of 90+ characters (issue #19), and packets built
before the cap, or before an abbreviation-table change hashed a token
differently, still carry the old `{Co}_{Role}` token. For each Shortlist job
(kept, never applied: a packet still to send), the
newest packet's resume/cover-letter/ATS documents whose token is one of
`paths.document_tokens` but not `document_token` are renamed, and
`artifacts.path` is rewritten for exactly those files in one transaction.

A FAMILY (`{person}_Resume_-_{token}` with its `.md`/`.docx`/`_ATS.ats.txt`/
`.vN` variants) is renamed together or not at all, and not at all when any
member is:

- named by a `packet_sends` row: an as-sent record must keep naming the file
  that was sent;
- quarantined, or named by a QUARANTINED packet report (`held_names` refuses by
  name, so a renamed held file would escape the hold);
- named in the folder's `RELEASED.txt` (the audit trail of a quarantine
  override names the files it released);
- about to land on a name that already exists.

The company/role come from the build's own metadata (`artifacts.built_as`), the
same pair `role_files` matches on, so the renamed files are still found.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from blastjob.core import artifacts
from blastjob.core.paths import (
    canonical,
    document_token,
    document_tokens,
    locate,
    resolve_role_slug,
)
from blastjob.core.quarantine import RELEASED_FILENAME, held_names
from blastjob.core.stage_sql import SHORTLIST_WHERE

log = logging.getLogger(__name__)

_DOCUMENT_KINDS = frozenset({"resume", "cover_letter", "ats_text"})
_DOC_NAME = re.compile(
    r"^(?P<pre>.+?_(?:Resume|Cover_Letter)_-_)(?P<tok>.+?)"
    r"(?P<suf>(?:_ATS)?(?:\.v\d+)?(?:\.ats)?\.[A-Za-z]+)$"
)


@dataclass(frozen=True)
class Rename:
    job_id: int
    src: Path
    dst: Path


@dataclass(frozen=True)
class Skip:
    name: str
    why: str


@dataclass(frozen=True)
class RenamePlan:
    renames: tuple[Rename, ...] = ()
    skipped: tuple[Skip, ...] = ()
    # Index rows still naming the old file when only the new one exists (a run
    # killed between the rename and the commit): row rewrites, no file moves.
    repairs: tuple[Rename, ...] = ()


@dataclass(frozen=True)
class RenameResult:
    renamed: tuple[Rename, ...] = ()
    failed: tuple[Skip, ...] = ()


def _sent_paths(conn: sqlite3.Connection, root: Path) -> set[Path]:
    out: set[Path] = set()
    for r in conn.execute("SELECT resume_path, cover_letter_path FROM packet_sends"):
        out.update(artifacts.stored_path(raw, root) for raw in r if raw)
    return out


def _job_documents(conn: sqlite3.Connection, job: sqlite3.Row, folder: Path) -> list[Path]:
    """role_files plus the job's indexed documents in that folder."""
    found = artifacts.role_files(
        folder, company=job["company"] or "", role=job["title"] or "", job_id=job["id"]
    )
    files = {p for p in found if artifacts.kind_for(p.name) in _DOCUMENT_KINDS}
    present = {f.name for f in folder.iterdir()}
    for row in artifacts.artifacts_for(conn, int(job["id"])):
        p = Path(row["path"])
        if row["kind"] in _DOCUMENT_KINDS and p.name in present:
            files.add((folder / p.name).resolve())
    return sorted(files)


def _repairs(
    conn: sqlite3.Connection,
    job_id: int,
    folder: Path,
    root: Path,
    tokens: tuple[str, ...],
    current: str,
) -> list[Rename]:
    """Rows of this job naming an old-token document in `folder` that is gone
    while its current-token twin exists."""
    out: list[Rename] = []
    for row in artifacts.artifacts_for(conn, job_id):
        here = locate(row["path"], root)
        m = _DOC_NAME.match(here.name)
        if here.parent.resolve() != folder.resolve() or here.exists() or m is None:
            continue
        if m["tok"] in tokens and m["tok"] != current:
            twin = here.with_name(f"{m['pre']}{current}{m['suf']}")
            if twin.is_file():
                out.append(Rename(job_id, here, twin))
    return out


def _released_text(folder: Path) -> str:
    try:
        return (folder / RELEASED_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return ""


def _family_problem(
    members: list[tuple[Path, Path]],
    held: frozenset[str],
    released: str,
    sent: set[Path],
    root: Path,
) -> str:
    for src, dst in members:
        if src.parent.name == "quarantine" or src.name in held:
            return "quarantined or held by its packet report"
        if src.name in released:
            return f"{src.name} is named in {RELEASED_FILENAME}"
        if artifacts.stored_path(src, root) in sent:
            return f"{src.name} is named by an as-sent record"
        if os.path.lexists(dst):
            return f"{dst.name} already exists"
    return ""


def _plan_job(
    conn: sqlite3.Connection, job: sqlite3.Row, root: Path, sent: set[Path]
) -> tuple[list[Rename], list[Skip], list[Rename]]:
    packet = artifacts.latest_packet(conn, int(job["id"]), output_root=root)
    if packet is None or (packet[1] / "metadata.json").is_file():
        return [], [], []  # no packet, or a legacy per-role folder with bare names
    folder = packet[1]
    company, role = artifacts.built_as(
        folder, resolve_role_slug(folder, job["title"] or ""), job["company"] or "", job["title"]
    )
    tokens, current = document_tokens(company, role), document_token(company, role)
    families: dict[str, list[tuple[Path, Path]]] = {}
    for src in _job_documents(conn, job, folder):
        m = _DOC_NAME.match(src.name)
        if m is None or m["tok"] not in tokens or m["tok"] == current:
            continue
        dst = src.with_name(f"{m['pre']}{current}{m['suf']}")
        families.setdefault(f"{m['pre']}{m['tok']}", []).append((src, dst))
    held, released = held_names(folder), _released_text(folder)
    renames: list[Rename] = []
    skipped: list[Skip] = []
    for family, members in sorted(families.items()):
        problem = _family_problem(members, held, released, sent, root)
        if problem:
            skipped.append(Skip(family, f"#{job['id']}: {problem}"))
            continue
        renames += [Rename(int(job["id"]), src, dst) for src, dst in members]
    return renames, skipped, _repairs(conn, int(job["id"]), folder, root, tokens, current)


def plan_renames(conn: sqlite3.Connection, output_root: str | Path) -> RenamePlan:
    """What the rename step would do. Reads only."""
    root = Path(os.path.expanduser(str(output_root)))
    if not root.is_dir():
        return RenamePlan()
    sent = _sent_paths(conn, root)
    renames: list[Rename] = []
    skipped: list[Skip] = []
    repairs: list[Rename] = []
    # Shortlist only: a packet still to send. An application already went out
    # under the old name (often with no as-sent record, which predates migration
    # 39), and a closed or archived role will never be uploaded.
    jobs = conn.execute(
        "SELECT id, company, title FROM jobs "
        f"WHERE {SHORTLIST_WHERE} AND COALESCE(applied_at,'')='' ORDER BY id"
    ).fetchall()
    for job in jobs:
        r, s, fix = _plan_job(conn, job, root, sent)
        renames += r
        skipped += s
        repairs += fix
    return RenamePlan(tuple(renames), tuple(skipped), tuple(repairs))


def _insert_row(conn: sqlite3.Connection, r: Rename, path: str) -> None:
    """Index a renamed file that had no row, inside the caller's transaction
    (`artifacts.register` commits, which would end it)."""
    data = r.dst.read_bytes()
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO artifacts (job_id, kind, path, sha256, bytes, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            r.job_id,
            artifacts.kind_for(r.dst.name),
            path,
            hashlib.sha256(data).hexdigest(),
            len(data),
            now,
            now,
        ),
    )


def _point_row(conn: sqlite3.Connection, r: Rename, root: Path, *, repair: bool) -> None:
    """Make the index name `r.dst`, never a missing file. A rename whose row
    is missing gets one; a repair whose target is already indexed drops the
    stale old-name row instead of tripping `artifacts.path UNIQUE`."""
    old = str(artifacts.stored_path(r.src, root))
    new = str(canonical(r.dst.resolve(), root))
    if repair and conn.execute("SELECT 1 FROM artifacts WHERE path=?", (new,)).fetchone():
        conn.execute("DELETE FROM artifacts WHERE path=?", (old,))
        return
    if conn.execute("UPDATE artifacts SET path=? WHERE path=?", (new, old)).rowcount == 0:
        _insert_row(conn, r, new)


def apply_renames(
    conn: sqlite3.Connection, output_root: str | Path, plan: RenamePlan
) -> RenameResult:
    """Rename the files, then rewrite their `artifacts.path` rows, in ONE
    transaction; any failure renames every file back and rolls back. The
    caller holds the filing lock (`core/filing_lock.exclusive`)."""
    if not plan.renames and not plan.repairs:
        return RenameResult()
    root = Path(os.path.expanduser(str(output_root)))
    done: list[Rename] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for r in plan.repairs:
            _point_row(conn, r, root, repair=True)
        for r in plan.renames:
            if os.path.lexists(r.dst):
                raise FileExistsError(str(r.dst))
            os.rename(r.src, r.dst)
            done.append(r)
            _point_row(conn, r, root, repair=False)
        conn.commit()
    except (OSError, sqlite3.Error) as exc:
        conn.rollback()
        for r in reversed(done):
            try:
                os.rename(r.dst, r.src)
            except OSError:
                log.error("could not rename %s back to %s", r.dst, r.src, exc_info=True)
        return RenameResult(failed=(Skip("rename step", f"{type(exc).__name__}: {exc}"),))
    return RenameResult(renamed=tuple(done))


def format_renames(plan: RenamePlan, *, applied: bool, result: RenameResult | None = None) -> str:
    done = result.renamed if result is not None else plan.renames
    verb = "Renamed" if applied else "Would rename"
    lines = [f"{verb} {len(done)} packet document(s) to the current capped name."]
    lines += [f"  - #{r.job_id} {r.src.parent.name}/{r.src.name} -> {r.dst.name}" for r in done]
    if plan.repairs:
        fixed = "Repaired" if applied else "Would repair"
        lines += ["", f"{fixed} {len(plan.repairs)} index row(s) still naming the old file:"]
        lines += [f"  - #{r.job_id} {r.src.name} -> {r.dst.name}" for r in plan.repairs]
    problems = [*plan.skipped, *(result.failed if result is not None else ())]
    if problems:
        lines += ["", f"Not renamed ({len(problems)}):"]
        lines += [f"  - {s.name}: {s.why}" for s in problems]
    return "\n".join(lines)

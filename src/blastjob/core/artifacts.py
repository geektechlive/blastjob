"""An index of the files written for a job (migration 43).

Two output layouts exist on disk, and the current one shares a flat
`{company-slug}-{date}/` folder across every role tailored for that company
that day. So "the files for this job" is never a glob of a folder: it is the
list `role_files` derives from the build's own naming rules (`core/build.py`,
`core/paths.py`, the exporters), and once registered here it is a recorded
path rather than a recomputed one.

Registration from the build/refine/prep/follow-up hooks is best-effort
(`register_paths`): a failure logs a warning and never changes what was
written or the build's status. `register` itself is strict -- a missing file
raises -- so callers that care (the backfill) can report it.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from blastjob.core.paths import name_token, resolve_role_slug
from blastjob.core.quarantine import quarantined_files

log = logging.getLogger(__name__)

ARTIFACT_KINDS: tuple[str, ...] = (
    "resume",
    "cover_letter",
    "ats_text",
    "job_description",
    "company_research",
    "fit_score",
    "cover_letter_score",
    "coverage_gaps",
    "metadata",
    "packet_report",
    "apply_link",
    "interview_prep",
    "followup",
    "transcript",
    "debrief",
    "cheatsheet",
    "other",
)

# Role-prefixed sidecars, `{role_slug}.<suffix>` (core/build.py); the legacy
# per-role layout wrote the same names bare. Longest suffix first matters:
# `.cover_letter_score.json` must win over anything cover-letter shaped.
_SIDECARS: tuple[tuple[str, str], ...] = (
    ("cover_letter_score.json", "cover_letter_score"),
    ("cover_letter_score.md", "cover_letter_score"),
    ("company_research.md", "company_research"),
    ("job_description.md", "job_description"),
    ("packet_report.json", "packet_report"),
    ("interview_prep.md", "interview_prep"),
    ("coverage_gaps.md", "coverage_gaps"),
    ("fit_score.json", "fit_score"),
    ("fit_score.md", "fit_score"),
    ("metadata.json", "metadata"),
    ("apply_link.html", "apply_link"),
)
_FOLLOWUP = re.compile(r"\.followup-\d+\.md$")
_HAND_FILED: tuple[tuple[str, str], ...] = (
    ("transcript", "transcript"),
    ("debrief", "debrief"),
    ("cheatsheet", "cheatsheet"),
)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def kind_for(filename: str) -> str:
    """The artifact kind a filename implies, from the names the build writes."""
    name = Path(filename).name
    for suffix, kind in _SIDECARS:
        if name == suffix or name.endswith(f".{suffix}"):
            return kind
    if _FOLLOWUP.search(name):
        return "followup"
    if name.endswith(".ats.txt"):
        return "ats_text"
    stem = name.split(".", 1)[0]
    if "_Cover_Letter_-_" in name or stem == "cover_letter":
        return "cover_letter"
    if "_Resume_-_" in name or stem == "resume":
        return "resume"
    squashed = re.sub(r"[^a-z]", "", name.lower())
    for word, kind in _HAND_FILED:
        if word in squashed:
            return kind
    return "other"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def register(
    conn: sqlite3.Connection,
    path: str | Path,
    kind: str,
    *,
    job_id: int | None = None,
    build_run_id: int | None = None,
    note: str = "",
) -> int:
    """Idempotent upsert by absolute path; commits. A re-register refreshes
    sha256/bytes/updated_at and fills job_id/build_run_id only where they are
    still NULL -- an artifact is never reassigned to another job or run.
    A missing file raises FileNotFoundError; an unknown kind, ValueError."""
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"unknown artifact kind: {kind!r}")
    target = Path(path).expanduser().resolve()
    if not target.is_file():
        raise FileNotFoundError(str(target))
    sha, size, now = _sha256(target), target.stat().st_size, _now()
    conn.execute(
        "INSERT INTO artifacts (job_id, build_run_id, kind, path, sha256, bytes, note, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, bytes=excluded.bytes, "
        "updated_at=excluded.updated_at, "
        "job_id=COALESCE(artifacts.job_id, excluded.job_id), "
        "build_run_id=COALESCE(artifacts.build_run_id, excluded.build_run_id)",
        (job_id, build_run_id, kind, str(target), sha, size, note, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM artifacts WHERE path=?", (str(target),)).fetchone()
    return int(row["id"])


def is_registered(conn: sqlite3.Connection, path: str | Path) -> bool:
    target = str(Path(path).expanduser().resolve())
    return conn.execute("SELECT 1 FROM artifacts WHERE path=?", (target,)).fetchone() is not None


def artifacts_for(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    """A job's registered files, most recently touched first."""
    return conn.execute(
        "SELECT * FROM artifacts WHERE job_id=? ORDER BY updated_at DESC, id DESC", (job_id,)
    ).fetchall()


def register_paths(
    conn: sqlite3.Connection,
    paths: Iterable[Path],
    *,
    job_id: int | None,
    build_run_id: int | None = None,
) -> int:
    """Best-effort registration for the write hooks. Never raises; a failure is
    a logged warning. Returns how many paths were registered."""
    count = 0
    for path in paths:
        try:
            register(conn, path, kind_for(path.name), job_id=job_id, build_run_id=build_run_id)
            count += 1
        except Exception:  # noqa: BLE001 -- bookkeeping never fails the work
            log.warning("could not register artifact %s for job %s", path, job_id, exc_info=True)
    return count


def build_run_pk(conn: sqlite3.Connection, run_id: str) -> int | None:
    """`build_runs.id` for a runtime run id."""
    row = conn.execute("SELECT id FROM build_runs WHERE run_id=?", (run_id,)).fetchone()
    return int(row["id"]) if row else None


def run_for_dir(conn: sqlite3.Connection, job_id: int, artifact_dir: str | Path) -> int | None:
    """This job's newest successful build whose recorded folder is `artifact_dir`."""
    want = Path(artifact_dir).expanduser().resolve()
    rows = conn.execute(
        "SELECT id, artifact_dir FROM build_runs WHERE job_id=? AND status='done' "
        "AND artifact_dir != '' ORDER BY id DESC",
        (job_id,),
    ).fetchall()
    for row in rows:
        if Path(row["artifact_dir"]).expanduser().resolve() == want:
            return int(row["id"])
    return None


def latest_packet(conn: sqlite3.Connection, job_id: int) -> tuple[int, Path] | None:
    """(build_runs.id, folder) of the job's newest successful build, when that
    folder still exists -- the same row `web.packets.job_packet_dirs` puts
    first. None when there is no such build or its folder is gone."""
    row = conn.execute(
        "SELECT id, artifact_dir FROM build_runs WHERE job_id=? AND status='done' "
        "AND artifact_dir != '' ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    folder = Path(row["artifact_dir"]).expanduser()
    return (int(row["id"]), folder) if folder.is_dir() else None


# --- which files in a packet folder belong to one role -------------------------


def _built_as(folder: Path, slug: str, company: str, role: str) -> tuple[str, str]:
    """The company/role the build used, from `{slug}.metadata.json` when it
    records them (the job's title may have been edited since)."""
    try:
        data = json.loads((folder / f"{slug}.metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return company, role
    return str(data.get("company") or company), str(data.get("role") or role)


def _document_pattern(company: str, role: str) -> re.Pattern[str]:
    """Resume/cover-letter files for exactly this company+role token pair --
    `_VP_IT` must not swallow `_VP_IT_and_Data` -- including the `_ATS` export
    and refine's archived `.vN.md` rotations."""
    tokens = re.escape(f"{name_token(company)}_{name_token(role)}")
    return re.compile(
        rf"_(?:Resume|Cover_Letter)_-_{tokens}(?:_ATS)?(?:\.v\d+)?(?:\.ats)?\.[A-Za-z]+$"
    )


def _candidates(folder: Path) -> list[Path]:
    files = [p for p in sorted(folder.iterdir()) if p.is_file()]
    return files + quarantined_files(folder)


def role_files(folder: str | Path, *, company: str, role: str, job_id: int) -> list[Path]:
    """The files in a packet folder that belong to one role, resolved.

    A legacy per-role folder (bare `metadata.json`) belongs wholly to its one
    role. A current-layout folder is shared, so only these count: the role's
    `{role_slug}.*` sidecars, its resume/cover-letter documents (top level or
    quarantined), and follow-up drafts written for this job id."""
    base = Path(folder).expanduser()
    if not base.is_dir():
        return []
    if (base / "metadata.json").is_file():
        return [p.resolve() for p in _candidates(base)]
    slug = resolve_role_slug(base, role)
    built_company, built_role = _built_as(base, slug, company, role)
    documents = _document_pattern(built_company, built_role)
    own_followup = f".followup-{job_id}.md"

    def belongs(name: str) -> bool:
        if _FOLLOWUP.search(name):
            # Named by slugify(role) + job id: another job's draft can share
            # the slug prefix, never the id.
            return name.endswith(own_followup)
        return name.startswith(f"{slug}.") or bool(documents.search(name))

    out = [p.resolve() for p in _candidates(base) if belongs(p.name)]
    return list(dict.fromkeys(out))

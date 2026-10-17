"""Surface a tailored bundle's location -- read-only lookup over `build_runs`.

The build pipeline (`web/build.py`) already saves the artifact directory to
`build_runs.artifact_dir` (`db/client.py:finish_build`), but nothing in the app
ever reads it back: the SSE "Saved bundle to {out_dir}" log line is
immediately overwritten by the `done` handler, and there is no `SELECT
artifact_dir` anywhere else in `src/`. This module is the read side of that
column -- resolving "what packet is this job's, is it still there, and is it
stale" without ever recomputing the folder from today's slugs (there are two
folder layouts on disk from different points in the project's life; only the
stored path is trustworthy for a bundle built on an earlier day).

Bug caught in independent review of the first draft of this module: querying
`WHERE status='done' ORDER BY ... LIMIT 1` picks the best *successful* run,
which silently hides a *failed* re-tailor that happened after it -- the UI
would show a ready packet while a fresh attempt actually failed. The fix:
always order by recency first, look at the true latest attempt's status, and
only fall back to an earlier successful run when the latest one didn't
produce a directory -- labelling that fallback explicitly (`previous=True`).
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from blastjob import config as cfg_mod
from blastjob.core import quarantine as qz
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig

# (relative path under data_dir, human label used in the staleness reason)
_TRACKED_FILES: tuple[tuple[str, str], ...] = (
    ("work_history.md", "work history"),
    ("templates/standard.md", "resume template"),
    ("templates/ats.md", "ATS template"),
)

_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


def _expand(raw: str) -> Path:
    return Path(os.path.expanduser(raw))


def packet_for_job(
    conn,
    job_id: int,
    load_config=cfg_mod.load_config,
) -> dict[str, Any] | None:
    """The tailored-bundle packet for a job, or None if it was never tailored.

    Queries every `build_runs` row for the job ordered by recency -- NOT
    filtered to status='done' before ordering, so a failed latest attempt is
    never masked by an earlier success. If the latest run succeeded, its
    directory is the packet. If the latest run did not (failed, or still
    running with no directory yet) but an earlier run did succeed, that
    earlier directory is returned with `previous=True` and `latest_status`
    set to the true latest attempt's status, so the caller can tell the user
    the packet on screen is not from the most recent try.
    """
    rows = conn.execute(
        # A `refine:` row is the refine's claim on the folder (a lock), not a
        # build attempt, so it never makes the current packet look "previous".
        "SELECT run_id, status, artifact_dir, created_at "
        "FROM build_runs WHERE job_id=? AND run_id NOT LIKE ? ORDER BY id DESC",
        (job_id, f"{dbc.REFINE_RUN_PREFIX}%"),
    ).fetchall()
    if not rows:
        return None

    latest = rows[0]
    latest_status = latest["status"]

    chosen = None
    previous = False
    if latest["status"] == "done" and latest["artifact_dir"]:
        chosen = latest
    else:
        for row in rows[1:]:
            if row["status"] == "done" and row["artifact_dir"]:
                chosen = row
                previous = True
                break

    if chosen is None:
        return {
            "dir": "",
            "built_at": latest["created_at"],
            "exists": False,
            "status": latest_status,
            "stale": False,
            "stale_reason": "",
            "files": [],
            "previous": False,
            "latest_status": latest_status,
            "quarantined": False,
        }

    artifact_dir = chosen["artifact_dir"]
    path = _expand(artifact_dir)
    exists = path.is_dir()
    stale, stale_reason = staleness(chosen["created_at"], load_config()) if exists else (False, "")
    if exists:
        jd_stale, jd_reason = jd_staleness(conn, job_id, chosen["created_at"])
        if jd_stale:
            stale, stale_reason = True, jd_reason

    return {
        "dir": artifact_dir,
        "built_at": chosen["created_at"],
        "exists": exists,
        "status": chosen["status"],
        "stale": stale,
        "stale_reason": stale_reason,
        "files": packet_files(path) if exists else [],
        "previous": previous,
        "latest_status": latest_status,
        "quarantined": qz.is_quarantined(path) if exists else False,
    }


def _file_entry(entry: Path, *, quarantined: bool) -> dict[str, Any]:
    return {
        "name": entry.name,
        "size": entry.stat().st_size,
        # Legacy nested packets wrote a bare resume.* / cover_letter.*.
        "is_resume": "_Resume_-_" in entry.name or entry.stem == "resume",
        "is_cover": "_Cover_Letter_-_" in entry.name or entry.stem == "cover_letter",
        "quarantined": quarantined,
    }


def packet_files(path: Path) -> list[dict[str, Any]]:
    """Listing of a bundle directory's files, classified for the UI.

    Bundles are a flat folder produced by `core/build.py`, plus exactly one
    known subdirectory: `quarantine/` (see `core/quarantine.py`), where a
    fatal packet-gate finding moves the generated documents that are not
    sendable. This is NOT a recursive walk -- only that one known
    subdirectory is ever descended, so a quarantined packet still lists its
    documents (flagged `quarantined: True`) instead of reading as an empty
    folder. Resume files carry `_Resume_-_` in the stem (including the
    `_ATS` variant), cover letters carry `_Cover_Letter_-_`.
    """
    path = Path(path)
    if not path.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(path.iterdir(), key=lambda p: p.name):
        if not entry.is_file():
            continue
        out.append(_file_entry(entry, quarantined=False))
    for entry in qz.quarantined_files(path):
        out.append(_file_entry(entry, quarantined=True))
    return out


# Compacted characters of a title that must appear in a truncated filename.
_TRUNCATED_TITLE_CHARS = 30


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def role_documents(files: list[dict[str, Any]], title: str) -> dict[str, str]:
    """The resume and cover letter .docx that belong to `title`.

    A current-layout folder is shared by every role tailored for that company
    that day, so a bare "first resume in the folder" can hand over another
    role's resume. The builder puts the role in the filename, so match on it;
    legacy per-role folders use bare names and have only one of each."""
    want = _compact(title)
    docs: dict[str, str] = {}
    for kind in ("resume", "cover"):
        candidates = [
            f["name"]
            for f in files
            if f[f"is_{kind}"] and f["name"].endswith(".docx") and not f.get("quarantined")
        ]
        mine = [n for n in candidates if want and want in _compact(n)]
        # The builder truncates long titles in filenames, so a leading slice of
        # the title is the fallback -- long enough that two roles won't share it.
        head = want[:_TRUNCATED_TITLE_CHARS]
        truncated = [
            n for n in candidates if len(head) == _TRUNCATED_TITLE_CHARS and head in _compact(n)
        ]
        bare = [n for n in candidates if Path(n).stem in ("resume", "cover_letter")]
        pick = mine or truncated or bare
        if pick:
            docs[kind] = pick[0]
    return docs


def packet_file_path(cfg: BlastJobConfig, packet_dir: str, name: str) -> Path | None:
    """A file to serve from a packet, or None. The name must be exactly one of
    the non-quarantined files directly inside the packet folder, and the
    folder must lie under the configured output directory -- so no path from
    a URL reaches disk. A quarantined document is listed (see `packet_files`)
    so the packet is legible, but it is never servable -- the only route to a
    download is Release (`core/quarantine.release`)."""
    folder = safe_output_path(cfg, packet_dir)
    if folder is None or not folder.is_dir():
        return None
    allowed = {f["name"] for f in packet_files(folder) if not f["quarantined"]}
    if name not in allowed:
        return None
    # `folder` is resolved, `name` may be a symlink: serve the file only if it
    # still resolves to somewhere inside that folder.
    target = (folder / name).resolve()
    try:
        target.relative_to(folder)
    except ValueError:
        return None
    return folder / name


def job_packet_dirs(conn, job_id: int) -> list[str]:
    """Artifact folders of every successful build for a job, newest first."""
    rows = conn.execute(
        "SELECT artifact_dir FROM build_runs WHERE job_id=? AND status='done' "
        "AND artifact_dir != '' ORDER BY id DESC",
        (job_id,),
    ).fetchall()
    return list(dict.fromkeys(r["artifact_dir"] for r in rows))


def history_job_ids(conn, entries: list) -> dict[tuple[Path, str], int]:
    """Map each history entry's folder+role to the job whose build produced it.

    Only `build_runs.artifact_dir` is trusted (two layouts exist on disk, so a
    path is never recomputed). A shared company-day folder can hold several
    roles; the job is chosen by title, and an ambiguous folder links nothing."""
    by_dir: dict[Path, list[tuple[int, str]]] = {}
    rows = conn.execute(
        "SELECT DISTINCT b.job_id, b.artifact_dir, j.title FROM build_runs b "
        "JOIN jobs j ON j.id = b.job_id WHERE b.status='done' AND b.artifact_dir != ''"
    ).fetchall()
    for r in rows:
        try:
            key = _expand(r["artifact_dir"]).resolve()
        except (OSError, RuntimeError):
            continue
        by_dir.setdefault(key, []).append((int(r["job_id"]), r["title"] or ""))
    entries_per_dir: dict[Path, int] = {}
    for e in entries:
        entries_per_dir[e.path.resolve()] = entries_per_dir.get(e.path.resolve(), 0) + 1
    out: dict[tuple[Path, str], int] = {}
    for e in entries:
        folder = e.path.resolve()
        jobs = dict(by_dir.get(folder, []))
        same_title = [jid for jid, title in jobs.items() if _compact(title) == _compact(e.role)]
        if len(same_title) == 1:
            out[(e.path, e.role)] = same_title[0]
        elif not same_title and len(jobs) == 1 and entries_per_dir[folder] == 1:
            # One build, one packet: the title was edited after tailoring.
            out[(e.path, e.role)] = next(iter(jobs))
    return out


def jd_staleness(conn, job_id: int, built_at: str) -> tuple[bool, str]:
    """Whether the packet was tailored to less than the real job description:
    built from a snippet we still hold, or built before the user pasted the full
    description in (or the employer's own posting was fetched). Either way it reads as
    tailored when it was not."""
    job = conn.execute(
        "SELECT raw_jd, jd_pasted_at, jd_fetched_at FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    if job is None:
        return False, ""
    if dbc.is_jd_snippet(job["raw_jd"], job["jd_pasted_at"]):
        return True, (
            "built from a snippet of the job description, not the full posting; "
            "paste the full description and re-tailor"
        )
    if job["jd_pasted_at"] and job["jd_pasted_at"] > (built_at or ""):
        return True, "the full job description was pasted after this packet was built"
    if job["jd_fetched_at"] and job["jd_fetched_at"] > (built_at or ""):
        return True, (
            "the employer's full description arrived after this packet was built; re-tailor"
        )
    return False, ""


def staleness(built_at: str, cfg: BlastJobConfig) -> tuple[bool, str]:
    """Whether the packet was built before the source material it draws from.

    Compares `built_at` against the mtimes of work_history.md and the two
    templates in the data dir. A packet built before any of those changed is
    stale -- e.g. a corrected work-history credential means every packet
    built before the correction still carries the superseded claim.
    """
    try:
        built_dt = datetime.fromisoformat(built_at)
    except (TypeError, ValueError):
        return False, ""

    data_dir = cfg_mod.data_dir(cfg)
    newer: list[tuple[datetime, str]] = []
    for rel_path, label in _TRACKED_FILES:
        candidate = data_dir / rel_path
        if not candidate.is_file():
            continue
        mtime = datetime.fromtimestamp(candidate.stat().st_mtime)
        if mtime > built_dt:
            newer.append((mtime, label))

    if not newer:
        return False, ""

    newer.sort(reverse=True)
    mtime, label = newer[0]
    reason = (
        f"{label} updated {mtime.date().isoformat()}, packet built {built_dt.date().isoformat()}"
    )
    return True, reason


def is_local_request(request: Any) -> bool:
    """True only for a request whose TCP peer is the Mac itself (loopback).

    The cockpit can bind to the LAN for access from another device, and the Origin check in
    `security.py` accepts any RFC1918 address -- it cannot tell the Mac from
    that device. Only `request.client.host`, the actual peer address, can. A
    "Reveal in Finder" action must gate on this: `subprocess`-opening a
    Finder window is a no-op (or worse, confusing) when the click came from
    a tablet on the LAN.
    """
    client = getattr(request, "client", None)
    if client is None:
        return False
    return getattr(client, "host", None) in _LOOPBACK_HOSTS


def safe_output_path(cfg: BlastJobConfig, raw: str) -> Path | None:
    """Resolve `raw` and return it only if it lies under the configured
    output_dir -- the guard every reveal/serve path must call before touching
    a path that ultimately came from the database."""
    if not raw:
        return None
    root = cfg_mod.output_dir(cfg).resolve()
    try:
        candidate = _expand(raw).resolve()
    except (OSError, RuntimeError):
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate

"""Work history editor for the web cockpit.

`work_history.md` is the single input every resume, cover letter, interview prep
pack, and groundedness judgment derives from. This module lets the cockpit read and
save it directly, so the cockpit is the only surface the user needs.

Save path mirrors the existing tmp-write + `os.replace` convention used elsewhere
in the codebase (`core/ingest.py`) so a failed or
partial write can never truncate the canonical file, plus a timestamped backup
before overwrite. The backup filename follows the naming convention already
established for DB backups (`db/client.py`: `blastjob-YYYYMMDD-HHMMSS.db` written
into `backups/` under the data dir) rather than inventing a new one, with a
numeric suffix appended if two backups would otherwise land on the same second.

Lost-update protection: the GET view carries a sha256 of the file's current
content (`version`). A save must submit the version it was loaded with
(`expected_version`); if the file changed on disk since then (e.g. a second
browser tab saved first), the save is rejected rather than silently overwriting
someone else's edit — this file is the canonical factual source for every
generated document, so a silent revert is the worst failure mode it can have.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from blastjob import config as cfg_mod
from blastjob.models.config import BlastJobConfig

FILENAME = "work_history.md"
BACKUP_FILE_MODE = 0o600  # backups hold a person's history: owner-only


class EmptyWorkHistoryError(ValueError):
    """Raised when a save would blank out work_history.md."""


@dataclass(frozen=True)
class WorkHistoryView:
    content: str
    exists: bool
    mtime: datetime | None
    size_bytes: int
    version: str


class WorkHistoryConflictError(ValueError):
    """Raised when the on-disk file no longer matches the version the save
    was based on -- someone else (another tab) saved first."""

    def __init__(self, current: WorkHistoryView):
        super().__init__(
            "Work history changed on disk since this page was loaded "
            "(likely saved from another tab). Reload to see the "
            "latest version before saving again."
        )
        self.current = current


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def with_content(view: WorkHistoryView, content: str) -> WorkHistoryView:
    """Return a copy of `view` with only its content field replaced -- used to
    re-render a rejected save while preserving what the user actually typed."""
    return replace(view, content=content)


def work_history_path(cfg: BlastJobConfig) -> Path:
    return cfg_mod.data_dir(cfg) / FILENAME


def read_work_history(cfg: BlastJobConfig) -> WorkHistoryView:
    path = work_history_path(cfg)
    if not path.exists():
        empty = ""
        return WorkHistoryView(
            content=empty, exists=False, mtime=None, size_bytes=0, version=content_hash(empty)
        )
    stat = path.stat()
    content = path.read_text(encoding="utf-8")
    return WorkHistoryView(
        content=content,
        exists=True,
        mtime=datetime.fromtimestamp(stat.st_mtime),
        size_bytes=stat.st_size,
        version=content_hash(content),
    )


def backup_work_history(
    path: Path, data_dir: Path, *, now: datetime | None = None, label: str = ""
) -> Path:
    """Copy `path` into `<data_dir>/backups/work_history-<stamp>[-<label>].md`,
    with a numeric suffix on a same-second collision. Public so any caller that
    is about to overwrite work_history.md (the web save route, the ingest
    route) uses the one backup implementation rather than a second copy of it.
    """
    backups_dir = cfg_mod.ensure_private_dir(data_dir / "backups")
    ts = now or datetime.now()
    base_name = f"work_history-{ts:%Y%m%d-%H%M%S}"
    if label:
        base_name += f"-{label}"

    # Open the source first: an unreadable one must not leave a zero-byte backup behind
    # that later passes for a real snapshot.
    src_file = open(path, "rb")  # noqa: SIM115 - closed in the with-block below
    dest = backups_dir / f"{base_name}.md"
    suffix = 1
    while True:
        try:
            # O_EXCL: never overwrite, and the copy is born owner-only (0600) whatever
            # the source's mode or the umask (copy2 would carry the source's mode over).
            fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, BACKUP_FILE_MODE)
            break
        except FileExistsError:
            dest = backups_dir / f"{base_name}-{suffix}.md"
            suffix += 1

    try:
        with src_file as src, os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(src, out)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    st = path.stat()
    os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))  # keep the source's times, as copy2 did
    return dest


def save_work_history(
    cfg: BlastJobConfig,
    content: str,
    *,
    expected_version: str,
    now: datetime | None = None,
) -> WorkHistoryView:
    """Back up the existing file (if any), then atomically replace it.

    Raises `EmptyWorkHistoryError` for a blank/whitespace-only body, and
    `WorkHistoryConflictError` if the file on disk no longer matches
    `expected_version` -- in both cases nothing on disk is touched. Any I/O
    failure during the write propagates (typically `OSError`) with the
    original file left untouched, since only the final `os.replace` call ever
    touches the real path.
    """
    if not content.strip():
        raise EmptyWorkHistoryError("Work history cannot be empty.")

    current = read_work_history(cfg)
    if current.version != expected_version:
        raise WorkHistoryConflictError(current)

    data_dir = cfg_mod.ensure_private_dir(cfg_mod.data_dir(cfg))
    path = data_dir / FILENAME

    if path.exists():
        backup_work_history(path, data_dir, now=now)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)

    return read_work_history(cfg)

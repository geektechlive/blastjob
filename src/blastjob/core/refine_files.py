"""File handling for a resume refinement: lease, staging, publishing.

Split out of `core/refine.py`. A refinement rewrites documents in a packet folder
that other roles share and that the cockpit serves downloads from, so:

- a per-resume LEASE (an exclusive lock file) stops two refinements racing for the
  same version number or the same files;
- everything is written to a STAGING folder first (`.refine/stage-*`, a directory,
  so the packet listing never offers its contents for download) and moved into
  place with `os.replace`, so a reader never sees a half-written document;
- replaced or retired files are PARKED, never deleted: they are evidence.
"""

from __future__ import annotations

import fcntl
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from blastjob.core.quarantine import DOCUMENT_EXTENSIONS

REFINE_DIRNAME = ".refine"
_QUARANTINE = "quarantine"
_SUPERSEDED = "superseded"
LOCK_ATTEMPTS = 5


class RefineBusyError(RuntimeError):
    """Another refinement of this resume is already running."""


def _try_lock(path: Path) -> int | None:
    """Open `path` and take an exclusive, non-blocking `flock`; the fd, or None
    when another holder has it. The kernel drops the lock when its holder dies, so
    liveness is never guessed from an age. Retries when the file it locked was
    unlinked by the previous holder meanwhile (it would be a lock on nothing)."""
    for _ in range(LOCK_ATTEMPTS):
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        try:
            same_file = os.fstat(fd).st_ino == os.stat(path).st_ino
        except FileNotFoundError:
            same_file = False
        if same_file:
            return fd
        os.close(fd)
    return None


@contextmanager
def lease(run_dir: Path, stem: str) -> Iterator[None]:
    """Hold the exclusive refine lease for one resume stem, or raise
    `RefineBusyError`.

    An `flock` on `.refine/{stem}.lock`: it lives exactly as long as the holding
    process, so a slow refine (several model calls, each up to the CLI timeout) is
    never mistaken for a crashed one, a crashed one frees the lock by dying, and
    two waiters can never both take it over. The file is removed on release.
    """
    folder = run_dir / REFINE_DIRNAME
    folder.mkdir(exist_ok=True)
    path = folder / f"{stem}.lock"
    fd = _try_lock(path)
    if fd is None:
        raise RefineBusyError("A revision of this resume is already in progress.")
    try:
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        # Unlink while still holding the lock, so no waiter can lock a dead inode.
        path.unlink(missing_ok=True)
        os.close(fd)


def new_stage(run_dir: Path) -> Path:
    """A fresh private staging folder under `.refine/`."""
    folder = run_dir / REFINE_DIRNAME
    folder.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="stage-", dir=folder))


def remove_stage(stage: Path) -> None:
    """Drop a staging folder and whatever a failed run left in it."""
    if not stage.is_dir():
        return
    for p in stage.iterdir():
        p.unlink(missing_ok=True)
    stage.rmdir()


def _superseded_dir(run_dir: Path) -> Path:
    folder = run_dir / _QUARANTINE / _SUPERSEDED
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def park(path: Path, folder: Path, *, when: datetime | None = None) -> Path:
    """Move `path` into `folder` under a unique, timestamped name."""
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = folder / f"{path.name}.{stamp}"
    n = 1
    while target.exists():
        n += 1
        target = folder / f"{path.name}.{stamp}-{n}"
    os.replace(path, target)
    return target


def highest_version(run_dir: Path, stem: str) -> int:
    """Highest `{stem}.v{N}.md` across the packet folder and its quarantine
    folders, so a numbering never repeats after a rotation moved a file."""
    pattern = re.compile(rf"^{re.escape(stem)}\.v(\d+)\.md$")
    folders = [run_dir, run_dir / _QUARANTINE, run_dir / _QUARANTINE / _SUPERSEDED]
    highest = 0
    for folder in folders:
        if not folder.is_dir():
            continue
        for p in folder.iterdir():
            m = pattern.match(p.name)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest


def stem_names(stem: str) -> list[str]:
    """Every document name a stem (and its `_ATS` twin) can have."""
    return [f"{s}{ext}" for s in (stem, f"{stem}_ATS") for ext in DOCUMENT_EXTENSIONS]


def publish(
    stage: Path, run_dir: Path, stem: str, *, prior_n: int, source_in_quarantine: bool
) -> list[str]:
    """Rotate the old resume and move the staged documents into the packet folder.

    The old `{stem}.md` becomes `{stem}.v{prior_n}.md` beside it, or under
    `quarantine/superseded/` when it was a held (failed) version: an archived
    copy of a failed resume must not become a downloadable "older version". An
    old export (docx/pdf/ats) the new build did not regenerate is parked, so the
    folder never holds a stale export beside a new resume. Returns the names
    published.
    """
    source_folder = run_dir / _QUARANTINE if source_in_quarantine else run_dir
    old_md = source_folder / f"{stem}.md"
    rotated: Path | None = None
    if old_md.is_file():
        if source_in_quarantine:
            dest = _superseded_dir(run_dir) / f"{stem}.v{prior_n}.md"
        else:
            dest = run_dir / f"{stem}.v{prior_n}.md"
        os.replace(old_md, dest)
        rotated = dest

    published: list[str] = []
    try:
        for name in stem_names(stem):
            staged = stage / name
            public = run_dir / name
            if staged.is_file():
                os.replace(staged, public)
                published.append(name)
            elif public.is_file():
                park(public, _superseded_dir(run_dir))
    except OSError:
        # Nothing new landed: put the original back so the role never ends up with
        # no resume at all. The staged revision stays in `stage` (the caller keeps
        # it); a partial publish is left as is rather than guessed at.
        if rotated is not None and not published and not old_md.exists():
            os.replace(rotated, old_md)
        raise
    return published


def retire_held(run_dir: Path, stem: str) -> list[str]:
    """Park every held document of `stem` (the failed version a passing refine
    replaces), so nothing of the stem remains in `quarantine/`."""
    qdir = run_dir / _QUARANTINE
    if not qdir.is_dir():
        return []
    moved: list[str] = []
    for p in sorted(qdir.iterdir()):
        if p.is_file() and p.name.startswith((f"{stem}.", f"{stem}_ATS.")):
            park(p, _superseded_dir(run_dir))
            moved.append(p.name)
    return moved

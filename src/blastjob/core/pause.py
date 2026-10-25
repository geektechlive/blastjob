"""The PAUSE file: `<data_dir>/PAUSE` makes the scheduled daily run exit without doing
anything (docs/how-it-works/daily.md). The cockpit's "Pause scheduled runs" button creates and
deletes the same file the owner could `touch` by hand.

Pausing does not stop a run already in flight; the Stop button on `/runs` does."""

from __future__ import annotations

import contextlib
import datetime as dt
import os
from pathlib import Path

PAUSE_FILE = "PAUSE"


class PauseError(OSError):
    """The PAUSE file could not be made (a symlink in its place, no permission)."""


def pause_path(data_dir: Path) -> Path:
    return data_dir / PAUSE_FILE


def is_paused(data_dir: Path) -> bool:
    return pause_path(data_dir).exists()


def paused_since(data_dir: Path) -> str:
    """When the file was made (local, to the minute), or "" when not paused or unreadable."""
    try:
        mtime = pause_path(data_dir).stat().st_mtime
    except OSError:
        return ""
    return dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")


def set_paused(data_dir: Path, paused: bool) -> None:
    """Create or delete the file. Idempotent. Never follows a symlink in the last component."""
    path = pause_path(data_dir)
    if not paused:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
    except OSError as e:
        raise PauseError(
            f"Could not create the PAUSE file ({e.strerror or type(e).__name__})."
        ) from e
    os.close(fd)

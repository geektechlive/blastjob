"""`<data_dir>/run.lock`: one discovery or matching pass at a time, across processes.

The daily run, a hand-run command and the cockpit all write the same SQLite file and spend the
same model quota, so two of them at once is never wanted (docs/how-it-works/daily.md).

- `fcntl.flock(LOCK_EX | LOCK_NB)`: a second taker fails at once with `RunLockHeld`, never
  queues. The kernel drops the lock when its holder dies, even by SIGKILL, so a crash can
  never leave a stale lock behind. Nothing ever matches a process by name.
- After locking, the holder writes its PID, what it is doing and when it started into the
  file. That text is only for the message; the lock itself is the flock.
- Reentrant inside one context: `run_daily` holds the lock across discovery and matching,
  while `run_discovery` and `run_matching` also take it (they are the orchestration boundary,
  so no caller can forget). The inner take sees the ownership token and does nothing.
  A different task or thread, which has no token, contends like any other process: the open
  file descriptions differ, so flock refuses it.
- The descriptor is close-on-exec, so a spawned model child never inherits the lock.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

LOCK_NAME = "run.lock"


class _Ownership:
    """One held lock. A task created inside `hold` copies the context, so it can outlive the
    block; `live` is cleared on release, so a leftover copy never counts as holding the lock."""

    def __init__(self) -> None:
        self.live = True


# Lock path -> ownership this context holds (the reentrancy token).
_owned: ContextVar[dict[str, _Ownership]] = ContextVar("blastjob_run_lock_owned")


@dataclass(frozen=True)
class Holder:
    pid: int = 0
    what: str = ""
    started_at: str = ""


class RunLockHeld(RuntimeError):
    """Another run holds the lock. `str(exc)` is a message fit for the cockpit and the log."""

    def __init__(self, holder: Holder, reason: str = "") -> None:
        self.holder = holder
        super().__init__(reason or describe(holder))


def describe(holder: Holder) -> str:
    """ "Another run is in progress (daily run, started 07:30)", with whatever is known."""
    parts = [holder.what] if holder.what else []
    try:
        started = dt.datetime.fromisoformat(holder.started_at).strftime("%H:%M")
    except ValueError:
        started = ""
    if started:
        parts.append(f"started {started}")
    return "Another run is in progress" + (f" ({', '.join(parts)})" if parts else "")


MAX_HOLDER_TEXT = 80


def _clean(value: object) -> str:
    """Text read back from the lock file: control characters dropped, length capped. The file
    is only a note, but its text reaches the log, the cockpit and the digest footer."""
    text = "".join(ch for ch in str(value) if ch.isprintable())
    return text[:MAX_HOLDER_TEXT]


def _unavailable(e: OSError) -> str:
    return f"The run lock could not be taken ({e.strerror or type(e).__name__}); not running"


def _read_holder(fd: int) -> Holder:
    try:
        raw = os.pread(fd, 4096, 0).decode("utf-8", "replace")
        data = json.loads(raw)
        return Holder(
            pid=int(data.get("pid", 0)),
            what=_clean(data.get("what", "")),
            started_at=_clean(data.get("started_at", "")),
        )
    except (OSError, ValueError, AttributeError, TypeError):
        return Holder()  # empty or half-written: still held, just unnamed


def _write_holder(fd: int, what: str) -> None:
    info = {
        "pid": os.getpid(),
        "what": what,
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    os.ftruncate(fd, 0)
    os.pwrite(fd, json.dumps(info).encode("utf-8"), 0)


def holder_of(data_dir: str | Path) -> Holder | None:
    """Who holds the lock right now, or None when it is free. For tests and diagnostics only:
    it finds out by taking the lock briefly, so a real run that starts in that instant would be
    refused. Never call it from a page or a poll."""
    path = Path(data_dir) / LOCK_NAME
    if not path.exists():
        return None
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _read_holder(fd)
        return None
    finally:
        os.close(fd)


@contextmanager
def hold(data_dir: str | Path, what: str) -> Iterator[None]:
    """Hold the run lock for the block. Raises `RunLockHeld` at once if another process (or
    another task here) holds it. Reentrant in the same context."""
    root = Path(os.path.expanduser(str(data_dir)))
    key = str(root / LOCK_NAME)
    mine = _owned.get({})
    if key in mine and mine[key].live:
        yield
        return
    root.mkdir(parents=True, exist_ok=True)
    try:
        # O_NOFOLLOW: a symlink planted at the lock path is refused, not followed.
        fd = os.open(key, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise RunLockHeld(Holder(), _unavailable(e)) from e
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunLockHeld(_read_holder(fd)) from None
        except OSError as e:
            # ENOLCK or a filesystem without flock: fail closed, with a refusal the caller
            # records and reports like a held lock instead of crashing unrecorded.
            raise RunLockHeld(Holder(), _unavailable(e)) from e
        ownership = _Ownership()
        token = _owned.set({**mine, key: ownership})
        try:
            _write_holder(fd, what)
            yield
        finally:
            ownership.live = False
            _owned.reset(token)
            with suppress(OSError):
                os.ftruncate(fd, 0)  # not stale text for the next reader
    finally:
        os.close(fd)  # releases the flock

"""`<output_dir>/.filing.lock`: writers vs. the packet filer (issue #20).

Filing moves a whole entry folder with one rename (`core/packet_filing.py`).
A writer that resolved an entry with `paths.locate` and is about to write into
it must not have that folder renamed out from under it, or its file lands at a
path that no longer exists (or recreates a stray root folder). So:

- writers hold the lock SHARED around locate + write (they never block each
  other), and never across an LLM call: a build or refine is covered for its
  streaming window by its running `build_runs` row, which makes the filer skip
  the entries it can write (`packet_filing`). Async code takes it with
  `shared_async`, which waits in a worker thread so the event loop never stalls
  behind a filing pass;
- filing takes it EXCLUSIVE and NON-BLOCKING: if any writer holds it, the pass
  is skipped and reported "busy" instead of stalling a cockpit request behind a
  minutes-long build.

An `flock` lives exactly as long as the holding process, so a crashed writer
frees it by dying. Unlike `refine_files.lease`, the file is never unlinked: with
shared holders, unlinking would let the next exclusive locker lock a fresh inode
while a reader still holds the old one.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

LOCK_NAME = ".filing.lock"


def _open(output_root: Path) -> int:
    return os.open(output_root / LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)


@contextmanager
def shared(output_root: str | Path) -> Iterator[None]:
    """Hold the lock shared (blocking: filing holds it only for a rename).
    Creates the output dir if needed, which every writer does anyway."""
    root = Path(os.path.expanduser(str(output_root)))
    root.mkdir(parents=True, exist_ok=True)
    fd = _open(root)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)


@asynccontextmanager
async def shared_async(output_root: str | Path) -> AsyncIterator[None]:
    """`shared` for async code: the (brief) wait happens in a worker thread."""
    root = Path(os.path.expanduser(str(output_root)))
    root.mkdir(parents=True, exist_ok=True)
    fd = _open(root)
    try:
        await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)


@contextmanager
def exclusive(output_root: str | Path) -> Iterator[bool]:
    """Try the lock exclusively without waiting; yields False when a writer
    holds it. Never creates the output dir: there is nothing to file there."""
    root = Path(os.path.expanduser(str(output_root)))
    if not root.is_dir():
        yield False
        return
    fd = _open(root)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)

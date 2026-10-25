"""The run lock (#95): one discovery or matching pass at a time, across processes."""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import signal
import time
from pathlib import Path

import pytest

from blastjob.core import run_lock


def _hold_forever(data_dir: str, ready) -> None:  # runs in a child process
    with run_lock.hold(data_dir, "daily run"):
        ready.set()
        time.sleep(60)


def _spawn_holder(data_dir: Path):
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(target=_hold_forever, args=(str(data_dir), ready))
    proc.start()
    assert ready.wait(30), "holder never took the lock"
    return proc


def test_second_process_is_refused_with_the_holders_details(tmp_path):
    proc = _spawn_holder(tmp_path)
    try:
        with pytest.raises(run_lock.RunLockHeld) as info:
            with run_lock.hold(tmp_path, "matching"):
                pass
        assert info.value.holder.pid == proc.pid
        assert info.value.holder.what == "daily run"
        assert str(info.value).startswith("Another run is in progress (daily run, started ")
    finally:
        proc.kill()
        proc.join()


def test_lock_is_freed_when_the_holder_is_sigkilled(tmp_path):
    proc = _spawn_holder(tmp_path)
    os.kill(proc.pid, signal.SIGKILL)
    proc.join()
    with run_lock.hold(tmp_path, "matching"):
        assert run_lock.holder_of(tmp_path) is not None


def test_reentrant_in_one_context(tmp_path):
    with run_lock.hold(tmp_path, "daily run"):
        with run_lock.hold(tmp_path, "discovery"):
            pass
        # leaving the inner block must not have released the outer lock
        assert run_lock.holder_of(tmp_path) is not None
    assert run_lock.holder_of(tmp_path) is None


def test_holder_is_reported_while_held_and_cleared_after(tmp_path):
    with run_lock.hold(tmp_path, "daily run"):
        holder = run_lock.holder_of(tmp_path)
        assert holder is not None and holder.pid == os.getpid()
    assert run_lock.holder_of(tmp_path) is None


def test_task_started_inside_the_lock_shares_it(tmp_path):
    async def child() -> str:
        with run_lock.hold(tmp_path, "matching"):
            return "got it"

    async def main() -> str:
        with run_lock.hold(tmp_path, "discovery"):
            return await asyncio.create_task(child())

    assert asyncio.run(main()) == "got it"


def test_task_started_before_the_lock_contends(tmp_path):
    """A cockpit task that began outside the holder's context is a different run."""

    async def sibling(go: asyncio.Event, done: asyncio.Event) -> str:
        await go.wait()
        try:
            with run_lock.hold(tmp_path, "matching"):
                return "got it"
        except run_lock.RunLockHeld as e:
            return str(e)
        finally:
            done.set()

    async def main() -> str:
        go, done = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(sibling(go, done))
        with run_lock.hold(tmp_path, "discovery"):
            go.set()
            await done.wait()
        return await task

    assert asyncio.run(main()).startswith("Another run is in progress (discovery")


def test_describe_without_details():
    assert run_lock.describe(run_lock.Holder()) == "Another run is in progress"


def test_lock_file_is_not_inherited_by_children(tmp_path):
    with run_lock.hold(tmp_path, "daily run"):
        fd = os.open(tmp_path / run_lock.LOCK_NAME, os.O_RDONLY)
        os.close(fd)
        # every descriptor the lock opened is close-on-exec
        flags = [
            os.get_inheritable(int(n))
            for n in os.listdir("/dev/fd")
            if n.isdigit() and _is_lock_fd(int(n), tmp_path)
        ]
        assert flags and not any(flags)


def _is_lock_fd(n: int, data_dir: Path) -> bool:
    try:
        return os.path.samestat(os.fstat(n), os.stat(data_dir / run_lock.LOCK_NAME))
    except OSError:
        return False


def test_a_task_that_outlives_the_lock_does_not_borrow_it(tmp_path):
    """A task created inside the lock copies its context. Awaited after release, it must
    contend like any other taker, not skip locking because of a stale token."""

    async def later(gate: asyncio.Event) -> str:
        await gate.wait()
        with run_lock.hold(tmp_path, "matching"):
            # a real, held lock: another taker is refused while this block runs
            return str(run_lock.holder_of(tmp_path) is not None)

    async def main() -> str:
        gate = asyncio.Event()
        with run_lock.hold(tmp_path, "daily run"):
            task = asyncio.create_task(later(gate))
        gate.set()
        result = await task
        assert run_lock.holder_of(tmp_path) is None
        return result

    assert asyncio.run(main()) == "True"


def test_a_symlink_at_the_lock_path_is_refused_not_followed(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("keep me")
    (tmp_path / run_lock.LOCK_NAME).symlink_to(target)

    with pytest.raises(run_lock.RunLockHeld, match="could not be taken"):
        with run_lock.hold(tmp_path, "daily run"):
            pass
    assert target.read_text() == "keep me"


def test_a_flock_failure_other_than_contention_fails_closed_with_a_refusal(tmp_path, monkeypatch):
    import errno
    import fcntl

    def no_locks(_fd, _op):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", no_locks)

    with pytest.raises(run_lock.RunLockHeld, match="No locks available"):
        with run_lock.hold(tmp_path, "daily run"):
            pass


def test_holder_text_is_capped_and_stripped_of_control_characters(tmp_path):
    import json as _json

    path = tmp_path / run_lock.LOCK_NAME
    ready = {
        "pid": 1,
        "what": "x" * 500 + "\x1b[31m\nred",
        "started_at": "\x07" + "2026-10-25T07:30:00",
    }

    with run_lock.hold(tmp_path, "daily run"):
        fd = os.open(path, os.O_RDWR)
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, _json.dumps(ready).encode(), 0)
        finally:
            os.close(fd)
        with pytest.raises(run_lock.RunLockHeld) as info:
            _in_other_context(tmp_path)

    assert info.value.holder.what == "x" * run_lock.MAX_HOLDER_TEXT
    assert "\x1b" not in str(info.value) and "\n" not in str(info.value)
    assert info.value.holder.started_at.startswith("2026-10-25")


def _in_other_context(data_dir):
    import contextvars

    def take():
        with run_lock.hold(data_dir, "matching"):
            pass

    contextvars.Context().run(take)

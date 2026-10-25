"""Run one model CLI under a supervisor that guarantees it does not outlive its owner.

    python -m blastjob.llm.supervise --deadline-s N --parent-fd FD -- <argv...>

The supervisor leads its own session. It starts the command in a process group of its own
(so the supervisor is never inside the group it has to wait out), relays stdin to the child
and the child's stdout to its own stdout unchanged, and watches two things besides the
child: the parent-death pipe (inherited read end `FD`; EOF means the owning process died, so
the write end closed) and its own deadline. On either, or on SIGTERM, it sends TERM to the
child's group, waits 2 s, sends KILL, and exits only once `killpg(pgid, 0)` says the group is
gone, so a grandchild cannot outlive the call. A failed kill is logged on stderr. Nothing is
ever matched by name. After a normal child exit it drains the child's stdout to EOF before it
exits, so the terminal `result` line is never lost to a slow reader; only the deadline or the
owner dying cuts that short.

Limit: a descendant that calls setsid or double-forks into another group escapes the group
check. `claude` runs toolless (no shell), so the risk is low. How the caller uses it:
docs/how-it-works/llm.md.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import select
import signal
import subprocess
import sys
import threading
import time

TERM_GRACE_S = 2.0
KILL_WAIT_S = 2.0
POLL_S = 0.1
READ_CHUNK = 65536
EXIT_DEADLINE = 124
EXIT_OWNER_GONE = 143
EXIT_CANNOT_START = 127
EXIT_KILL_FAILED = 70
JOIN_S = 2.0
DRAIN_POLL_S = 0.1

_stop_requested = threading.Event()


def _log(message: str) -> None:
    sys.stderr.write(f"blastjob.llm.supervise: {message}\n")
    sys.stderr.flush()


def _group_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, sig)


def _wait_gone(child: subprocess.Popen[bytes], pgid: int, seconds: float) -> bool:
    """Poll until the group is empty. `child.poll()` reaps the leader so it is not a zombie."""
    end = time.monotonic() + seconds
    while True:
        child.poll()
        if _group_gone(pgid):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.05)


def stop_group(child: subprocess.Popen[bytes], pgid: int) -> bool:
    """TERM the child's group, wait, KILL, and report whether the group is gone."""
    _signal_group(pgid, signal.SIGTERM)
    if _wait_gone(child, pgid, TERM_GRACE_S):
        return True
    _signal_group(pgid, signal.SIGKILL)
    if _wait_gone(child, pgid, KILL_WAIT_S):
        return True
    _log(f"could not kill process group {pgid}; it may still be running")
    return False


def _pump_in(child: subprocess.Popen[bytes]) -> None:
    assert child.stdin is not None
    try:
        while True:
            data = os.read(0, READ_CHUNK)
            if not data:
                break
            child.stdin.write(data)
            child.stdin.flush()
    except OSError:
        pass  # the child closed its stdin, or ours is unreadable
    finally:
        with contextlib.suppress(OSError):
            child.stdin.close()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _pump_out(child: subprocess.Popen[bytes]) -> None:
    assert child.stdout is not None
    fd = child.stdout.fileno()
    try:
        while True:
            data = os.read(fd, READ_CHUNK)
            if not data:
                break
            _write_all(1, data)
    except OSError:
        pass  # our stdout closed: the owner is gone


def _owner_gone(parent_fd: int) -> bool:
    ready, _, _ = select.select([parent_fd], [], [], POLL_S)
    if not ready:
        return False
    try:
        return os.read(parent_fd, 1) == b""
    except OSError:
        return True


def _exit_code(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 + -returncode


def _watch(child: subprocess.Popen[bytes], parent_fd: int, deadline: float) -> int | None:
    """Return a forced exit code when the owner died or time ran out, else None once the
    child has exited on its own."""
    while child.poll() is None:
        if _stop_requested.is_set():
            return EXIT_OWNER_GONE
        if _owner_gone(parent_fd):
            return EXIT_OWNER_GONE
        if time.monotonic() >= deadline:
            return EXIT_DEADLINE
    return None


def _drain(out_thread: threading.Thread, parent_fd: int, deadline: float) -> int | None:
    """Wait for the relay to finish writing the child's stdout. Returns a forced exit code if
    the deadline passes or the owner dies first, else None."""
    while out_thread.is_alive():
        out_thread.join(DRAIN_POLL_S)
        if _stop_requested.is_set() or _owner_gone(parent_fd):
            return EXIT_OWNER_GONE
        if time.monotonic() >= deadline:
            return EXIT_DEADLINE
    return None


def _positive(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="blastjob.llm.supervise")
    parser.add_argument("--deadline-s", type=_positive, required=True)
    parser.add_argument("--parent-fd", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    args.command = command
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    deadline = time.monotonic() + args.deadline_s
    if os.getpgrp() != os.getpid():
        with contextlib.suppress(OSError):
            os.setsid()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: _stop_requested.set())
    try:
        os.fstat(args.parent_fd)
        child = subprocess.Popen(
            args.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        _log(f"could not start {args.command[0]}: {e}")
        return EXIT_CANNOT_START
    pgid = child.pid
    threading.Thread(target=_pump_in, args=(child,), daemon=True).start()
    out_thread = threading.Thread(target=_pump_out, args=(child,), daemon=True)
    out_thread.start()

    forced = _watch(child, args.parent_fd, deadline)
    gone = stop_group(child, pgid)  # also reaps any grandchild the leader left behind
    if forced is None:
        forced = _drain(out_thread, args.parent_fd, deadline)
    else:
        out_thread.join(JOIN_S)
    if not gone:
        return EXIT_KILL_FAILED
    if forced is not None:
        return forced
    return _exit_code(child.returncode)


if __name__ == "__main__":
    sys.exit(main())

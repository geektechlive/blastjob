"""Real-subprocess tests for `llm/supervise.py` (docs/how-it-works/llm.md).

`sleep` and tiny python scripts written to `tmp_path` stand in for `claude`: no real
model binary, no network. Each test checks that no process outlives its owner."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SUPERVISE = [sys.executable, "-m", "blastjob.llm.supervise"]
WAIT_S = 12.0

# Writes "<own pid> <grandchild pid>" to argv[1], then either exits at once or sleeps.
_TREE_SCRIPT = """
import os, subprocess, sys, time
grandchild = subprocess.Popen(["sleep", "60"])
with open(sys.argv[1], "w") as fh:
    fh.write(f"{os.getpid()} {grandchild.pid}")
if sys.argv[2] == "exit":
    raise SystemExit(0)
time.sleep(60)
"""

_ECHO_SCRIPT = """
import sys
data = sys.stdin.read()
sys.stdout.write(data.upper())
sys.stdout.flush()
raise SystemExit(3)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_until(predicate, timeout: float = WAIT_S) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _start(argv: list[str], deadline_s: int = 60) -> tuple[subprocess.Popen[bytes], int]:
    """Start the supervisor as the cockpit does; return it and the parent-death write fd."""
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        [*SUPERVISE, "--deadline-s", str(deadline_s), "--parent-fd", str(read_fd), "--", *argv],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        pass_fds=(read_fd,),
        start_new_session=True,
    )
    os.close(read_fd)
    return proc, write_fd


def _tree(tmp_path: Path, mode: str) -> tuple[list[str], Path]:
    script = tmp_path / "tree.py"
    script.write_text(_TREE_SCRIPT)
    pidfile = tmp_path / "pids.txt"
    return [sys.executable, str(script), str(pidfile), mode], pidfile


def _pids(pidfile: Path) -> tuple[int, int]:
    assert _wait_until(lambda: pidfile.exists() and pidfile.read_text().count(" ") == 1)
    child, grandchild = pidfile.read_text().split()
    return int(child), int(grandchild)


def test_parent_death_kills_the_whole_group(tmp_path):
    argv, pidfile = _tree(tmp_path, "sleep")
    proc, write_fd = _start(argv)
    child, grandchild = _pids(pidfile)
    assert _alive(child) and _alive(grandchild)

    os.close(write_fd)  # the parent died: the pipe hits EOF

    assert _wait_until(lambda: proc.poll() is not None), "supervisor did not exit"
    assert _wait_until(lambda: not _alive(child) and not _alive(grandchild))


def test_the_supervisors_deadline_kills_the_whole_group(tmp_path):
    argv, pidfile = _tree(tmp_path, "sleep")
    proc, write_fd = _start(argv, deadline_s=1)
    try:
        child, grandchild = _pids(pidfile)
        assert _wait_until(lambda: proc.poll() is not None), "supervisor did not exit"
        assert proc.returncode != 0
        assert _wait_until(lambda: not _alive(child) and not _alive(grandchild))
    finally:
        os.close(write_fd)


def test_a_grandchild_dies_when_the_leader_child_exits_first(tmp_path):
    argv, pidfile = _tree(tmp_path, "exit")
    proc, write_fd = _start(argv)
    try:
        child, grandchild = _pids(pidfile)
        assert _wait_until(lambda: proc.poll() is not None), "supervisor did not exit"
        assert proc.returncode == 0  # the child's own code
        assert _wait_until(lambda: not _alive(grandchild))
    finally:
        os.close(write_fd)


def test_stdin_and_stdout_relay_and_the_childs_exit_code_is_mirrored(tmp_path):
    script = tmp_path / "echo.py"
    script.write_text(_ECHO_SCRIPT)
    proc, write_fd = _start([sys.executable, str(script)])
    try:
        out, _ = proc.communicate(b"hello \xc3\xa9 world\n", timeout=WAIT_S)
    finally:
        os.close(write_fd)
    assert out == b"HELLO \xc3\x89 WORLD\n"
    assert proc.returncode == 3


def test_sigterm_to_the_supervisor_cleans_up_the_group(tmp_path):
    argv, pidfile = _tree(tmp_path, "sleep")
    proc, write_fd = _start(argv)
    try:
        child, grandchild = _pids(pidfile)
        os.killpg(proc.pid, 15)
        assert _wait_until(lambda: proc.poll() is not None)
        assert _wait_until(lambda: not _alive(child) and not _alive(grandchild))
    finally:
        os.close(write_fd)


def test_a_missing_binary_exits_nonzero(tmp_path):
    proc, write_fd = _start(["/nonexistent/claude-stand-in"])
    try:
        proc.communicate(b"", timeout=WAIT_S)
    finally:
        os.close(write_fd)
    assert proc.returncode not in (0, None)


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_a_non_positive_deadline_is_rejected(bad):
    out = subprocess.run(
        [*SUPERVISE, "--deadline-s", bad, "--parent-fd", "0", "--", "true"],
        capture_output=True,
        timeout=WAIT_S,
    )
    assert out.returncode != 0

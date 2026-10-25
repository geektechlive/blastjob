"""The model gateway: deadline, origin tag, SDK client limits and the supervised CLI path.

The supervised-CLI tests spawn the real supervisor with a python stand-in for `claude`
(no real binary, no network)."""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from blastjob.llm import gateway, providers

_FAKE_CLAUDE = """
import json, sys
sys.stdin.read()
for text in ("Hello ", "world"):
    print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
          "delta": {"type": "text_delta", "text": text}}}), flush=True)
print(json.dumps({"type": "result", "usage": {"input_tokens": 3, "output_tokens": 2}}), flush=True)
"""

_HANGING_CLAUDE = """
import os, subprocess, sys, time
grandchild = subprocess.Popen(["sleep", "60"])
with open(sys.argv[1], "w") as fh:
    fh.write(f"{os.getpid()} {grandchild.pid}")
sys.stdin.read()
time.sleep(60)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# --- deadline -----------------------------------------------------------------


def test_with_no_deadline_the_per_call_timeout_is_unchanged():
    assert gateway.remaining() is None
    assert gateway.call_limit(900.0) == 900.0


def test_a_deadline_caps_the_per_call_timeout_at_what_is_left():
    with gateway.deadline_scope(100.0):
        limit = gateway.call_limit(900.0)
    assert 95.0 < limit <= 100.0
    with gateway.deadline_scope(100.0):
        assert gateway.call_limit(10.0) == 10.0  # a short per-call limit still wins


def test_a_call_is_refused_with_under_thirty_seconds_left():
    with gateway.deadline_scope(gateway.MIN_START_SECONDS - 1):
        with pytest.raises(gateway.DeadlineReached):
            gateway.call_limit(900.0)


def test_deadline_reached_is_a_cli_error_so_fail_soft_callers_handle_it():
    assert issubclass(gateway.DeadlineReached, providers.CLIError)
    assert providers.CLIError is gateway.CLIError


def test_a_nested_scope_can_only_tighten_and_the_outer_one_is_restored():
    with gateway.deadline_scope(200.0):
        outer = gateway.remaining()
        with gateway.deadline_scope(1000.0):
            assert gateway.remaining() <= outer
        with gateway.deadline_scope(60.0):
            assert gateway.remaining() <= 60.0
        assert gateway.remaining() > 100.0
    assert gateway.remaining() is None


def test_origin_defaults_to_clicked_and_a_scope_tags_scheduled():
    assert gateway.current_origin() == "clicked"
    with gateway.origin_scope("scheduled"):
        assert gateway.current_origin() == "scheduled"
    assert gateway.current_origin() == "clicked"


async def test_make_stream_refuses_to_start_inside_a_spent_deadline(monkeypatch):
    from blastjob.models.config import BlastJobConfig

    monkeypatch.setattr(providers, "detect_provider", lambda _cfg: "claude-cli")
    with gateway.deadline_scope(5.0), pytest.raises(gateway.DeadlineReached):
        await providers.make_stream(BlastJobConfig(), [], [])


# --- SDK clients --------------------------------------------------------------


def test_the_anthropic_client_does_not_retry_and_has_a_bounded_timeout():
    client = gateway.anthropic_client("test-key")
    assert client.max_retries == 0
    assert client.timeout == gateway.SDK_TIMEOUT_SECONDS


def test_the_sdk_timeout_is_capped_by_the_remaining_deadline():
    with gateway.deadline_scope(60.0):
        client = gateway.anthropic_client("test-key")
    assert client.timeout <= 60.0


def test_the_openai_client_does_not_retry(monkeypatch):
    pytest.importorskip("openai")
    client = gateway.openai_client("test-key")
    assert client.max_retries == 0


# --- supervised CLI -----------------------------------------------------------


async def test_run_cli_streams_through_the_real_supervisor(tmp_path):
    script = tmp_path / "fake_claude.py"
    script.write_text(_FAKE_CLAUDE)
    box = providers._CLIUsageBox()

    chunks = [c async for c in providers._run_cli([sys.executable, str(script)], "hi", box)]

    assert "".join(chunks) == "Hello world"
    assert box.usage is not None and box.usage.input_tokens == 3


async def test_cancelling_a_supervised_call_kills_the_whole_group(tmp_path):
    script = tmp_path / "hang.py"
    script.write_text(_HANGING_CLAUDE)
    pidfile = tmp_path / "pids.txt"
    box = providers._CLIUsageBox()

    async def drain() -> None:
        async for _ in providers._run_cli([sys.executable, str(script), str(pidfile)], "x", box):
            pass

    task = asyncio.create_task(drain())
    end = time.monotonic() + 10
    while not (pidfile.exists() and " " in pidfile.read_text()) and time.monotonic() < end:
        await asyncio.sleep(0.05)
    child, grandchild = (int(p) for p in pidfile.read_text().split())
    assert _alive(child) and _alive(grandchild)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    end = time.monotonic() + 10
    while (_alive(child) or _alive(grandchild)) and time.monotonic() < end:
        await asyncio.sleep(0.05)
    assert not _alive(child) and not _alive(grandchild)


async def test_run_cli_refuses_to_spawn_with_under_thirty_seconds_left(monkeypatch):
    async def never(*_a, **_k):
        raise AssertionError("spawned a child past the deadline")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", never)
    with gateway.deadline_scope(5.0), pytest.raises(gateway.DeadlineReached):
        await anext(providers._run_cli(["claude"], "x", providers._CLIUsageBox()))


async def test_the_cli_is_launched_through_the_supervisor(monkeypatch):
    seen: dict = {}

    async def fake_exec(*args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        raise OSError("stop here")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(providers.CLIError):
        await anext(providers._run_cli(["claude", "-p"], "x", providers._CLIUsageBox()))

    args = seen["args"]
    assert args[:4] == (sys.executable, "-I", "-m", "blastjob.llm.supervise")
    assert args[args.index("--") + 1 :] == ("claude", "-p")
    assert "--parent-fd" in args and "--deadline-s" in args
    assert seen["kwargs"]["start_new_session"] is True
    assert len(seen["kwargs"]["pass_fds"]) == 1


# --- BoundedStream ------------------------------------------------------------


class _Live:
    def __init__(self, hang: bool = False):
        self._hang = hang

    @property
    def text_stream(self):
        return self._gen()

    async def _gen(self):
        yield "a"
        if self._hang:
            await asyncio.Event().wait()
        yield "b"

    async def get_final_message(self):
        return "final"


class _Manager:
    """Shaped like the Anthropic SDK: entering returns a different, live stream object."""

    def __init__(self, live: _Live):
        self._live = live
        self.exited = False

    async def __aenter__(self):
        return self._live

    async def __aexit__(self, *_a):
        self.exited = True


async def test_bounded_stream_delegates_to_the_live_stream_and_finishes():
    inner = _Manager(_Live())
    stream = gateway.BoundedStream(inner, 5.0, gateway.reserve("t"))
    async with stream:
        text = [c async for c in stream.text_stream]
        final = await stream.get_final_message()
    assert (text, final, inner.exited) == (["a", "b"], "final", True)


async def test_bounded_stream_times_out_a_stalled_stream_as_a_cli_error():
    stream = gateway.BoundedStream(_Manager(_Live(hang=True)), 0.1, gateway.reserve("t"))
    with pytest.raises(gateway.CLIError, match="timed out"):
        async with stream:
            async for _ in stream.text_stream:
                pass


def test_scope_records_that_a_call_was_refused_for_the_deadline():
    with gateway.deadline_scope(gateway.MIN_START_SECONDS - 1) as state:
        assert not state.hit
        with pytest.raises(gateway.DeadlineReached):
            gateway.call_limit(900.0)
        assert state.hit
        assert state.hit_at  # the real cutoff time, for an honest status line


def test_deadline_reached_is_true_when_too_little_time_is_left_even_with_no_refused_call():
    assert gateway.deadline_reached() is False  # no scope, no deadline
    with gateway.deadline_scope(100.0):
        assert gateway.deadline_reached() is False
    with gateway.deadline_scope(gateway.MIN_START_SECONDS - 1):
        assert gateway.deadline_reached() is True


def test_nested_scope_shares_the_outer_state():
    with gateway.deadline_scope(100.0) as outer:
        with gateway.deadline_scope(gateway.MIN_START_SECONDS - 1) as inner:
            with pytest.raises(gateway.DeadlineReached):
                gateway.call_limit(10.0)
        assert inner is outer and outer.hit

"""Gateway hardening: isolated supervisor, reap timing, spawn cancellation, SDK errors."""

from __future__ import annotations

import asyncio
import os
import sys

import anthropic
import httpx
import pytest

from blastjob.llm import gateway, providers
from blastjob.llm.cost import CostTracker
from blastjob.models.config import BlastJobConfig
from tests.test_llm_gateway import _Live, _Manager


def _api_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://example.com"))


# --- isolated supervisor ------------------------------------------------------


async def test_the_supervisor_ignores_modules_planted_in_the_cwd_and_pythonpath(tmp_path):
    for name in ("argparse.py", "blastjob.py"):
        (tmp_path / name).write_text("raise RuntimeError('planted module imported')\n")
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    child = await gateway.spawn_cli(["/bin/echo", "ok"], cwd=str(tmp_path), env=env, limit=30.0)
    try:
        assert child.proc.stdout is not None
        out = await asyncio.wait_for(child.proc.stdout.read(), 10)
        code = await asyncio.wait_for(child.proc.wait(), 10)
    finally:
        await gateway.reap(child)

    assert (out, code) == (b"ok\n", 0)


# --- reap and spawn cancellation ----------------------------------------------

_STUBBORN = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(60)
"""


async def test_reap_lets_the_supervisor_finish_its_own_term_then_kill(tmp_path):
    script = tmp_path / "stubborn.py"
    script.write_text(_STUBBORN)
    child = await gateway.spawn_cli(
        [sys.executable, str(script)], cwd=str(tmp_path), env=dict(os.environ), limit=60.0
    )
    await asyncio.sleep(0.5)

    await gateway.reap(child)

    assert child.proc.returncode == 143  # the supervisor's clean exit, not a SIGKILL (-9)


async def test_cancelling_during_spawn_closes_the_parent_death_pipe(monkeypatch):
    pipes: list[tuple[int, int]] = []
    real_pipe = os.pipe

    def recording_pipe() -> tuple[int, int]:
        fds = real_pipe()
        pipes.append(fds)
        return fds

    async def stuck(*_a, **_k):
        await asyncio.Event().wait()

    monkeypatch.setattr(os, "pipe", recording_pipe)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", stuck)
    task = asyncio.create_task(gateway.spawn_cli(["claude"], cwd="/", env={}, limit=30.0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for fd in pipes[0]:
        with pytest.raises(OSError):
            os.fstat(fd)


# --- SDK errors and client closing --------------------------------------------


def test_sdk_errors_become_cli_errors():
    with pytest.raises(gateway.CLIError, match="model API"), gateway.sdk_errors():
        raise _api_error()


async def test_bounded_stream_maps_an_sdk_error_mid_stream():
    class _Boom(_Live):
        async def _gen(self):
            yield "a"
            raise _api_error()

    stream = gateway.BoundedStream(_Manager(_Boom()), 5.0, gateway.reserve("t"))
    with pytest.raises(gateway.CLIError):
        async with stream:
            async for _ in stream.text_stream:
                pass


class _FakeMessages:
    def __init__(self, error: Exception):
        self._error = error

    async def create(self, **_kw):
        raise self._error


class _FakeClient:
    def __init__(self, error: Exception):
        self.messages = _FakeMessages(error)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def test_a_failed_research_call_is_fail_soft_matched_and_closes_the_client(monkeypatch):
    outcomes: list[str] = []
    client = _FakeClient(_api_error())
    monkeypatch.setattr(providers, "detect_provider", lambda _cfg: "anthropic")
    monkeypatch.setattr(gateway, "anthropic_client", lambda _key: client)
    monkeypatch.setattr(gateway, "finish", lambda _res, outcome: outcomes.append(outcome))

    result = await providers.make_research_call(BlastJobConfig(), "Acme", CostTracker())

    assert result == ""
    assert outcomes == ["error"]
    assert client.closed


async def test_a_client_that_cannot_be_built_still_matches_reserve_with_finish(monkeypatch):
    outcomes: list[str] = []

    def broken(_key):
        raise ValueError("bad key")

    monkeypatch.setattr(providers, "detect_provider", lambda _cfg: "anthropic")
    monkeypatch.setattr(gateway, "anthropic_client", broken)
    monkeypatch.setattr(gateway, "finish", lambda _res, outcome: outcomes.append(outcome))

    with pytest.raises(ValueError):
        await providers.make_research_call(BlastJobConfig(), "Acme", CostTracker())
    assert outcomes == ["error"]

"""Round 2, R10: a missing `claude` binary or a pipe that breaks while the prompt is
written surfaces as CLIError, the one failure the fail-soft callers catch."""

from __future__ import annotations

import asyncio

import pytest

from blastjob.llm import providers
from tests.test_e_cli import _collect, _delta, _install, _Proc, _result


async def test_a_missing_claude_binary_is_a_cli_error(monkeypatch):
    async def missing(*_a, **_kw):
        raise FileNotFoundError(2, "No such file or directory", "claude")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)

    with pytest.raises(providers.CLIError, match="claude"):
        await _collect()


async def test_a_spawn_permission_error_is_a_cli_error(monkeypatch):
    async def denied(*_a, **_kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", denied)

    with pytest.raises(providers.CLIError):
        await _collect()


async def test_a_broken_pipe_while_sending_the_prompt_is_a_cli_error_and_reaps_the_child(
    monkeypatch,
):
    proc = _Proc([_delta("x"), _result()])
    proc.returncode = None  # still running, so reaping must kill it

    async def broken(*_a, **_kw):
        raise BrokenPipeError(32, "Broken pipe")

    proc.stdin.drain = broken
    _install(monkeypatch, proc)

    with pytest.raises(providers.CLIError, match="prompt"):
        await _collect()
    assert proc.killed


async def test_a_write_failure_is_a_cli_error(monkeypatch):
    proc = _Proc([_result()])

    def broken(_data):
        raise BrokenPipeError(32, "Broken pipe")

    proc.stdin.write = broken
    _install(monkeypatch, proc)

    with pytest.raises(providers.CLIError):
        await _collect()

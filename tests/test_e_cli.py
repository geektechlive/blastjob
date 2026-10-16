"""E4/E5: the claude-cli reader decodes UTF-8 incrementally, fails loudly, and
never leaves a child process or a predictable working directory behind."""

from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest

from blastjob.llm import providers


def _delta(text: str) -> bytes:
    return (
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": text},
                },
            },
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def _result(**extra) -> bytes:
    payload = {"type": "result", "usage": {"input_tokens": 1, "output_tokens": 1}, **extra}
    return (json.dumps(payload) + "\n").encode("utf-8")


class _Stdout:
    def __init__(self, chunks: list[bytes], hang: bool = False):
        self._chunks = list(chunks)
        self._hang = hang

    async def read(self, _n: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        if self._hang:
            await asyncio.Event().wait()  # a CLI that never answers
        return b""


class _Stdin:
    def write(self, _data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Proc:
    def __init__(self, chunks: list[bytes], *, code: int = 0, hang: bool = False):
        self.stdin = _Stdin()
        self.stdout = _Stdout(chunks, hang)
        self.returncode: int | None = None if hang else code
        self._code = code
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = self._code
        return self.returncode


def _install(monkeypatch, proc: _Proc) -> dict:
    seen: dict = {}

    async def fake_exec(*args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return seen


async def _collect(box=None) -> str:
    box = box or providers._CLIUsageBox()
    stream = await providers.claudecli_stream("sys", "prompt", box)
    return "".join([chunk async for chunk in stream])


async def test_multibyte_text_split_across_chunks_is_not_corrupted(monkeypatch):
    line = _delta("naïve — café 日本")
    cut = line.index("—".encode()) + 1  # inside the 3-byte em dash
    _install(monkeypatch, _Proc([line[:cut], line[cut:], _result()]))
    assert await _collect() == "naïve — café 日本"


async def test_nonzero_exit_raises(monkeypatch):
    _install(monkeypatch, _Proc([_delta("partial"), _result()], code=1))
    with pytest.raises(providers.CLIError, match="exit"):
        await _collect()


async def test_result_marked_is_error_raises(monkeypatch):
    _install(monkeypatch, _Proc([_result(is_error=True, result="Credit balance is too low")]))
    with pytest.raises(providers.CLIError, match="Credit balance"):
        await _collect()


async def test_a_hung_cli_times_out_and_is_killed(monkeypatch):
    proc = _Proc([], hang=True)
    _install(monkeypatch, proc)
    monkeypatch.setattr(providers, "CLI_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(providers.CLIError, match="timed out"):
        await _collect()
    assert proc.killed


async def test_closing_the_stream_early_kills_the_child(monkeypatch):
    proc = _Proc([_delta("one")], hang=True)
    _install(monkeypatch, proc)
    stream = await providers.claudecli_stream("sys", "prompt", providers._CLIUsageBox())
    assert await stream.__anext__() == "one"
    await stream.aclose()
    assert proc.killed


async def test_cancelling_the_reader_kills_the_child(monkeypatch):
    proc = _Proc([], hang=True)
    _install(monkeypatch, proc)

    async def read_all():
        await _collect()

    task = asyncio.create_task(read_all())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed


async def test_cli_stream_context_exit_closes_the_reader(monkeypatch):
    proc = _Proc([_delta("one")], hang=True)
    _install(monkeypatch, proc)
    async with providers._CLIStream("sys", "prompt") as stream:
        it = stream.text_stream
        assert await it.__anext__() == "one"
    assert proc.killed


async def test_cwd_is_a_private_per_user_directory(monkeypatch):
    seen = _install(monkeypatch, _Proc([_result()]))
    await _collect()
    cwd = seen["kwargs"]["cwd"]
    assert os.path.isdir(cwd)
    assert os.path.basename(cwd) != "blastjob-cli-cwd"  # not the old fixed, guessable path
    info = os.stat(cwd)
    assert stat.S_IMODE(info.st_mode) == 0o700
    assert info.st_uid == os.getuid()


async def test_isolation_flags_are_unchanged(monkeypatch):
    seen = _install(monkeypatch, _Proc([_result()]))
    await _collect()
    args = list(seen["args"])
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in args
    assert "--mcp-config" not in args


async def test_research_degrades_to_no_research_when_the_cli_fails(monkeypatch):
    from blastjob.llm.cost import CostTracker
    from blastjob.models.config import BlastJobConfig

    _install(monkeypatch, _Proc([_result()], code=1))
    monkeypatch.setattr(providers, "detect_provider", lambda _cfg: "claude-cli")
    out = await providers.make_research_call(BlastJobConfig(), "Acme", CostTracker())
    assert out == ""  # the build calls this unguarded; it must not abort

"""The `claude -p` subprocess must reach no tools at all.

It is fed job postings, alert emails and research text, all attacker-
controllable. Denying only the built-in tools left every MCP server from the
user's Claude config loaded (measured: 334 tools across 41 servers, including
mail send, chat send, network-device and GitHub writes, some pre-allowed in the
user's settings), so an injected instruction in a posting could act outside
the app. Draft-only must hold for the model too.
"""

from __future__ import annotations

import asyncio
import os

from blastjob.llm import providers


class _FakeProc:
    def __init__(self) -> None:
        self.stdin = self
        self.stdout = self
        self.returncode = 0

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def read(self, n: int = -1) -> bytes:
        return b""

    async def wait(self) -> int:
        return 0


def _argv(monkeypatch) -> list[str]:
    seen: list[str] = []

    async def fake_exec(*args, **kwargs):
        seen.extend(args)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def drain() -> None:
        stream = await providers.claudecli_stream("sys", "prompt", providers._CLIUsageBox())
        async for _ in stream:
            pass

    asyncio.run(drain())
    return seen


def _value_after(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def test_cli_loads_no_mcp_servers(monkeypatch):
    argv = _argv(monkeypatch)
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" not in argv


def test_cli_enables_no_builtin_tools(monkeypatch):
    assert _value_after(_argv(monkeypatch), "--tools") == ""


def test_cli_ignores_user_and_project_settings(monkeypatch):
    # User settings carry permission allowlists and hooks; neither belongs here.
    assert _value_after(_argv(monkeypatch), "--setting-sources") == ""


def test_cli_child_env_omits_blastjob_secrets(monkeypatch, tmp_path):
    """The child sees no secrets-file key and no API key, but PATH/HOME to log in."""
    from blastjob import secrets as secrets_mod

    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("DISCORD_BOT_TOKEN=tok-secret\n")
    monkeypatch.setattr(secrets_mod, "_configured", secrets_file)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-secret")
    seen: dict = {}

    async def fake_exec(*args, **kwargs):
        seen.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def drain() -> None:
        stream = await providers.claudecli_stream("sys", "prompt", providers._CLIUsageBox())
        async for _ in stream:
            pass

    asyncio.run(drain())
    env = seen["env"]
    assert "DISCORD_BOT_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == os.environ["PATH"] and env["HOME"] == os.environ["HOME"]

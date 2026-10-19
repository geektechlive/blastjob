"""Child processes never inherit blastjob's own secrets."""

from __future__ import annotations

from pathlib import Path

import pytest

from blastjob import secrets as secrets_mod
from blastjob.llm.child_env import child_env


@pytest.fixture
def secrets_file(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "secrets.env"
    monkeypatch.setattr(secrets_mod, "_configured", path)
    return path


def test_removes_keys_named_in_secrets_file_even_if_already_in_environ(secrets_file, monkeypatch):
    secrets_file.write_text("# c\nDISCORD_BOT_TOKEN=a\nIMAP_PW = 'b'\n")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "shell-value")  # set before any load
    monkeypatch.setenv("IMAP_PW", "x")
    monkeypatch.setenv("UNRELATED", "keep")

    env = child_env()

    assert "DISCORD_BOT_TOKEN" not in env and "IMAP_PW" not in env
    assert env["UNRELATED"] == "keep"


def test_anthropic_api_key_always_removed(secrets_file, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert "ANTHROPIC_API_KEY" not in child_env()


def test_missing_secrets_file_removes_only_anthropic_key(secrets_file, monkeypatch):
    assert not secrets_file.exists()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    expected = {k: v for k, v in __import__("os").environ.items() if k != "ANTHROPIC_API_KEY"}
    assert child_env() == expected


def test_other_users_environment_passes_through(secrets_file, monkeypatch):
    secrets_file.write_text("DISCORD_BOT_TOKEN=a\n")
    for k in ("HTTPS_PROXY", "SSL_CERT_FILE", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.setenv(k, "v")
    env = child_env()
    for k in ("HTTPS_PROXY", "SSL_CERT_FILE", "CLAUDE_CODE_OAUTH_TOKEN", "PATH", "HOME"):
        assert k in env


def test_returns_a_copy(secrets_file):
    child_env()["PATH"] = "mutated"
    assert child_env()["PATH"] != "mutated"

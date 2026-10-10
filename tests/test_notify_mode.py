"""`notify.mode`: only `discord` needs Discord credentials."""

from __future__ import annotations

import asyncio

import pytest

from blastjob import daily
from blastjob.models.config import BlastJobConfig
from blastjob.notify import discord


def _cfg(mode: str) -> BlastJobConfig:
    cfg = BlastJobConfig()
    cfg.notify.mode = mode  # type: ignore[assignment]
    return cfg


def test_stdout_mode_prints_and_needs_no_credentials(monkeypatch, capsys):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    send = daily.sender_for(_cfg("stdout"), dry_run=False)
    asyncio.run(send("digest text"))
    assert "digest text" in capsys.readouterr().out


def test_off_mode_and_dry_runs_send_nothing(capsys):
    for send in (
        daily.sender_for(_cfg("off"), dry_run=False),
        daily.sender_for(_cfg("discord"), dry_run=True),
    ):
        asyncio.run(send("digest text"))
    assert capsys.readouterr().out == ""


def test_discord_mode_without_credentials_fails_loudly(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_CHANNEL_ID", raising=False)
    with pytest.raises(discord.DiscordConfigError):
        daily.sender_for(_cfg("discord"), dry_run=False)


def test_discord_credentials_read_the_configured_env_names(monkeypatch):
    cfg = _cfg("discord")
    cfg.notify.discord_token_env = "MY_BOT"
    cfg.notify.discord_channel_env = "MY_CHAN"
    monkeypatch.setenv("MY_BOT", "t")
    monkeypatch.setenv("MY_CHAN", "c")
    assert discord.credentials(token_env="MY_BOT", channel_env="MY_CHAN") == ("t", "c")
    assert callable(daily.sender_for(cfg, dry_run=False))


def test_default_mode_is_stdout():
    assert BlastJobConfig().notify.mode == "stdout"

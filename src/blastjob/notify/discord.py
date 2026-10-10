"""Post a message to a private Discord channel.

Plain REST (`POST /channels/{id}/messages` with a bot token) — no gateway, no
discord.py. The same bot + channel is shared with another of the user's
projects for a separate approval workflow; blastjob messages are
distinguished by their header line, not a separate channel (the user's call,
2026-09-04). Secrets come from the environment
(`DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`), autoloaded from
the secrets file (`paths.secrets_file`) by `blastjob.secrets`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

import httpx

from blastjob.discovery.http import request_json

API_BASE = "https://discord.com/api/v10"
# Discord caps content at 2000 chars; leave headroom for a continuation marker.
MAX_MESSAGE_CHARS = 1900
TOKEN_ENV = "DISCORD_BOT_TOKEN"
CHANNEL_ENV = "DISCORD_CHANNEL_ID"


class DiscordConfigError(RuntimeError):
    """Bot token or channel id missing from the environment."""


def credentials(
    env: Mapping[str, str] | None = None,
    *,
    token_env: str = TOKEN_ENV,
    channel_env: str = CHANNEL_ENV,
) -> tuple[str, str]:
    """(token, channel id) from the environment variables NAMED by the
    `notify.discord_*_env` settings."""
    e = os.environ if env is None else env
    token = (e.get(token_env) or "").strip()
    channel = (e.get(channel_env) or "").strip()
    if not token or not channel:
        raise DiscordConfigError(
            f"{token_env} and {channel_env} must be set (see your secrets file)"
        )
    return token, channel


def chunk_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split on line boundaries so no chunk exceeds `limit`. A single line
    longer than the limit is hard-cut (never happens with digest lines)."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        extra = len(line) + (1 if current else 0)
        if size + extra > limit and current:
            chunks.append("\n".join(current))
            current, size = [], 0
            extra = len(line)
        current.append(line)
        size += extra
    if current:
        chunks.append("\n".join(current))
    return [c for c in chunks if c.strip()]


async def send_message(
    text: str,
    *,
    token: str,
    channel_id: str,
    timeout_s: int = 15,
    max_retries: int = 3,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    """Post `text` (split into <=2000-char messages) and return the Discord
    message ids. Raises on any non-2xx so callers never stamp "notified" for a
    message Discord did not accept."""
    ids: list[str] = []
    async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
        for chunk in chunk_message(text):
            payload = await request_json(
                client,
                "POST",
                f"{API_BASE}/channels/{channel_id}/messages",
                json={"content": chunk, "allowed_mentions": {"parse": []}},
                headers={"Authorization": f"Bot {token}"},
                max_retries=max_retries,
            )
            ids.append(str(payload.get("id", "")))
    return ids

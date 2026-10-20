"""request_json honours Retry-After (header or Discord's JSON body) on a 429."""

from __future__ import annotations

import httpx
import pytest

from blastjob.discovery import http


def _client(responses: list[httpx.Response]) -> httpx.AsyncClient:
    queue = list(responses)
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: queue.pop(0)))


async def _run(responses: list[httpx.Response]) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    async with _client(responses) as client:
        await http.request_json(client, "GET", "https://example.com/x", sleep=fake_sleep)
    return slept


OK = httpx.Response(200, json={"ok": True})


async def test_429_sleeps_for_retry_after_header():
    slept = await _run([httpx.Response(429, headers={"Retry-After": "7"}), OK])
    assert slept == [7.0]


async def test_429_sleeps_for_discord_json_retry_after():
    body = {"message": "rate limited", "retry_after": 1.5, "global": False}
    slept = await _run([httpx.Response(429, json=body), OK])
    assert slept == [1.5]


async def test_retry_after_is_capped():
    slept = await _run([httpx.Response(429, headers={"Retry-After": "3600"}), OK])
    assert slept == [http.MAX_RETRY_AFTER_S]


@pytest.mark.parametrize("value", ["soon", "-3", "Wed, 21 Oct 2026 07:28:00 GMT"])
async def test_unusable_retry_after_falls_back_to_backoff(value):
    slept = await _run([httpx.Response(429, headers={"Retry-After": value}), OK])
    assert slept == [1]


async def test_429_without_hint_keeps_linear_backoff():
    slept = await _run([httpx.Response(429), httpx.Response(503), OK])
    assert slept == [1, 2]


async def test_retry_after_ignored_on_non_429():
    slept = await _run([httpx.Response(503, headers={"Retry-After": "9"}), OK])
    assert slept == [1]

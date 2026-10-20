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


@pytest.fixture(autouse=True)
def _fresh_budget():
    http.reset_retry_budget()
    yield
    http.reset_retry_budget()


async def test_a_deeply_nested_429_body_is_not_a_hint():
    body = httpx.Response(429, content=b"[" * 200_000)
    slept = await _run([body, OK])
    assert slept == [1]


async def test_retry_after_waits_stop_once_the_per_host_budget_is_spent():
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    for _ in range(3):
        hinted = [httpx.Response(429, headers={"Retry-After": "30"}) for _ in range(2)]
        async with _client([*hinted, OK]) as client:
            await http.request_json(client, "GET", "https://example.com/x", sleep=fake_sleep)

    # 120s budget = four 30s waits; the fifth and sixth fall back to 1s, 2s backoff.
    assert slept == [30.0, 30.0, 30.0, 30.0, 1, 2]


async def test_the_budget_is_per_host():
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    for host in ("a.example.com", "b.example.com"):
        for _ in range(2):
            hinted = [httpx.Response(429, headers={"Retry-After": "30"}) for _ in range(2)]
            async with _client([*hinted, OK]) as client:
                await http.request_json(client, "GET", f"https://{host}/x", sleep=fake_sleep)

    assert slept == [30.0] * 8

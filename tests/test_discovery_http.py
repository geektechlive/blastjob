"""`discovery/http.request_json` retry policy.

The module declares `RETRYABLE_STATUS = (429, 500, 502, 503, 504)`, but
`resp.raise_for_status()` raises `httpx.HTTPStatusError` for ANY 4xx/5xx and the
retry loop caught that exception class generically — so a 403 or a 404 was
retried with the full linear backoff. That is wrong three ways: it triples the
requests against a server that has already given a final answer, it costs 3s per
dead endpoint (measurable — one test in test_resolve.py took exactly 3.00s for
this reason, against a next-slowest of 0.08s), and a 404 is *proof of absence*
that the closure logic cares about, so retrying it is meaningless.

No network: every case goes through `httpx.MockTransport`, and `asyncio.sleep`
is replaced so a retry is observable without actually waiting.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from blastjob.discovery import http as dh


def _client(handler, calls: list[httpx.Request]) -> httpx.AsyncClient:
    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(counting))


@pytest.fixture
def no_sleep(monkeypatch):
    """Record backoff durations instead of waiting them out."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422])
async def test_a_final_client_error_is_not_retried(status, no_sleep):
    calls: list[httpx.Request] = []
    async with _client(lambda r: httpx.Response(status), calls) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await dh.request_json(client, "GET", "https://example.test/x")
    assert len(calls) == 1, f"{status} was retried {len(calls)} times"
    assert no_sleep == []


@pytest.mark.parametrize("status", dh.RETRYABLE_STATUS)
async def test_a_retryable_status_is_retried_with_linear_backoff(status, no_sleep):
    calls: list[httpx.Request] = []
    async with _client(lambda r: httpx.Response(status), calls) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await dh.request_json(client, "GET", "https://example.test/x")
    assert len(calls) == 3  # default max_retries
    assert no_sleep == [1, 2]


async def test_a_timeout_is_still_retried(no_sleep):
    calls: list[httpx.Request] = []

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("too slow")

    async with _client(boom, calls) as client:
        with pytest.raises(httpx.TimeoutException):
            await dh.request_json(client, "GET", "https://example.test/x")
    assert len(calls) == 3
    assert no_sleep == [1, 2]


async def test_a_retryable_status_that_then_succeeds_returns_the_json(no_sleep):
    calls: list[httpx.Request] = []

    def flaky(_request: httpx.Request) -> httpx.Response:
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    async with _client(flaky, calls) as client:
        assert await dh.request_json(client, "GET", "https://example.test/x") == {"ok": True}
    assert len(calls) == 2
    assert no_sleep == [1]


async def test_success_makes_one_request_and_never_sleeps(no_sleep):
    calls: list[httpx.Request] = []
    async with _client(lambda r: httpx.Response(200, json=[1, 2]), calls) as client:
        assert await dh.request_json(client, "GET", "https://example.test/x") == [1, 2]
    assert len(calls) == 1
    assert no_sleep == []

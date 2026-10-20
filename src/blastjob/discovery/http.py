"""Shared async HTTP with bounded timeout + retry for discovery adapters.

External job sources fail transiently (rate limits, blips). Adapters call
`get_json` and get either parsed JSON or a raised error after N attempts —
they never see a half-response. No key handling here; that's per-adapter.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

USER_AGENT = "blastjob/0.1 (personal job-search tool)"


RETRYABLE_STATUS = (429, 500, 502, 503, 504)
# A server's Retry-After is honoured up to this long; a larger ask is capped so one
# misbehaving host cannot stall a run.
MAX_RETRY_AFTER_S = 30.0

# Total Retry-After waiting allowed per host per process. Once spent, a 429 fails
# fast on the plain short backoff instead of waiting out another server-set delay.
RETRY_AFTER_BUDGET_S = 120.0
_retry_after_spent: dict[str, float] = {}

Sleep = Callable[[float], Awaitable[None]]


def reset_retry_budget() -> None:
    """Forget all spent Retry-After time (tests; a long-lived process may call it too)."""
    _retry_after_spent.clear()


def _budgeted_wait(host: str, hinted: float | None, attempt: int) -> float:
    """The seconds to sleep: the hint if there is one and the host's budget covers
    it (charging the budget), else the plain `attempt`-second backoff."""
    if hinted is None:
        return attempt
    spent = _retry_after_spent.get(host, 0.0)
    if spent + hinted > RETRY_AFTER_BUDGET_S:
        return attempt
    _retry_after_spent[host] = spent + hinted
    return hinted


def retry_after_seconds(resp: httpx.Response) -> float | None:
    """Seconds a 429 asks us to wait, capped at MAX_RETRY_AFTER_S, else None.

    Reads the Retry-After header (seconds form only; the HTTP-date form is treated
    as absent) or Discord's JSON body `retry_after` (float seconds). Anything
    unparseable, negative or non-finite is None so the caller keeps its backoff."""
    if resp.status_code != 429:
        return None
    raw: object = resp.headers.get("Retry-After")
    if raw is None:
        try:
            body = resp.json()
        except (ValueError, RecursionError):  # RecursionError: a hostile deeply nested body
            return None
        raw = body.get("retry_after") if isinstance(body, dict) else None
    try:
        seconds = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_S)


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any = None,
    max_retries: int = 3,
    headers: dict[str, str] | None = None,
    sleep: Sleep | None = None,
) -> Any:
    """Issue one request on an existing client and return parsed JSON, retrying
    transient errors (timeouts, 5xx, 429) with linear backoff (a 429 waits for its
    Retry-After instead when it gives one, capped). Adapters that make
    many calls per run (Workday listing + detail) share one client this way."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)

    # Looked up per call (not a default argument) so a test can patch asyncio.sleep.
    nap = sleep or asyncio.sleep
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = await client.request(method, url, params=params, json=json, headers=hdrs)
            if resp.status_code in RETRYABLE_STATUS:
                raise httpx.HTTPStatusError(
                    f"retryable {resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            # `raise_for_status()` raises this for ANY 4xx/5xx, so catching the
            # class alone retried 403/404/422 — tripling requests against a
            # server that already gave its final answer, and costing the full
            # 1s+2s backoff per dead endpoint. A 404 is also proof of absence
            # that the closure logic reads; retrying it says nothing new.
            if e.response.status_code not in RETRYABLE_STATUS:
                raise
            last_exc = e
            if attempt < max_retries:
                hinted = retry_after_seconds(e.response)
                await nap(_budgeted_wait(e.response.request.url.host, hinted, attempt))
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            if attempt < max_retries:
                await nap(attempt)  # 1s, 2s, ...
    assert last_exc is not None
    raise last_exc


async def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout_s: int = 15,
    max_retries: int = 3,
    headers: dict[str, str] | None = None,
) -> Any:
    """GET `url` and return parsed JSON. Retries transient errors (timeouts,
    5xx, 429) with linear backoff. Raises on final failure or non-JSON body."""
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
        return await request_json(
            client, "GET", url, params=params, max_retries=max_retries, headers=headers
        )

"""Shared async HTTP with bounded timeout + retry for discovery adapters.

External job sources fail transiently (rate limits, blips). Adapters call
`get_json` and get either parsed JSON or a raised error after N attempts —
they never see a half-response. No key handling here; that's per-adapter.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

USER_AGENT = "blastjob/0.1 (personal job-search tool)"


RETRYABLE_STATUS = (429, 500, 502, 503, 504)


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any = None,
    max_retries: int = 3,
    headers: dict[str, str] | None = None,
) -> Any:
    """Issue one request on an existing client and return parsed JSON, retrying
    transient errors (timeouts, 5xx, 429) with linear backoff. Adapters that make
    many calls per run (Workday listing + detail) share one client this way."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)

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
                await asyncio.sleep(attempt)  # 1s, 2s, ...
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            if attempt < max_retries:
                await asyncio.sleep(attempt)  # 1s, 2s, ...
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

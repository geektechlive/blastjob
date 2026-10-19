"""Tests for upload_guard.py (parse_content_length helper and MaxBodySizeMiddleware)."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from blastjob.web.upload_guard import (
    MaxBodySizeMiddleware,
    parse_content_length,
)


def test_parse_content_length():
    # None or empty
    assert parse_content_length(None) is None
    assert parse_content_length("") is None
    assert parse_content_length(b"") is None

    # Valid non-negative integers
    assert parse_content_length("0") == 0
    assert parse_content_length(b"0") == 0
    assert parse_content_length("100") == 100
    assert parse_content_length(b"100") == 100
    assert parse_content_length("  42  ") == 42
    assert parse_content_length(42) == 42

    # Malformed / invalid
    assert parse_content_length("abc") is None
    assert parse_content_length(b"abc") is None
    assert parse_content_length("-1") is None
    assert parse_content_length(b"-1") is None
    assert parse_content_length("-100") is None
    assert parse_content_length("12.34") is None
    assert parse_content_length([100]) is None  # type: ignore[arg-type]


def test_max_body_size_middleware_allows_normal_request():
    async def echo(request):
        body = await request.body()
        return PlainTextResponse(body.decode("utf-8"))

    app = Starlette(routes=[Route("/upload", echo, methods=["POST"])])
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=100)
    client = TestClient(app)

    r = client.post("/upload", content=b"hello", headers={"content-length": "5"})
    assert r.status_code == 200
    assert r.text == "hello"


def test_max_body_size_middleware_rejects_oversized_declared_length():
    async def echo(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/upload", echo, methods=["POST"])])
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=100)
    client = TestClient(app)

    r = client.post("/upload", content=b"short", headers={"content-length": "1000"})
    assert r.status_code == 413
    assert "too large" in r.text.lower()


def test_max_body_size_middleware_malformed_content_length_falls_through_to_streaming():
    async def echo(request):
        body = await request.body()
        return PlainTextResponse(body.decode("utf-8"))

    app = Starlette(routes=[Route("/upload", echo, methods=["POST"])])
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=100)
    client = TestClient(app, raise_server_exceptions=False)

    for malformed in ("abc", "-1", "none"):
        r = client.post("/upload", content=b"hello", headers={"content-length": malformed})
        assert r.status_code == 200
        assert r.text == "hello"


def test_max_body_size_middleware_malformed_content_length_streaming_cap():
    async def echo(request):
        await request.body()
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/upload", echo, methods=["POST"])])
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=10)
    client = TestClient(app)

    with pytest.raises(ClientDisconnect):
        client.post("/upload", content=b"a" * 20, headers={"content-length": "abc"})

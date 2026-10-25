"""Reject an oversized request body before anything buffers it.

The LinkedIn import form is a plain multipart POST with a hidden `_token` and no
HTMX header, so `require_csrf` takes its `await request.form()` branch — and
Starlette spools the ENTIRE body to disk to build that form before the route
function runs. So the route's own content-length check and its chunked-write cap
are both dead code: the bytes have already landed by the time either executes,
and the comment claiming the upload is "streamed in chunks rather than buffered
whole" was not true. Middleware is the only layer that actually runs first.

The cap is deliberately generous. A real LinkedIn archive runs to tens of
megabytes, and a previous 10MB limit silently rejected exactly the file this
feature exists for.
"""

from __future__ import annotations

import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Lives here rather than in web/app.py: the middleware below enforces it and the
# network-import route checks against it, so the cap and its enforcement should
# not be able to drift apart in two files.
MAX_NETWORK_UPLOAD_BYTES = 400 * 1024 * 1024


def parse_content_length(raw: str | bytes | None) -> int | None:
    """A declared Content-Length as a non-negative int, or None when absent or
    malformed. Callers treat None as "unknown" and rely on the byte-counting
    middleware, so a garbage header never raises into a 500."""
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


class MaxBodySizeMiddleware:
    """413 an over-cap body, by declared Content-Length or by counting bytes."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        declared = parse_content_length(headers.get(b"content-length"))
        # A malformed header is None here and falls through to the streaming count.
        if declared is not None and declared > self.max_bytes:
            await _reject(send)
            return

        seen = 0

        async def guarded_receive() -> Message:
            # A chunked body carries no content-length, so the only way to bound
            # it is to count as it arrives and cut the stream short.
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, guarded_receive, send)


async def _reject(
    send: Send, status: int = 413, body: bytes = b"That upload is too large."
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": body})


# The interview debrief (#84) takes a pasted transcript as an urlencoded form. Its cap is far
# below the network import's, and a multipart body is refused outright: Starlette spools a
# multipart body to a temporary file before any route runs, which would write the transcript
# to disk. Bodies here are parsed in memory only.
MAX_DEBRIEF_BODY_BYTES = 2 * 1024 * 1024
# A prefix, matched without an end anchor: Starlette's route patterns end in `$`, which also
# accepts a trailing newline (`/debrief/upload%0A`), so an anchored pattern here would miss it.
DEBRIEF_PATH = r"/jobs/\d+/debrief"
_MULTIPART = b"multipart/"


class PathBodyCapMiddleware:
    """Apply a smaller body cap, and refuse multipart, on paths that start with `pattern`."""

    def __init__(self, app: ASGIApp, pattern: str, max_bytes: int) -> None:
        self.app = app
        self.pattern = re.compile(pattern)
        self.capped = MaxBodySizeMiddleware(app, max_bytes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.pattern.match(scope["path"]):
            await self.app(scope, receive, send)
            return
        if scope["method"] == "POST":
            # Every content-type header counts: a duplicate must not hide a multipart one.
            ctypes = [v for k, v in scope.get("headers") or [] if k.lower() == b"content-type"]
            if any(v.strip().lower().startswith(_MULTIPART) for v in ctypes):
                await _reject(send, 415, b"This page accepts pasted text only.")
                return
        await self.capped(scope, receive, send)

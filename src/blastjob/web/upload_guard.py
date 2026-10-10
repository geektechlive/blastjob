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

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Lives here rather than in web/app.py: the middleware below enforces it and the
# network-import route checks against it, so the cap and its enforcement should
# not be able to drift apart in two files.
MAX_NETWORK_UPLOAD_BYTES = 400 * 1024 * 1024


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
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await _reject(send)
                    return
            except ValueError:
                pass  # malformed header — fall through to the streaming count

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


async def _reject(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": b"That upload is too large."})

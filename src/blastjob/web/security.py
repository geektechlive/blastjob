"""Localhost is not an auth boundary (Codex #7): a malicious web page can POST to
127.0.0.1 and trigger costly actions. Defenses, proportionate for a single-user
local tool:

- a per-launch secret required on every state-changing request (header or form
  field), so a drive-by page that doesn't know the token can't act;
- an Origin/Referer host check limiting requests to localhost.

Jinja autoescaping (on by default) handles stored-XSS from untrusted job text;
that's enforced at the template layer, not here.
"""

from __future__ import annotations

import ipaddress
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.types import ASGIApp, Receive, Scope, Send

# One token per process launch. Embedded in pages, required back on writes.
LAUNCH_TOKEN = secrets.token_urlsafe(24)

# Spelled out rather than using ipaddress.is_private, which also returns True for
# reserved and documentation ranges such as 203.0.113.0/24 (TEST-NET-3). Those are
# not networks the user is ever on, so there is no reason to trust an origin claiming
# to be one.
_PRIVATE_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)
CSRF_HEADER = "x-blastjob-token"
CSRF_FIELD = "_token"


def _host_ok(value: str) -> bool:
    """Accept loopback, and any RFC1918 private address.

    The private range is needed because reaching the cockpit from another device
    on the LAN means the browser sends that device's view of the server as the
    Origin -- with a loopback-only allowlist every button 403s and the page is
    read-only. Public origins stay rejected, which is what actually stops a
    malicious web page from driving this via DNS rebinding: the attacker's page
    carries its own domain in Origin, never a private address.
    """
    if not value or "://" not in value:
        return False
    try:
        host = urlsplit(value).hostname
    except ValueError:
        return False
    return _is_local(host or "")


def _is_local(host: str) -> bool:
    """A bare hostname (no port, no brackets) that is loopback or a private
    address literal. A DNS name other than localhost never is: that is the
    name a rebinding page would arrive under."""
    if host.strip("[]") in {"127.0.0.1", "localhost", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return any(ip in net for net in _PRIVATE_NETS)


def _host_header_ok(value: str) -> bool:
    try:
        host = urlsplit(f"//{value}").hostname
    except ValueError:
        return False
    return _is_local(host or "")


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class LocalRequestGuard:
    """Runs before routing and before any body is read.

    - Host must be loopback or a private address literal (421 otherwise). A
      DNS-rebinding page reaches the cockpit under its own domain name, so
      this is what keeps it from reading pages; the Origin check below only
      covers writes.
    - A write whose Origin/Referer is foreign is refused here (403), before
      Starlette spools a multipart body to disk. `require_csrf` still checks
      the token per route.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        if not _host_header_ok(headers.get("host", "")):
            await _refuse(send, 421, b"misdirected request: not a local host")
            return
        if scope["type"] == "http" and scope["method"] not in _SAFE_METHODS:
            origin = headers.get("origin") or headers.get("referer") or ""
            if origin and not _host_ok(origin):
                await _refuse(send, 403, b"bad origin")
                return
        await self.app(scope, receive, send)


async def _refuse(send: Send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def require_csrf(request: Request) -> None:
    """Guard for state-changing routes. Accepts the token via the HX/AJAX header
    or a form field, and requires a localhost Origin/Referer when one is present."""
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if origin and not _host_ok(origin):
        raise HTTPException(status_code=403, detail="bad origin")

    token = request.headers.get(CSRF_HEADER)
    if token is None:
        form = await request.form()
        field = form.get(CSRF_FIELD)
        token = field if isinstance(field, str) else None
    if not token or not secrets.compare_digest(token, LAUNCH_TOKEN):
        raise HTTPException(status_code=403, detail="bad or missing token")

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

from fastapi import HTTPException, Request

# One token per process launch. Embedded in pages, required back on writes.
LAUNCH_TOKEN = secrets.token_urlsafe(24)

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}

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
    if not value:
        return False
    # value is a scheme://host[:port]; extract host
    try:
        hostport = value.split("://", 1)[1].split("/", 1)[0]
    except IndexError:
        return False
    host = hostport.rsplit(":", 1)[0] if ":" in hostport else hostport
    if host in _ALLOWED_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return any(ip in net for net in _PRIVATE_NETS)


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

"""The cockpit answers only requests addressed to a local host.

POSTs were already Origin-checked, but GETs were not, and nothing checked the
Host header: a web page could rebind its own domain to the cockpit's address
and read every page (resumes, settings, contacts, the CSRF token). A rebinding
page always carries its own domain in Host, so a Host allowlist of loopback
and private-address literals stops it. The Origin check also has to run before
a body is read, or a hostile page can still make the cockpit spool a large
upload to disk before the 403.
"""

from __future__ import annotations

import pytest

from blastjob.web.security import LocalRequestGuard


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _call(method: str, host: str, origin: str | None = None) -> int:
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    scope = {"type": "http", "method": method, "path": "/", "headers": headers}

    async def receive():
        raise AssertionError("the guard must decide before reading the body")

    status: list[int] = []

    async def send(message):
        if message["type"] == "http.response.start":
            status.append(message["status"])

    await LocalRequestGuard(_ok_app)(scope, receive, send)
    return status[0]


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1:8765",
        "localhost:8765",
        "localhost",
        "[::1]:8765",
        "192.168.20.15:8765",
        "10.0.0.4",
    ],
)
async def test_local_hosts_are_served(host):
    assert await _call("GET", host) == 200


@pytest.mark.parametrize(
    "host", ["attacker.example:8765", "attacker.example", "203.0.113.5:8765", "", "8.8.8.8"]
)
async def test_other_hosts_are_refused(host):
    assert await _call("GET", host) == 421


async def test_foreign_origin_post_refused_before_body_is_read():
    assert await _call("POST", "127.0.0.1:8765", origin="https://attacker.example") == 403


async def test_local_origin_post_passes_through():
    assert await _call("POST", "127.0.0.1:8765", origin="http://127.0.0.1:8765") == 200


async def test_get_with_foreign_origin_header_is_not_refused():
    # Origin on a GET is not a write; the Host check is what protects reads.
    assert await _call("GET", "127.0.0.1:8765", origin="https://attacker.example") == 200


def test_cockpit_app_installs_the_guard(tmp_path, monkeypatch):
    from blastjob.models.config import BlastJobConfig
    from blastjob.web.app import create_app

    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(tmp_path))
    app = create_app(load_config=BlastJobConfig, save_config=lambda _c: None)
    assert any(m.cls is LocalRequestGuard for m in app.user_middleware)

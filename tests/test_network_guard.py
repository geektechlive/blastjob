"""The suite-wide network guard (tests/conftest.py::_no_real_network) refuses
every non-loopback connection and lets loopback through.

The refused cases use TEST-NET-1 (192.0.2.1, RFC 5737) and the reserved
`.invalid` TLD with a short timeout, so a broken guard can neither reach a
real host nor hang the run. Each refusal is also recorded on the fixture's
list; these tests clear it so their own teardown does not fail.
"""

from __future__ import annotations

import asyncio
import socket
import threading

import httpx
import pytest

from tests.conftest import NetworkBlockedError

BLOCKED_IP = "192.0.2.1"
BLOCKED_HOST = "blastjob-guard-test.invalid"
TIMEOUT_S = 0.5


@pytest.fixture
def guard(_no_real_network):
    yield _no_real_network
    _no_real_network.clear()


def _loopback_listener(family: int, host: str) -> socket.socket:
    srv = socket.socket(family, socket.SOCK_STREAM)
    srv.bind((host, 0))
    srv.listen(1)
    return srv


def test_connect_to_external_ip_is_refused_and_recorded(guard):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(TIMEOUT_S)
        with pytest.raises(NetworkBlockedError, match="test_network_guard.py"):
            s.connect((BLOCKED_IP, 443))
    assert guard == [f"socket.connect({(BLOCKED_IP, 443)!r})"]


def test_connect_ex_to_external_ip_is_refused(guard):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(TIMEOUT_S)
        with pytest.raises(NetworkBlockedError):
            s.connect_ex((BLOCKED_IP, 80))
    assert len(guard) == 1


def test_create_connection_to_hostname_is_refused_without_resolving(guard):
    with pytest.raises(NetworkBlockedError, match="Fake the call"):
        socket.create_connection((BLOCKED_HOST, 443), timeout=TIMEOUT_S)
    assert guard == [f"socket.create_connection({(BLOCKED_HOST, 443)!r})"]


def test_dns_lookup_of_external_host_is_refused(guard):
    with pytest.raises(NetworkBlockedError):
        socket.getaddrinfo(BLOCKED_HOST, 443)
    assert len(guard) == 1


def test_external_ipv6_is_refused(guard):
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
        with pytest.raises(NetworkBlockedError):
            s.connect(("2001:db8::1", 443, 0, 0))
    assert len(guard) == 1


def test_unspecified_and_empty_hosts_are_refused(guard):
    for host in ("0.0.0.0", ""):
        with pytest.raises(NetworkBlockedError):
            socket.create_connection((host, 80), timeout=TIMEOUT_S)
    assert len(guard) == 2


def test_httpx_request_to_external_host_is_refused(guard):
    """The path every discovery adapter takes; fail-soft handlers that catch
    httpx.TransportError must not swallow it."""
    with pytest.raises(NetworkBlockedError):
        httpx.get(f"http://{BLOCKED_HOST}/", timeout=TIMEOUT_S)
    assert guard


def test_asyncio_open_connection_is_refused(guard):
    async def go() -> None:
        await asyncio.open_connection(BLOCKED_HOST, 443)

    with pytest.raises(NetworkBlockedError):
        asyncio.run(go())
    assert guard


def test_refusal_in_a_worker_thread_is_still_recorded(guard):
    """A thread's exception never reaches the test; the record does."""

    def leak() -> None:
        try:
            socket.create_connection((BLOCKED_IP, 443), timeout=TIMEOUT_S)
        except NetworkBlockedError:
            pass  # swallowed on purpose: simulating a fail-soft caller

    t = threading.Thread(target=leak)
    t.start()
    t.join()
    assert len(guard) == 1


def test_loopback_ipv4_connects():
    srv = _loopback_listener(socket.AF_INET, "127.0.0.1")
    with srv, socket.create_connection(srv.getsockname()[:2], timeout=TIMEOUT_S):
        pass


def test_localhost_name_connects():
    srv = _loopback_listener(socket.AF_INET, "127.0.0.1")
    with srv, socket.create_connection(("localhost", srv.getsockname()[1]), timeout=TIMEOUT_S):
        pass


def test_loopback_ipv6_connects():
    if not socket.has_ipv6:
        pytest.skip("no IPv6")
    try:
        srv = _loopback_listener(socket.AF_INET6, "::1")
    except OSError:
        pytest.skip("::1 not bindable here")
    with srv, socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as c:
        c.settimeout(TIMEOUT_S)
        c.connect(srv.getsockname())


def test_unix_sockets_and_socketpair_work():
    a, b = socket.socketpair()
    with a, b:
        a.sendall(b"ok")
        assert b.recv(2) == b"ok"


def test_fastapi_testclient_is_unaffected():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"ok": "yes"}

    assert TestClient(app).get("/ping").json() == {"ok": "yes"}


def test_sqlite_is_unaffected(tmp_path):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "x.db")
    try:
        assert conn.execute("select 1").fetchone() == (1,)
    finally:
        conn.close()

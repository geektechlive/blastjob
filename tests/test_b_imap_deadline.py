"""B1: an IMAP fetch is bounded by a socket timeout AND a wall-clock deadline.

`asyncio.to_thread` cannot be cancelled, so a stalled mailbox used to hold the
whole discovery run forever. The bound lives inside the blocking loop: past the
deadline it raises and the connection is closed on the way out.
"""

from __future__ import annotations

import imaplib
from datetime import date

import pytest

from blastjob.discovery import mailalerts

HEADER = b"Message-ID: <m%d@x>\r\nFrom: jobs-noreply@linkedin.com\r\nSubject: alert\r\n\r\n"


class _FakeImap:
    def __init__(self, uids: int, clock: list[float], step: float):
        self.uids, self.clock, self.step = uids, clock, step
        self.logged_out = False
        self.fetches = 0

    def login(self, *_a):
        return "OK", []

    def select(self, *_a, **_k):
        return "OK", []

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [b" ".join(str(i).encode() for i in range(1, self.uids + 1))]
        self.fetches += 1
        self.clock[0] += self.step  # each server round trip "takes" this long
        n = int(args[0])
        if "HEADER" in args[1]:
            return "OK", [(b"1", HEADER % n)]
        return "OK", [(b"1", HEADER % n + b"<p>hi</p>")]

    def logout(self):
        self.logged_out = True


def _run(conn, clock, deadline_s):
    return mailalerts.fetch_alert_messages(
        host="h",
        user="u",
        password="p",
        since=date(2026, 1, 1),
        skip_message_ids=set(),
        connect=lambda _host: conn,
        deadline_s=deadline_s,
        clock=lambda: clock[0],
    )


def test_deadline_stops_early_and_closes_the_connection():
    # Round 2 (D2): the partial list is returned, not discarded (see test_r2d_*).
    clock = [0.0]
    conn = _FakeImap(uids=50, clock=clock, step=10.0)
    out = _run(conn, clock, deadline_s=30.0)
    assert 0 < len(out) < 50
    assert conn.logged_out
    assert conn.fetches < 50 * 2  # stopped early, not after draining the mailbox


def test_within_deadline_returns_messages():
    clock = [0.0]
    conn = _FakeImap(uids=3, clock=clock, step=1.0)
    out = _run(conn, clock, deadline_s=300.0)
    assert len(out) == 3 and conn.logged_out


def test_failing_logout_falls_back_to_shutdown():
    clock = [0.0]
    conn = _FakeImap(uids=50, clock=clock, step=10.0)
    shut = []

    def bad_logout():
        raise OSError("stalled")

    conn.logout = bad_logout  # type: ignore[method-assign]
    conn.shutdown = lambda: shut.append(True)  # type: ignore[attr-defined]
    _run(conn, clock, deadline_s=30.0)
    assert shut == [True]


def test_open_imap_sets_a_socket_timeout(monkeypatch):
    seen = {}

    def fake(host, *a, **kw):
        seen.update(kw)
        return object()

    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake)
    mailalerts.open_imap("imap.example.com", timeout=7)
    assert seen["timeout"] == 7 and seen["ssl_context"] is not None


def test_open_imap_default_timeout_is_finite(monkeypatch):
    seen = {}
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda host, **kw: seen.update(kw))
    mailalerts.open_imap("imap.example.com")
    assert seen["timeout"] and seen["timeout"] > 0


def test_fetch_without_custom_connect_passes_timeout_to_socket(monkeypatch):
    seen = {}

    def fake(host, **kw):
        seen.update(kw)
        raise RuntimeError("stop")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake)
    with pytest.raises(RuntimeError):
        mailalerts.fetch_alert_messages(
            host="h",
            user="u",
            password="p",
            since=date(2026, 1, 1),
            skip_message_ids=set(),
            timeout_s=9,
        )
    assert seen["timeout"] == 9

"""Round 2 D1/D2: a thinner research re-run keeps stored facts; an IMAP deadline
returns the messages already read instead of discarding them."""

from __future__ import annotations

import tempfile
from datetime import date

import pytest

from blastjob.core import company as co
from blastjob.db import client as dbc
from blastjob.discovery import mailalerts
from blastjob.models.config import BlastJobConfig


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    cfg.paths.output_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    yield c
    c.close()


def test_sparse_rerun_keeps_stored_facts_and_sources(conn):
    full = co.CompanyProfile(
        company="Acme", employees=100, ceo="Old Ceo", hq="Oakbrook, OH",
        tech_stack=[{"area": "erp", "vendor": "X"}],
        sources=[{"url": "https://acme.example.com/a"}, {"url": "https://acme.example.com/b"}],
    )  # fmt: skip
    co.upsert_profile(conn, full)
    seen = co.get_profile(conn, "Acme")["updated_at"]

    sparse = co.CompanyProfile(company="Acme", ceo="New Ceo")
    co.refresh_research_facts(conn, sparse, expected_updated_at=seen)

    got = co.get_profile(conn, "Acme")
    assert got["ceo"] == "New Ceo"  # a non-empty value still lands
    assert got["employees"] == 100 and got["hq"] == "Oakbrook, OH"
    assert got["tech_stack"] == [{"area": "erp", "vendor": "X"}]
    assert len(got["sources"]) == 2


def test_rerun_with_nothing_new_still_bumps_nothing_destructive(conn):
    co.upsert_profile(conn, co.CompanyProfile(company="Acme", employees=7))
    seen = co.get_profile(conn, "Acme")["updated_at"]
    assert co.refresh_research_facts(conn, co.CompanyProfile(company="Acme"),
                                     expected_updated_at=seen) is not None  # fmt: skip
    assert co.get_profile(conn, "Acme")["employees"] == 7


HEADER = b"Message-ID: <m%d@x>\r\nFrom: jobs-noreply@linkedin.com\r\nSubject: alert\r\n\r\n"


class _FakeImap:
    def __init__(self, uids: int, clock: list[float], step: float):
        self.uids, self.clock, self.step = uids, clock, step
        self.logged_out = False

    def login(self, *_a):
        return "OK", []

    def select(self, *_a, **_k):
        return "OK", []

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [b" ".join(str(i).encode() for i in range(1, self.uids + 1))]
        self.clock[0] += self.step
        n = int(args[0])
        if "HEADER" in args[1]:
            return "OK", [(b"1", HEADER % n)]
        return "OK", [(b"1", HEADER % n + b"<p>hi</p>")]

    def logout(self):
        self.logged_out = True


def _run(conn, clock, deadline_s):
    return mailalerts.fetch_alert_messages(
        host="h", user="u", password="p", since=date(2026, 1, 1),
        skip_message_ids=set(), connect=lambda _h: conn, deadline_s=deadline_s,
        clock=lambda: clock[0],
    )  # fmt: skip


def test_deadline_returns_the_messages_already_read(conn=None):
    clock = [0.0]
    imap = _FakeImap(uids=50, clock=clock, step=10.0)
    out = _run(imap, clock, deadline_s=65.0)
    assert 0 < len(out) < 50
    assert imap.logged_out


def test_deadline_before_any_message_still_raises():
    clock = [0.0]
    imap = _FakeImap(uids=5, clock=clock, step=10.0)
    clock[0] = 100.0  # already past the deadline at the first message
    deadline_start = clock[0]
    assert deadline_start == 100.0
    with pytest.raises(TimeoutError):
        mailalerts.fetch_alert_messages(
            host="h", user="u", password="p", since=date(2026, 1, 1),
            skip_message_ids=set(), connect=lambda _h: imap, deadline_s=-1.0,
            clock=lambda: clock[0],
        )  # fmt: skip
    assert imap.logged_out

"""B4: an alert message's ledger row commits WITH its postings, never before.

The ledger row marks a message done forever (status ok/ignored is never fetched
again). It used to be written before the postings were saved, so one raising
upsert lost those postings for good. Now each message is one transaction, and one
raising posting in a non-alert source no longer aborts the rest.
"""

from __future__ import annotations

import tempfile

import pytest

from blastjob.db import client as dbc
from blastjob.discovery import mailalerts as ma
from blastjob.discovery import pipeline
from blastjob.discovery.base import RawPosting
from blastjob.discovery.pipeline import processed_message_ids, run_discovery
from blastjob.models.config import BlastJobConfig

LI = "jobs-noreply@linkedin.com"


def _li_html(*ids: str) -> str:
    rows = "".join(
        f'<tr><td><a href="https://www.linkedin.com/comm/jobs/view/{4_000_000_000 + int(i)}/'
        f'?trackingId=x">VP IT {i}</a><p>Acme Corp · Chicago, IL</p></td></tr>'
        for i in ids
    )
    return f"<table>{rows}</table>"


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _adapter(messages):
    return ma.MailAlertAdapter(
        host="h",
        user="u",
        password="p",
        skip_message_ids=set(),
        fetch_messages=lambda **kw: messages,
        enrich=False,
    )


def _msg(mid, uid, *ids):
    return ma.MailMessage(mid, uid, LI, "alert", _li_html(*ids))


def _n_jobs(conn):
    return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


@pytest.mark.asyncio
async def test_ledger_and_postings_commit_together(conn):
    a = _adapter([_msg("<m1>", "1", "1001"), _msg("<m2>", "2", "1002")])
    await run_discovery(BlastJobConfig(), conn, [a])
    conn.rollback()  # prove durability: nothing was left pending
    assert _n_jobs(conn) == 2
    assert processed_message_ids(conn) == {"<m1>", "<m2>"}


@pytest.mark.asyncio
async def test_failing_message_rolls_back_and_stays_retryable(conn, monkeypatch):
    real = dbc.upsert_job

    def flaky(c, **kw):
        if kw["external_id"].endswith("1002"):
            raise RuntimeError("disk hiccup")
        return real(c, **kw)

    monkeypatch.setattr(dbc, "upsert_job", flaky)
    # Round 2 (D5): a message whose postings ALL fail is rolled back and retryable;
    # one with a surviving posting is 'ok' (see test_r2d_discovery).
    a = _adapter([_msg("<m1>", "1", "1001"), _msg("<m2>", "2", "1002")])
    res = await run_discovery(BlastJobConfig(), conn, [a])
    conn.rollback()

    # m1 landed; m2 saved nothing and stays retryable.
    assert _n_jobs(conn) == 1
    assert processed_message_ids(conn) == {"<m1>"}
    row = conn.execute("SELECT status FROM mail_ledger WHERE message_id='<m2>'").fetchone()
    assert row is not None and row["status"] == "error"
    assert "mail-alerts" in res.errors and "disk hiccup" in res.errors["mail-alerts"]

    # next run (upsert healthy again) picks the message up and finishes the job
    monkeypatch.setattr(dbc, "upsert_job", real)
    await run_discovery(BlastJobConfig(), conn, [_adapter([_msg("<m2>", "2", "1002")])])
    assert _n_jobs(conn) == 2
    assert "<m2>" in processed_message_ids(conn)


@pytest.mark.asyncio
async def test_posting_shared_by_two_messages_survives_first_message_failing(conn, monkeypatch):
    real = dbc.upsert_job
    calls = {"n": 0}

    def flaky(c, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first write fails")
        return real(c, **kw)

    monkeypatch.setattr(dbc, "upsert_job", flaky)
    a = _adapter([_msg("<m1>", "1", "1001"), _msg("<m2>", "2", "1001")])
    await run_discovery(BlastJobConfig(), conn, [a])
    # m1 failed, but m2 carries the same posting and saved it.
    assert _n_jobs(conn) == 1
    assert processed_message_ids(conn) == {"<m2>"}


@pytest.mark.asyncio
async def test_ignored_message_is_recorded(conn):
    junk = ma.MailMessage("<j>", "9", "news@example.com", "hi", "<p>hello</p>")
    await run_discovery(BlastJobConfig(), conn, [_adapter([junk])])
    conn.rollback()
    assert processed_message_ids(conn) == {"<j>"}


class _Board:
    source = "greenhouse:acme"
    snapshot = True

    def __init__(self, postings):
        self._p = postings

    async def fetch(self):
        return self._p


@pytest.mark.asyncio
async def test_one_raising_posting_does_not_abort_the_rest(conn, monkeypatch):
    real = dbc.upsert_job

    def flaky(c, **kw):
        if kw["external_id"] == "bad":
            raise RuntimeError("boom")
        return real(c, **kw)

    monkeypatch.setattr(dbc, "upsert_job", flaky)
    posts = [
        RawPosting(source="greenhouse:acme", external_id=i, company="Acme", title="VP IT")
        for i in ("a", "bad", "c")
    ]
    res = await run_discovery(BlastJobConfig(), conn, [_Board(posts)])
    conn.rollback()
    ids = {r["external_id"] for r in conn.execute("SELECT external_id FROM jobs")}
    assert ids == {"a", "c"}
    assert "greenhouse:acme" in res.errors and "bad" in res.errors["greenhouse:acme"]


@pytest.mark.asyncio
async def test_failed_posting_is_not_stale_marked(conn, monkeypatch):
    """A posting that failed to save was still LISTED by the board."""
    real = dbc.upsert_job
    first = [RawPosting(source="greenhouse:acme", external_id="x", company="Acme", title="VP")]
    await run_discovery(BlastJobConfig(), conn, [_Board(first)])

    def flaky(c, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(dbc, "upsert_job", flaky)
    await run_discovery(BlastJobConfig(), conn, [_Board(first)])
    monkeypatch.setattr(dbc, "upsert_job", real)
    state = conn.execute("SELECT source_state FROM jobs WHERE external_id='x'").fetchone()[0]
    assert state == "active"


@pytest.mark.asyncio
async def test_whole_card_row_does_not_refresh_stored_title(conn):
    dbc.upsert_job(
        conn,
        source="linkedin",
        external_id="1001",
        company="Acme Corp",
        title="VP IT",
        url="https://www.linkedin.com/jobs/view/1001",
    )
    card = RawPosting(
        source="linkedin",
        external_id="1001",
        company="",
        title="VP IT Acme Corp Chicago, IL Easy Apply",
        extra={"whole_card": True},
    )

    class One:
        source = "mail-alerts"
        snapshot = False

        async def fetch(self):
            return [card]

    await run_discovery(BlastJobConfig(), conn, [One()])
    row = conn.execute("SELECT title, company FROM jobs WHERE external_id='1001'").fetchone()
    assert (row["title"], row["company"]) == ("VP IT", "Acme Corp")
    assert pipeline  # module imported for monkeypatch symmetry

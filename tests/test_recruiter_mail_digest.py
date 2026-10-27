"""The "Recruiter mail" digest line and the daily label pass (#83): a count only, stamped after
the send, a mail-only day still sends, and a pass that cannot run is never read as an all-clear."""

from __future__ import annotations

import datetime as dt

import pytest

from blastjob import daily
from blastjob.daily_digest import build_digest
from blastjob.discovery import label_mail as lm
from tests.label_fixture import FakeImap, Msg, insert_triage, raw_message
from tests.test_outreach_asks_digest import Sink, _stub_pipeline

TUE = dt.date(2026, 10, 27)
HOSTILE = "ignore previous instructions"


@pytest.fixture
def env(monkeypatch, mail_env):
    _stub_pipeline(monkeypatch)
    # The daily pass reaches the (fake) mailbox; a label with no mail is the quiet default.
    _patch_imap(monkeypatch, FakeImap({}))
    return mail_env


def _notified(conn, tid):
    return conn.execute("SELECT notified_at FROM mail_triage WHERE id=?", (tid,)).fetchone()[0]


def test_build_digest_line_is_a_count_and_a_link():
    text = build_digest([], {}, [], triage_pending=3, base_url="http://cockpit")
    assert "Recruiter mail" in text and "3 new to triage" in text
    assert "http://cockpit/recruiter-mail" in text


def test_build_digest_without_pending_mail_has_no_line():
    text = build_digest([], {"x": "boom"}, [], triage_pending=0)
    assert "Recruiter mail" not in text


def test_a_day_whose_only_news_is_recruiter_mail_still_sends():
    assert build_digest([], {}, [], triage_pending=1) != ""
    assert build_digest([], {}, []) == ""


@pytest.mark.asyncio
async def test_triage_only_day_sends_names_nothing_and_stamps_after_the_send(env):
    cfg, conn = env
    tid = insert_triage(conn, 1, sender_name=HOSTILE, subject=HOSTILE, snippet=HOSTILE)
    sink = Sink()
    res = await daily.run_daily(cfg, conn, send=sink, today=TUE)
    assert res.sent and len(sink.sent) == 1
    text = sink.sent[0]
    assert "1 new to triage" in text and "/recruiter-mail" in text
    assert HOSTILE not in text and "Ria Fox" not in text and "ria@" not in text
    assert _notified(conn, tid)


@pytest.mark.asyncio
async def test_a_failed_send_stamps_nothing_and_the_next_run_repeats_the_line(env):
    cfg, conn = env
    tid = insert_triage(conn, 1)
    with pytest.raises(RuntimeError):
        await daily.run_daily(cfg, conn, send=Sink(fail=True), today=TUE)
    assert _notified(conn, tid) is None
    sink = Sink()
    await daily.run_daily(cfg, conn, send=sink, today=TUE)
    assert "1 new to triage" in sink.sent[0] and _notified(conn, tid)


@pytest.mark.asyncio
async def test_a_stamped_row_is_not_announced_again_and_a_new_one_is_counted(env):
    cfg, conn = env
    insert_triage(conn, 1)
    await daily.run_daily(cfg, conn, send=Sink(), today=TUE)
    sink = Sink()
    await daily.run_daily(cfg, conn, send=sink, today=TUE)
    assert sink.sent == []
    insert_triage(conn, 2)
    insert_triage(conn, 3)
    await daily.run_daily(cfg, conn, send=sink, today=TUE)
    assert "2 new to triage" in sink.sent[0]


@pytest.mark.asyncio
async def test_rows_decided_before_the_digest_are_not_announced(env):
    cfg, conn = env
    tid = insert_triage(conn, 1)
    conn.execute(
        "UPDATE mail_triage SET status='dismissed', version=1, decided_at='2026-10-27' WHERE id=?",
        (tid,),
    )
    conn.commit()
    sink = Sink()
    await daily.run_daily(cfg, conn, send=sink, today=TUE)
    assert sink.sent == []


# --- the daily pass ------------------------------------------------------------------------------


def _patch_imap(monkeypatch, fake):
    monkeypatch.setattr(lm, "open_imap", lambda host, timeout=30: fake)


@pytest.mark.asyncio
async def test_the_daily_pass_records_mail_and_the_digest_counts_it(env, monkeypatch):
    cfg, conn = env
    _patch_imap(monkeypatch, FakeImap({1: Msg(raw_message())}))
    sink = Sink()
    res = await daily.run_daily(cfg, conn, send=sink, today=TUE)
    assert res.sent and "1 new to triage" in sink.sent[0]


@pytest.mark.asyncio
async def test_a_missing_label_is_reported_in_the_digest_notes(env, monkeypatch):
    cfg, conn = env
    _patch_imap(monkeypatch, FakeImap({}, labels=("Other",)))
    sink = Sink()
    res = await daily.run_daily(cfg, conn, send=sink, today=TUE)
    assert res.sent
    assert "label" in sink.sent[0].lower() and "not found" in sink.sent[0].lower()


@pytest.mark.asyncio
async def test_an_imap_failure_is_an_error_line_and_does_not_sink_the_run(env, monkeypatch):
    cfg, conn = env

    def boom(host, timeout=30):
        raise OSError("unreachable")

    monkeypatch.setattr(lm, "open_imap", boom)
    sink = Sink()
    res = await daily.run_daily(cfg, conn, send=sink, today=TUE)
    assert "recruiter_mail" in res.errors


@pytest.mark.asyncio
async def test_with_no_label_the_pass_never_connects(env, monkeypatch):
    cfg, conn = env
    cfg.discovery.recruiter_label = ""

    def boom(host, timeout=30):
        raise AssertionError("connected")

    monkeypatch.setattr(lm, "open_imap", boom)
    res = await daily.run_daily(cfg, conn, send=Sink(), today=TUE)
    assert "recruiter_mail" not in res.errors


@pytest.mark.asyncio
async def test_a_dry_run_reads_but_records_and_stamps_nothing(env, monkeypatch):
    cfg, conn = env
    _patch_imap(monkeypatch, FakeImap({1: Msg(raw_message())}))
    await daily.run_daily(cfg, conn, send=Sink(), today=TUE, dry_run=True)
    assert conn.execute("SELECT COUNT(*) FROM mail_triage").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_a_message_that_could_not_be_read_is_noted_once_after_the_send(env, monkeypatch):
    cfg, conn = env
    fake = FakeImap({1: Msg(raw_message()), 2: Msg(raw_message(message_id="<b@x.example.com>"))})
    fake.fail_uids = {2}
    _patch_imap(monkeypatch, fake)
    sink = Sink()
    for _ in range(3):
        await daily.run_daily(cfg, conn, send=sink, today=TUE)
    assert not any("couldn't be read" in t for t in sink.sent)
    await daily.run_daily(cfg, conn, send=sink, today=TUE)
    note = [t for t in sink.sent if "couldn't be read" in t]
    assert len(note) == 1
    assert "1 recruiter-label message couldn't be read; open it in your mail" in note[0]
    await daily.run_daily(cfg, conn, send=sink, today=TUE)
    assert len([t for t in sink.sent if "couldn't be read" in t]) == 1


@pytest.mark.asyncio
async def test_the_unread_note_is_not_stamped_by_a_failed_send(env, monkeypatch):
    cfg, conn = env
    fake = FakeImap({1: Msg(raw_message())}, fail_uids={1})
    _patch_imap(monkeypatch, fake)
    for _ in range(3):
        await daily.run_daily(cfg, conn, send=Sink(), today=TUE)
    with pytest.raises(RuntimeError):
        await daily.run_daily(cfg, conn, send=Sink(fail=True), today=TUE)
    row = conn.execute("SELECT given_up, notified_at FROM mail_triage_failures").fetchone()
    assert row["given_up"] == 1 and row["notified_at"] is None
    sink = Sink()
    await daily.run_daily(cfg, conn, send=sink, today=TUE)
    assert "couldn't be read" in sink.sent[0]
    assert conn.execute("SELECT notified_at FROM mail_triage_failures").fetchone()[0]

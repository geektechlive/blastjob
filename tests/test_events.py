"""Application events (migration 42, `core/events.py`) and the writers that
emit them: manual state changes, the closure lifecycle, and recruiter replies.

The invariants under test are the Codex-review amendments: a state change and
its event commit together or not at all; no event when the state did not
change; deletes are scoped to (id, job_id, source='manual'); newest first is
`at DESC, id DESC` everywhere.
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import date, datetime, timedelta

import pytest

from blastjob.core import closure, events
from blastjob.db import client as dbc
from blastjob.db.schema import MIGRATIONS
from blastjob.models.config import BlastJobConfig
from blastjob.web import service


@pytest.fixture
def cfg() -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(conn, ext: str = "e1", **cols) -> int:
    jid = dbc.upsert_job(conn, source="s1", external_id=ext, company="Acme", title="VP IT")
    if cols:
        sets = ", ".join(f"{k}=?" for k in cols)
        conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), jid))
    conn.commit()
    return jid


def _kinds(conn, jid: int) -> list[tuple[str, str]]:
    return [(e["kind"], e["source"]) for e in events.events_for(conn, jid)]


# --- migrations ---------------------------------------------------------------


def test_migrations_42_and_43_create_both_tables_and_rerun_cleanly(cfg):
    versions = [v for v, _ in MIGRATIONS]
    assert 42 in versions and 43 in versions
    c = dbc.init_db(cfg)
    c.close()
    dbc.reset_migration_memo()
    c = dbc.init_db(cfg)  # second pass over an already-migrated DB
    tables = {
        r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"application_events", "artifacts"} <= tables
    for sql in (s for v, s in MIGRATIONS if v in (42, 43)):
        c.executescript(sql)  # IF NOT EXISTS everywhere: re-running is harmless
    c.close()


# --- core/events --------------------------------------------------------------


def test_add_event_validates_kind_date_and_source(conn):
    jid = _job(conn)
    assert events.add_event(conn, jid, "interview", "2026-10-01", note="panel") > 0
    with pytest.raises(ValueError):
        events.add_event(conn, jid, "lunch", "2026-10-01")
    with pytest.raises(ValueError):
        events.add_event(conn, jid, "note", "10/01/2026")
    with pytest.raises(ValueError):
        events.add_event(conn, jid, "note", "")
    with pytest.raises(ValueError):
        events.add_event(conn, jid, "note", "2026-10-01", source="robot")
    assert events.add_event(conn, jid, "note", "2026-10-01T09:30:00") > 0


def test_insert_event_does_not_commit(conn, cfg):
    jid = _job(conn)
    events.insert_event(conn, jid, "note", "2026-10-01")
    other = dbc.init_db(cfg)
    try:
        assert other.execute("SELECT COUNT(*) FROM application_events").fetchone()[0] == 0
    finally:
        other.close()
    conn.rollback()
    assert events.events_for(conn, jid) == []


def test_events_newest_first_with_id_breaking_a_timestamp_tie(conn):
    jid = _job(conn)
    first = events.add_event(conn, jid, "note", "2026-10-02", note="first")
    second = events.add_event(conn, jid, "note", "2026-10-02", note="second")
    events.add_event(conn, jid, "applied", "2026-09-01")
    got = events.events_for(conn, jid)
    assert [e["id"] for e in got[:2]] == [second, first]
    assert got[-1]["kind"] == "applied"


def test_a_dated_entry_is_not_older_than_a_timestamp_from_the_same_day(conn):
    """A thank-you the user logs for today (a bare date) was recorded after this
    morning's automated event; it must not sort below it."""
    jid = _job(conn)
    events.add_event(conn, jid, "interview", "2026-10-09T09:00:00", source="state")
    thanks = events.add_event(conn, jid, "thank_you", "2026-10-09")
    assert events.events_for(conn, jid)[0]["id"] == thanks
    assert events.latest_event(conn, [jid])[jid]["id"] == thanks


def test_latest_event_is_one_batch_and_breaks_ties_on_id(conn):
    a, b, c = _job(conn, "a"), _job(conn, "b"), _job(conn, "c")
    events.add_event(conn, a, "applied", "2026-09-01")
    events.add_event(conn, a, "screen", "2026-09-10")
    tie_1 = events.add_event(conn, b, "note", "2026-09-10")
    tie_2 = events.add_event(conn, b, "interview", "2026-09-10")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        latest = events.latest_event(conn, [a, b, c])
    finally:
        conn.set_trace_callback(None)
    assert len([s for s in statements if s.lstrip().upper().startswith("SELECT")]) == 1
    assert latest[a]["kind"] == "screen"
    assert latest[b]["id"] == tie_2 and tie_2 > tie_1
    assert c not in latest
    assert events.latest_event(conn, []) == {}


def test_delete_refuses_another_jobs_event_and_non_manual_events(conn):
    one, two = _job(conn, "one"), _job(conn, "two")
    theirs = events.add_event(conn, two, "note", "2026-10-01")
    stated = events.add_event(conn, one, "applied", "2026-10-01", source="state")
    mine = events.add_event(conn, one, "note", "2026-10-01")
    assert events.delete_manual_event(conn, one, theirs) is False
    assert events.delete_manual_event(conn, one, stated) is False
    assert events.delete_manual_event(conn, one, mine) is True
    assert [e["id"] for e in events.events_for(conn, two)] == [theirs]
    assert [e["id"] for e in events.events_for(conn, one)] == [stated]


# --- service.set_application_state (B2) ----------------------------------------


def test_state_change_writes_one_state_event(conn):
    jid = _job(conn, review_state="kept")
    assert service.set_application_state(conn, jid, "applied", ["", "drafted"])
    assert _kinds(conn, jid) == [("applied", "state")]
    assert service.set_application_state(conn, jid, "screen", ["applied"])
    assert _kinds(conn, jid)[0] == ("screen", "state")


def test_same_state_is_no_change_and_no_event(conn):
    jid = _job(conn, review_state="kept", application_state="screen")
    assert service.set_application_state(conn, jid, "screen", ["screen"]) is False
    assert events.events_for(conn, jid) == []


def test_drafted_and_cleared_states_write_no_event(conn):
    jid = _job(conn, review_state="kept")
    assert service.set_application_state(conn, jid, "drafted", [""])
    assert service.set_application_state(conn, jid, "", ["drafted"])
    assert events.events_for(conn, jid) == []


def test_refused_cas_writes_no_event(conn):
    jid = _job(conn, review_state="surfaced")
    assert service.set_application_state(conn, jid, "applied", [""]) is False
    assert events.events_for(conn, jid) == []


def test_a_failed_event_insert_rolls_the_state_change_back(conn, monkeypatch):
    jid = _job(conn, review_state="kept")

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(events, "insert_event", boom)
    with pytest.raises(sqlite3.OperationalError):
        service.set_application_state(conn, jid, "applied", [""])
    conn.commit()  # a later commit by the caller must not persist half of it
    row = conn.execute(
        "SELECT application_state, applied_at FROM jobs WHERE id=?", (jid,)
    ).fetchone()
    assert (row["application_state"] or "") == ""
    assert (row["applied_at"] or "") == ""


def test_returning_to_applied_is_a_note_not_a_second_application(conn):
    """Closed -> Applied (Reopen) must not claim he applied again today."""
    jid = _job(conn, review_state="kept")
    service.set_application_state(conn, jid, "applied", [""])
    service.set_application_state(conn, jid, "rejected", ["applied"])
    service.set_application_state(conn, jid, "applied", ["rejected"])
    got = events.events_for(conn, jid)
    assert [e["kind"] for e in got] == ["note", "rejected", "applied"]
    assert "applied" in got[0]["note"]
    assert got[0]["source"] == "state"


# --- closure lifecycle ---------------------------------------------------------


NOW = datetime(2026, 10, 9, 7, 30, 0)


def test_close_transition_emits_posting_closed_event(conn):
    jid = _job(conn, review_state="kept", posting_check="closed")
    assert closure.close_transition(conn, jid, now=NOW, auto=True)
    conn.commit()
    got = events.events_for(conn, jid)
    assert [(e["kind"], e["source"]) for e in got] == [("posting_closed", "closure")]
    assert got[0]["at"].startswith("2026-10-09")


def test_shadow_mode_close_emits_nothing(conn):
    jid = _job(conn, review_state="kept", posting_check="closed")
    assert closure.close_transition(conn, jid, now=NOW, auto=False) is False
    conn.commit()
    assert events.events_for(conn, jid) == []


def test_reopen_transition_emits_a_note(conn):
    jid = _job(conn, review_state="kept", posting_check="closed")
    closure.close_transition(conn, jid, now=NOW, auto=True)
    assert closure.reopen_transition(conn, jid, auto=True)
    conn.commit()
    got = events.events_for(conn, jid)
    assert got[0]["kind"] == "note" and got[0]["source"] == "closure"
    assert "reopened" in got[0]["note"].lower()


def test_sweep_filled_emits_one_filled_event_per_moved_job(conn):
    old = (NOW - timedelta(days=closure.FILLED_AFTER_DAYS + 1)).isoformat(timespec="seconds")
    common = dict(review_state="kept", application_state="applied", posting_check="closed")
    a = _job(conn, "a", posting_closed_at=old, **common)
    b = _job(conn, "b", posting_closed_at=old, **common)
    fresh = _job(conn, "c", posting_closed_at=NOW.isoformat(), **common)
    assert closure.sweep_filled(conn, now=NOW, auto=True) == 2
    for jid in (a, b):
        assert _kinds(conn, jid) == [("filled", "closure")]
    assert events.events_for(conn, fresh) == []


# --- recruiter replies ---------------------------------------------------------


def test_reply_scan_records_a_recruiter_reply_event_but_not_on_dry_run(conn, monkeypatch):
    import asyncio

    from blastjob.discovery import replies
    from blastjob.discovery.mailalerts import MailMessage

    jid = _job(
        conn,
        review_state="kept",
        application_state="applied",
        applied_at="2026-09-01",
        app_notes="recruiter jane@example.com",
    )
    monkeypatch.setenv("BJ_TEST_USER", "me@example.com")
    monkeypatch.setenv("BJ_TEST_PASS", "pw")
    cfg = BlastJobConfig()
    cfg.discovery.personal_mail_user_env = "BJ_TEST_USER"
    cfg.discovery.personal_mail_password_env = "BJ_TEST_PASS"

    msg = MailMessage(
        uid="7",
        message_id="<abc@example.com>",
        sender="Jane <jane@example.com>",
        subject="Re: VP IT",
        html="<p>Can we talk Tuesday?</p>",
    )

    def fetch(**_k):
        return [msg]

    def dates(**_k):
        return {"7": "2026-10-08"}

    def scan(record: bool):
        return asyncio.run(
            replies.scan_replies(
                cfg,
                conn,
                fetch_messages=fetch,
                fetch_dates=dates,
                today=date(2026, 10, 9),
                record=record,
            )
        )

    assert len(scan(record=False)) == 1
    assert events.events_for(conn, jid) == []
    assert len(scan(record=True)) == 1
    got = events.events_for(conn, jid)
    assert [(e["kind"], e["source"], e["ref"], e["at"]) for e in got] == [
        ("recruiter_reply", "replies", "<abc@example.com>", "2026-10-08")
    ]

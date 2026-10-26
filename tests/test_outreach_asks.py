"""Outreach asks: migration constraints and the core functions (#97)."""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from blastjob.core import outreach_asks as oa
from blastjob.db import client as dbc
from tests.asks_fixture import add_contact, add_job, add_network, make_cfg

TODAY = dt.date(2026, 10, 26)


@pytest.fixture
def conn():
    c = dbc.init_db(make_cfg())
    yield c
    c.close()


def _raw_insert(conn: sqlite3.Connection, **over: object) -> None:
    row: dict[str, object] = {
        "recipient_name": "Jo Poe",
        "kind": "intro",
        "channel": "email",
        "asked_on": "2026-10-20",
        "status": "waiting",
        "created_at": "x",
        "updated_at": "x",
        **over,
    }
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO outreach_asks ({cols}) VALUES ({marks})", list(row.values()))


def test_migration_is_65(conn):
    from blastjob.db.schema import MIGRATIONS

    assert MIGRATIONS[-1][0] == 65


def test_check_rejects_zero_recipients(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(conn)


def test_check_rejects_two_recipients(conn):
    job = add_job(conn, "1")
    n = add_network(conn, "Ann Lee")
    c = add_contact(conn, job)
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(conn, job_id=job, network_contact_id=n, contact_id=c)


@pytest.mark.parametrize("field,value", [("kind", "bogus"), ("channel", "fax"), ("status", "x")])
def test_enum_checks_hold(conn, field, value):
    n = add_network(conn, "Ann Lee")
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(conn, network_contact_id=n, **{field: value})


def _log(conn, **over):
    args = {
        "job_id": None,
        "network_contact_id": None,
        "contact_id": None,
        "kind": "intro",
        "channel": "email",
        "asked_on": "2026-10-20",
        "today": TODAY,
        **over,
    }
    return oa.log_ask(conn, **args)


def test_log_ask_default_follow_up_is_seven_days(conn):
    job = add_job(conn, "1")
    n = add_network(conn, "Ann Lee")
    ask = _log(conn, job_id=job, network_contact_id=n)
    row = oa.asks_for_job(conn, job)[0]
    assert row["id"] == ask and row["follow_up_on"] == "2026-10-27"
    assert row["status"] == "waiting" and row["recipient_name"] == "Ann Lee"


def test_log_ask_unknown_job(conn):
    n = add_network(conn, "Ann Lee")
    with pytest.raises(oa.AskError):
        _log(conn, job_id=999, network_contact_id=n)


def test_log_ask_contact_from_another_job(conn):
    a, b = add_job(conn, "1"), add_job(conn, "2", "Beta")
    c = add_contact(conn, b)
    with pytest.raises(oa.AskError):
        _log(conn, job_id=a, contact_id=c)


def test_log_ask_unknown_network_contact(conn):
    job = add_job(conn, "1")
    with pytest.raises(oa.AskError):
        _log(conn, job_id=job, network_contact_id=999)


def test_log_ask_network_contact_must_be_on_job_warm_path(conn):
    job = add_job(conn, "1", "Acme")
    elsewhere = add_network(conn, "Zed Kim", company="Beta Corp")
    with pytest.raises(oa.AskError):
        _log(conn, job_id=job, network_contact_id=elsewhere)


def test_log_ask_future_date(conn):
    n = add_network(conn, "Ann Lee")
    with pytest.raises(oa.AskError):
        _log(conn, network_contact_id=n, kind="advice", asked_on="2026-10-27")


def test_log_ask_note_over_300(conn):
    n = add_network(conn, "Ann Lee")
    with pytest.raises(oa.AskError):
        _log(conn, network_contact_id=n, kind="advice", note="x" * 301)


@pytest.mark.parametrize(
    "field,value", [("kind", "bogus"), ("channel", "fax"), ("asked_on", "10/20")]
)
def test_log_ask_rejects_bad_fields(conn, field, value):
    n = add_network(conn, "Ann Lee")
    over = {"kind": "advice", field: value}
    with pytest.raises(oa.AskError):
        _log(conn, network_contact_id=n, **over)


def test_follow_up_before_asked_on_rejected_but_past_timeline_allowed(conn):
    n = add_network(conn, "Ann Lee")
    with pytest.raises(oa.AskError):
        _log(conn, network_contact_id=n, kind="advice", follow_up_on="2026-10-19")
    # A backfilled ask whose whole timeline is in the past is fine.
    _log(
        conn, network_contact_id=n, kind="advice", asked_on="2026-09-01", follow_up_on="2026-09-08"
    )


def test_jobless_ask_needs_network_contact_and_recruiter_or_advice(conn):
    job = add_job(conn, "1")
    c = add_contact(conn, job)
    n = add_network(conn, "Ann Lee")
    with pytest.raises(oa.AskError):
        _log(conn, contact_id=c, kind="advice")  # job-less with a per-job contact
    with pytest.raises(oa.AskError):
        _log(conn, network_contact_id=n, kind="intro")  # job-less intro
    assert _log(conn, network_contact_id=n, kind="recruiter")


def test_contact_ask_sets_blank_contacted_at_and_keeps_existing(conn):
    job = add_job(conn, "1")
    c1, c2 = add_contact(conn, job, "Pat Roe"), add_contact(conn, job, "Sue Ode")
    conn.execute("UPDATE contacts SET contacted_at='2026-01-01T00:00:00' WHERE id=?", (c2,))
    conn.commit()
    _log(conn, job_id=job, contact_id=c1)
    _log(conn, job_id=job, contact_id=c2)
    rows = conn.execute("SELECT id, contacted_at FROM contacts").fetchall()
    got = {r["id"]: r["contacted_at"] for r in rows}
    assert got[c1] and got[c2] == "2026-01-01T00:00:00"


def test_request_token_replay_writes_one_row(conn):
    n = add_network(conn, "Ann Lee")
    first = _log(conn, network_contact_id=n, kind="advice", request_token="a" * 32)
    again = _log(conn, network_contact_id=n, kind="advice", request_token="a" * 32)
    assert first == again
    assert conn.execute("SELECT COUNT(*) FROM outreach_asks").fetchone()[0] == 1


def test_update_closing_status_clears_follow_up(conn):
    n = add_network(conn, "Ann Lee")
    ask = _log(conn, network_contact_id=n, kind="advice")
    oa.update_ask(conn, ask, status="helped", follow_up_on="2026-11-01", note="ok")
    row = conn.execute("SELECT * FROM outreach_asks WHERE id=?", (ask,)).fetchone()
    assert row["status"] == "helped" and row["follow_up_on"] == "" and row["note"] == "ok"


def test_update_validates_date_and_note(conn):
    n = add_network(conn, "Ann Lee")
    ask = _log(conn, network_contact_id=n, kind="advice")
    with pytest.raises(oa.AskError):
        oa.update_ask(conn, ask, status="waiting", follow_up_on="2026-10-01", note="")
    with pytest.raises(oa.AskError):
        oa.update_ask(conn, ask, status="waiting", follow_up_on="", note="x" * 301)
    with pytest.raises(oa.AskError):
        oa.update_ask(conn, 999, status="waiting", follow_up_on="", note="")


def test_newest_ask_is_by_asked_on_then_id(conn):
    job = add_job(conn, "1")
    n = add_network(conn, "Ann Lee")
    newer = _log(conn, job_id=job, network_contact_id=n, asked_on="2026-10-20")
    _log(conn, job_id=job, network_contact_id=n, asked_on="2026-10-01")  # entered later, older
    rows = oa.asks_for_job(conn, job)
    assert rows[0]["id"] == newer
    latest = oa.latest_by_recipient(rows)
    assert latest.network[n]["id"] == newer


def test_due_rule_and_ordering(conn):
    hi = add_job(conn, "1", "Acme", 90)
    lo = add_job(conn, "2", "Acme", 60)
    n1, n2, n3 = (add_network(conn, f"P{i} Lee") for i in range(3))
    older = _log(
        conn, job_id=lo, network_contact_id=n1, asked_on="2026-10-01", follow_up_on="2026-10-08"
    )
    hi_ask = _log(
        conn, job_id=hi, network_contact_id=n2, asked_on="2026-10-10", follow_up_on="2026-10-17"
    )
    lo_ask = _log(
        conn, job_id=lo, network_contact_id=n3, asked_on="2026-10-10", follow_up_on="2026-10-17"
    )
    future = _log(conn, job_id=hi, network_contact_id=n1, follow_up_on="2026-11-30")
    closed = _log(
        conn, job_id=hi, network_contact_id=n2, asked_on="2026-10-02", follow_up_on="2026-10-09"
    )
    oa.update_ask(conn, closed, status="no_reply", follow_up_on="", note="")
    undated = _log(conn, job_id=hi, network_contact_id=n3)
    conn.execute("UPDATE outreach_asks SET follow_up_on='' WHERE id=?", (undated,))
    conn.commit()
    ids = [r["id"] for r in oa.due_followups(conn, TODAY)]
    assert ids == [older, hi_ask, lo_ask]
    assert future not in ids and closed not in ids and undated not in ids


def test_jobless_ask_is_due_and_sorts_after_scored(conn):
    job = add_job(conn, "1", "Acme", 10)
    n = add_network(conn, "Ann Lee")
    m = add_network(conn, "Bo Lee", company="Beta Corp")
    scored = _log(
        conn, job_id=job, network_contact_id=n, asked_on="2026-10-10", follow_up_on="2026-10-17"
    )
    jobless = _log(
        conn,
        network_contact_id=m,
        kind="advice",
        asked_on="2026-10-10",
        follow_up_on="2026-10-17",
    )
    rows = oa.due_followups(conn, TODAY)
    assert [r["id"] for r in rows] == [scored, jobless]
    assert rows[1]["job_id"] is None and rows[1]["network_company"] == "Beta Corp"
    assert rows[0]["days_overdue"] == 9


def test_validate_recipient_shared_rules(conn):
    job = add_job(conn, "1")
    n = add_network(conn, "Ann Lee")
    got = oa.validate_recipient(conn, job_id=job, network_contact_id=n, contact_id=None)
    assert got.name == "Ann Lee" and got.network_contact_id == n
    with pytest.raises(oa.AskError):
        oa.validate_recipient(conn, job_id=job, network_contact_id=n, contact_id=n)
    with pytest.raises(oa.AskError):
        oa.validate_recipient(conn, job_id=job, network_contact_id=None, contact_id=None)


def test_token_replay_for_another_job_or_person_is_rejected(conn):
    a, b = add_job(conn, "1"), add_job(conn, "2")
    n = add_network(conn, "Ann Lee")
    m = add_network(conn, "Bo Lee")
    token = "c3" * 16
    _log(conn, job_id=a, network_contact_id=n, request_token=token)
    with pytest.raises(oa.AskError):
        _log(conn, job_id=b, network_contact_id=n, request_token=token)
    with pytest.raises(oa.AskError):
        _log(conn, job_id=a, network_contact_id=m, request_token=token)
    assert conn.execute("SELECT COUNT(*) FROM outreach_asks").fetchone()[0] == 1


def test_a_check_failure_becomes_an_ask_error_not_a_500(conn, monkeypatch):
    n = add_network(conn, "Ann Lee")
    monkeypatch.setattr(oa, "_clean_note", lambda note: "x" * 301)  # trips the table CHECK
    with pytest.raises(oa.AskError):
        _log(conn, network_contact_id=n, kind="advice")
    assert conn.execute("SELECT COUNT(*) FROM outreach_asks").fetchone()[0] == 0

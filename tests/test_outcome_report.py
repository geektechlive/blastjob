"""Closing the outcome loop: stage moves that record what happened to a sent
packet, and the descriptive report built on top of it.

`core/packet_sends.py` already snapshots WHICH documents went out (migrations
39/40, `web.stage_moves.mark_applied`). This file tests the other half: every
subsequent stage move (advance, withdraw, reopen) appends an outcome event to
that snapshot, best-effort and never blocking the move itself -- and
`outcome_report`/`job_event_log` turn that log into what the user actually
reads.

Closure transitions (rejected/ghosted/posting_closed/filled set via
`core/closure.py` and the manual status route in `web/app.py`) are not owned
by this workstream and are delivered as an INTEGRATION PATCH; the tests here
exercise `packet_sends.record_outcome_for_job` directly with each of those
outcome names to pin the contract those call sites depend on.
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from blastjob.core import packet_sends as ps
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import stage_moves


@pytest.fixture
def cfg() -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    return c


@pytest.fixture
def conn(cfg) -> sqlite3.Connection:
    c = dbc.init_db(cfg)
    yield c
    c.close()


_seq = iter(range(1, 1_000_000))


def _job(conn: sqlite3.Connection, **cols) -> int:
    n = next(_seq)
    job_id = dbc.upsert_job(
        conn,
        source=cols.pop("source", f"s{n}"),
        external_id=f"e{n}",
        company=cols.pop("company", f"Acme {n}"),
        title=cols.pop("title", "VP of Ops"),
    )
    if cols:
        sets = ", ".join(f"{k}=?" for k in cols)
        conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), job_id))
    conn.commit()
    return job_id


def _applied_job(conn: sqlite3.Connection, **cols) -> int:
    defaults = {"review_state": "kept", "application_state": "applied", "applied_at": "2026-09-01"}
    defaults.update(cols)
    return _job(conn, **defaults)


def _send(conn: sqlite3.Connection, tmp_path: Path, job_id: int, **kwargs) -> int:
    resume = tmp_path / f"resume-{job_id}.md"
    resume.write_text(f"resume for job {job_id}", encoding="utf-8")
    return ps.record_send(conn, job_id, str(tmp_path), resume, **kwargs)


# --- stage_moves wiring -------------------------------------------------


def test_advance_records_screen_outcome(conn, tmp_path):
    job_id = _applied_job(conn)
    send_id = _send(conn, tmp_path, job_id)

    assert stage_moves.advance(conn, job_id) is True

    row = conn.execute("SELECT * FROM packet_outcomes WHERE send_id=?", (send_id,)).fetchone()
    assert row["outcome"] == "screen"


def test_full_chain_applied_to_offer_records_outcomes_in_order(conn, tmp_path):
    job_id = _applied_job(conn)
    send_id = _send(conn, tmp_path, job_id)

    assert stage_moves.advance(conn, job_id) is True  # -> screen
    assert stage_moves.advance(conn, job_id) is True  # -> interview
    assert stage_moves.advance(conn, job_id) is True  # -> offer
    # There is no stage after offer.
    assert stage_moves.advance(conn, job_id) is False

    rows = conn.execute(
        "SELECT outcome FROM packet_outcomes WHERE send_id=? ORDER BY id", (send_id,)
    ).fetchall()
    assert [r["outcome"] for r in rows] == ["screen", "interview", "offer"]

    job = conn.execute("SELECT application_state FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert job["application_state"] == "offer"


def test_withdraw_records_withdrawn_outcome(conn, tmp_path):
    job_id = _applied_job(conn)
    send_id = _send(conn, tmp_path, job_id)

    assert stage_moves.withdraw(conn, job_id) is True

    row = conn.execute("SELECT * FROM packet_outcomes WHERE send_id=?", (send_id,)).fetchone()
    assert row["outcome"] == "withdrawn"


def test_reopen_records_applied_outcome_on_the_existing_send(conn, tmp_path):
    job_id = _applied_job(conn, application_state="withdrawn")
    send_id = _send(conn, tmp_path, job_id)
    ps.record_outcome(conn, send_id, "withdrawn")

    assert stage_moves.reopen(conn, job_id) is True

    rows = conn.execute(
        "SELECT outcome FROM packet_outcomes WHERE send_id=? ORDER BY id", (send_id,)
    ).fetchall()
    assert [r["outcome"] for r in rows] == ["withdrawn", "applied"]
    job = conn.execute("SELECT application_state FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert job["application_state"] == "applied"


def test_reopen_to_shortlist_records_nothing_no_prior_send_to_touch(conn):
    # A closed job that never actually applied (applied_at empty) goes back
    # to the Shortlist, not Applied -- there is no send to attach an outcome
    # to, and "" is not a valid packet_outcomes value.
    job_id = _job(conn, review_state="kept", application_state="posting_closed", applied_at="")

    assert stage_moves.reopen(conn, job_id) is True
    job = conn.execute("SELECT application_state FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert job["application_state"] == ""
    count = conn.execute("SELECT COUNT(*) AS n FROM packet_outcomes").fetchone()
    assert count["n"] == 0


def test_a_move_on_a_job_with_no_send_is_a_silent_noop_that_still_moves_the_job(conn):
    # No _send() call here -- this job was marked applied before packet_sends
    # existed, or the packet-existence check in mark_applied declined to
    # snapshot one. The move must not raise and must still happen.
    job_id = _applied_job(conn)

    assert stage_moves.advance(conn, job_id) is True

    job = conn.execute("SELECT application_state FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert job["application_state"] == "screen"
    count = conn.execute("SELECT COUNT(*) AS n FROM packet_outcomes").fetchone()
    assert count["n"] == 0


# --- closure-transition contract (integration patch calls this directly) --


@pytest.mark.parametrize("outcome", ["rejected", "ghosted", "posting_closed", "filled"])
def test_closure_transition_outcomes_record_against_the_existing_send(conn, tmp_path, outcome):
    """core/closure.py and web/app.py's manual status route are not owned by
    this workstream (see the INTEGRATION PATCH); both are meant to call
    `packet_sends.record_outcome_for_job` the same way stage_moves does. This
    pins that every closure-lifecycle outcome name is accepted."""
    job_id = _applied_job(conn)
    send_id = _send(conn, tmp_path, job_id)

    result = ps.record_outcome_for_job(conn, job_id, outcome)

    assert result is not None
    row = conn.execute("SELECT outcome FROM packet_outcomes WHERE send_id=?", (send_id,)).fetchone()
    assert row["outcome"] == outcome


# --- outcome_report / job_event_log --------------------------------------


def test_outcome_report_includes_documents_scores_furthest_stage_and_durations(conn, tmp_path):
    job_id = _applied_job(conn)
    send_id = _send(
        conn,
        tmp_path,
        job_id,
        now=datetime(2026, 9, 1, 9, 0, 0),
        groundedness=88,
        jd_phrasing=70,
    )
    # Direct inserts to control observed_at for a deterministic duration
    # check -- record_outcome always stamps "now".
    conn.execute(
        "INSERT INTO packet_outcomes (send_id, outcome, observed_at, note) VALUES (?,?,?,?)",
        (send_id, "screen", "2026-09-05T09:00:00", ""),
    )
    conn.execute(
        "INSERT INTO packet_outcomes (send_id, outcome, observed_at, note) VALUES (?,?,?,?)",
        (send_id, "interview", "2026-09-10T09:00:00", ""),
    )
    conn.commit()

    report = ps.outcome_report(conn)
    assert len(report) == 1
    entry = report[0]

    assert entry["job_id"] == job_id
    assert entry["company"].startswith("Acme")
    assert entry["title"] == "VP of Ops"
    assert entry["resume_path"]
    assert entry["groundedness"] == 88
    assert entry["jd_phrasing"] == 70
    assert entry["furthest_stage"] == "interview"

    durations = {d["stage"]: d["days"] for d in entry["stage_durations"]}
    assert durations["applied"] == 4.0
    assert durations["screen"] == 5.0
    # "interview" is the open-ended last stage; just confirm it is present
    # and non-negative, since it runs to "now" (real wall-clock time).
    assert durations["interview"] >= 0.0


def test_job_event_log_reflects_a_full_stage_moves_chain(conn, tmp_path):
    job_id = _applied_job(conn)
    _send(conn, tmp_path, job_id)

    stage_moves.advance(conn, job_id)  # -> screen
    stage_moves.advance(conn, job_id)  # -> interview
    stage_moves.withdraw(conn, job_id)  # -> withdrawn (withdraw is valid from any in-process state)

    log = ps.job_event_log(conn, job_id)
    assert [e["stage"] for e in log] == ["applied", "screen", "interview", "withdrawn"]
    assert log == sorted(log, key=lambda e: e["at"])

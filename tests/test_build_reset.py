"""Tests for manual build-state reset and stranded-build recovery.

Covers two defects in the tailor-on-keep pipeline:
- reset_build_state: the owner wants to re-tailor a job that already
  tailored (or failed) without waiting out any lease, but a build that is
  genuinely in flight must not be yanked out from under it.
- recover_stale_builds: a KeepAlive service restart mid-build
  must not strand a job at 'tailoring' forever -- a build_runs row owned by
  a PREVIOUS process is dead the instant a new process starts, lease or no
  lease; only a row owned by THIS process needs its lease to actually expire
  before it's considered orphaned.

All times are injected (`now=`) so nothing here depends on wall-clock timing.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta

import pytest

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig

NOW = datetime(2026, 9, 12, 12, 0, 0)


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _insert_run(conn, job_id, run_id, *, status, lease_expires_at, owner_token):
    conn.execute(
        "INSERT INTO build_runs "
        "(job_id, run_id, status, lease_expires_at, created_at, updated_at, owner_token) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, run_id, status, lease_expires_at, NOW.isoformat(), NOW.isoformat(), owner_token),
    )
    conn.commit()


# ---------- reset_build_state ----------


def test_reset_build_state_from_tailored(conn):
    jid = dbc.upsert_job(conn, source="s", external_id="e", title="t")
    conn.execute("UPDATE jobs SET build_state='tailored' WHERE id=?", (jid,))
    conn.commit()

    assert dbc.reset_build_state(conn, jid, now=NOW) is True
    assert (
        conn.execute("SELECT build_state FROM jobs WHERE id=?", (jid,)).fetchone()["build_state"]
        == "none"
    )


def test_reset_build_state_from_failed(conn):
    jid = dbc.upsert_job(conn, source="s", external_id="e", title="t")
    conn.execute("UPDATE jobs SET build_state='failed' WHERE id=?", (jid,))
    conn.commit()

    assert dbc.reset_build_state(conn, jid, now=NOW) is True
    assert (
        conn.execute("SELECT build_state FROM jobs WHERE id=?", (jid,)).fetchone()["build_state"]
        == "none"
    )


def test_reset_build_state_allows_expired_lease_tailoring_row(conn):
    jid = dbc.upsert_job(conn, source="s", external_id="e", title="t")
    conn.execute("UPDATE jobs SET build_state='tailoring' WHERE id=?", (jid,))
    conn.commit()
    expired_lease = (NOW - timedelta(minutes=1)).isoformat()
    _insert_run(
        conn,
        jid,
        "run-expired",
        status="running",
        lease_expires_at=expired_lease,
        owner_token=dbc.PROCESS_TOKEN,
    )

    assert dbc.reset_build_state(conn, jid, now=NOW) is True
    row = conn.execute("SELECT build_state FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["build_state"] == "none"
    run_row = conn.execute("SELECT status FROM build_runs WHERE run_id='run-expired'").fetchone()
    assert run_row["status"] == "failed"

    # and it's re-claimable afterward
    assert dbc.claim_for_build(conn, jid, "run-retry") is True


def test_reset_build_state_refuses_live_lease_tailoring_row(conn):
    jid = dbc.upsert_job(conn, source="s", external_id="e", title="t")
    conn.execute("UPDATE jobs SET build_state='tailoring' WHERE id=?", (jid,))
    conn.commit()
    live_lease = (NOW + timedelta(minutes=5)).isoformat()
    _insert_run(
        conn,
        jid,
        "run-live",
        status="running",
        lease_expires_at=live_lease,
        owner_token=dbc.PROCESS_TOKEN,
    )

    assert dbc.reset_build_state(conn, jid, now=NOW) is False
    row = conn.execute("SELECT build_state FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["build_state"] == "tailoring"


def test_reset_build_state_refuses_missing_job(conn):
    assert dbc.reset_build_state(conn, 999_999, now=NOW) is False


def test_reset_build_state_noop_when_already_none(conn):
    jid = dbc.upsert_job(conn, source="s", external_id="e", title="t")
    assert dbc.reset_build_state(conn, jid, now=NOW) is False


# ---------- recover_stale_builds: process-owner recovery ----------


def test_recover_stale_builds_clears_previous_process_row_even_with_live_lease(conn):
    jid = dbc.upsert_job(conn, source="s", external_id="e", title="t")
    conn.execute("UPDATE jobs SET build_state='tailoring' WHERE id=?", (jid,))
    conn.commit()
    live_lease = (NOW + timedelta(minutes=15)).isoformat()
    _insert_run(
        conn,
        jid,
        "run-prev-process",
        status="running",
        lease_expires_at=live_lease,
        owner_token="a-previous-process-token",
    )

    n = dbc.recover_stale_builds(conn, now=NOW)

    assert n == 1
    assert (
        conn.execute("SELECT build_state FROM jobs WHERE id=?", (jid,)).fetchone()["build_state"]
        == "failed"
    )
    assert (
        conn.execute("SELECT status FROM build_runs WHERE run_id='run-prev-process'").fetchone()[
            "status"
        ]
        == "failed"
    )


def test_recover_stale_builds_leaves_current_process_live_lease_alone(conn):
    jid = dbc.upsert_job(conn, source="s", external_id="e", title="t")
    conn.execute("UPDATE jobs SET build_state='tailoring' WHERE id=?", (jid,))
    conn.commit()
    live_lease = (NOW + timedelta(minutes=15)).isoformat()
    _insert_run(
        conn,
        jid,
        "run-current-process",
        status="running",
        lease_expires_at=live_lease,
        owner_token=dbc.PROCESS_TOKEN,
    )

    n = dbc.recover_stale_builds(conn, now=NOW)

    assert n == 0
    assert (
        conn.execute("SELECT build_state FROM jobs WHERE id=?", (jid,)).fetchone()["build_state"]
        == "tailoring"
    )
    assert (
        conn.execute("SELECT status FROM build_runs WHERE run_id='run-current-process'").fetchone()[
            "status"
        ]
        == "running"
    )

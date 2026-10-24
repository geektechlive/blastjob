"""Applications by channel (#81): counts only, cumulative, no rates."""

from __future__ import annotations

import tempfile

import pytest

from blastjob.core import events
from blastjob.core.channel_counts import ChannelRow, channel_counts
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.models.tracking import APPLICATION_CHANNELS


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(conn, ext, *, channel=None, applied="2026-01-05", state="applied") -> int:
    jid = dbc.upsert_job(conn, source="a", external_id=ext, company="Acme", title="VP IT")
    conn.execute(
        "UPDATE jobs SET app_channel=?, applied_at=?, application_state=? WHERE id=?",
        (channel, applied, state, jid),
    )
    conn.commit()
    return jid


def _by_key(rows: list[ChannelRow]) -> dict[str, ChannelRow]:
    return {r.key: r for r in rows}


def test_rows_follow_channel_order_then_unknown(conn):
    rows = channel_counts(conn)
    assert [r.key for r in rows] == [*APPLICATION_CHANNELS, "unknown"]
    assert rows[-1].label == "Unknown"
    assert all(r.applications == 0 for r in rows)


def test_counts_per_channel_including_unknown(conn):
    _job(conn, "1", channel="cold")
    _job(conn, "2", channel="cold", state="screen")
    _job(conn, "3", channel="referral", state="interview")
    _job(conn, "4", channel=None)
    _job(conn, "5", channel=None, state="screen")
    by = _by_key(channel_counts(conn))
    assert (by["cold"].applications, by["cold"].replied, by["cold"].interviewed) == (2, 1, 0)
    assert (by["referral"].applications, by["referral"].replied) == (1, 1)
    assert by["referral"].interviewed == 1
    assert (by["unknown"].applications, by["unknown"].replied) == (2, 1)
    assert by["recruiter"].applications == 0


def test_unapplied_jobs_are_excluded(conn):
    _job(conn, "1", channel="cold", applied="", state="drafted")
    _job(conn, "2", channel="cold", applied=None, state="")
    assert _by_key(channel_counts(conn))["cold"].applications == 0


def test_later_state_counts_in_earlier_columns(conn):
    _job(conn, "1", channel="recruiter", state="offer")
    row = _by_key(channel_counts(conn))["recruiter"]
    assert (row.applications, row.replied, row.interviewed) == (1, 1, 1)


def test_events_keep_progress_after_a_rejection(conn):
    jid = _job(conn, "1", channel="inbound", state="rejected")
    events.add_event(conn, jid, "interview", "2026-01-20")
    row = _by_key(channel_counts(conn))["inbound"]
    assert (row.applications, row.replied, row.interviewed) == (1, 1, 1)


def test_recruiter_reply_event_counts_as_replied_only(conn):
    jid = _job(conn, "1", channel="recruiter", state="applied")
    events.add_event(conn, jid, "recruiter_reply", "2026-01-12")
    row = _by_key(channel_counts(conn))["recruiter"]
    assert (row.replied, row.interviewed) == (1, 0)


def test_bare_rejection_is_not_a_reply(conn):
    _job(conn, "1", channel="cold", state="rejected")
    _job(conn, "2", channel="cold", state="ghosted")
    row = _by_key(channel_counts(conn))["cold"]
    assert (row.applications, row.replied, row.interviewed) == (2, 0, 0)

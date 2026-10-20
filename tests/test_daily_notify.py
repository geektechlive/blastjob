"""Regression tests for the digest-overflow stamping bug.

`build_digest` only ever renders `MAX_DIGEST_JOBS` (25) roles, but `run_daily`
used to call `mark_notified` on *every* pending job regardless of whether it
was actually shown. Since the digest only ever selects `notified_at=''` rows,
anything past the cutoff was permanently buried the moment it was stamped.

These tests pin two things: (1) only the jobs actually rendered in the sent
digest get stamped, so overflow leads next run's digest, and (2) the overflow
line points at a cockpit view that actually contains the overflow (`/all`,
which lists every active job) rather than `/` (surfaced-only).
"""

from __future__ import annotations

import datetime as dt
import tempfile

import httpx
import pytest

from blastjob import daily
from blastjob.db import client as dbc
from blastjob.discovery.pipeline import DiscoveryResult
from blastjob.llm.cost import CallCost
from blastjob.models.config import BlastJobConfig

N_JOBS = 30


@pytest.fixture
def db_many():
    """30 surfaced jobs with distinct descending scores, so ordering is
    unambiguous: id order == score order == digest order (no warm paths)."""
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)
    ids = []
    for i in range(N_JOBS):
        jid = dbc.upsert_job(
            conn,
            source="t",
            external_id=str(i),
            company=f"Company{i}",
            title="Role",
            url=f"https://x/{i}",
        )
        ids.append(jid)
        score = N_JOBS - i  # 30, 29, ..., 1 -> descending, unique
        conn.execute(
            "UPDATE jobs SET review_state='surfaced', alignment_score=? WHERE id=?",
            (score, jid),
        )
    conn.commit()
    yield cfg, conn, ids
    conn.close()


def _stub_pipeline(monkeypatch, errors=None):
    async def fake_discovery(cfg, conn, adapters=None):
        return DiscoveryResult(fetched=0, per_source={}, errors=dict(errors or {}))

    class M:
        cost = CallCost()
        scored = 0
        surfaced = 0
        queued = 0
        scoring_failures = 0

    async def fake_matching(cfg, conn, tracker, on_progress=None, **kw):
        return M()

    import blastjob.discovery.pipeline as dp
    import blastjob.matching.pipeline as mp

    monkeypatch.setattr(dp, "run_discovery", fake_discovery)
    monkeypatch.setattr(mp, "run_matching", fake_matching)


@pytest.mark.asyncio
async def test_only_rendered_jobs_are_stamped_overflow_leads_next_digest(db_many, monkeypatch):
    cfg, conn, ids = db_many
    _stub_pipeline(monkeypatch)
    sent: list[str] = []

    async def ok(text):
        sent.append(text)

    res = await daily.run_daily(cfg, conn, send=ok, today=dt.date(2026, 9, 8))
    assert res.sent
    assert res.new_jobs == N_JOBS  # digest reports the true pending count

    stamped_ids = {
        r["id"] for r in conn.execute("SELECT id FROM jobs WHERE notified_at<>''").fetchall()
    }
    assert len(stamped_ids) == daily.MAX_DIGEST_JOBS

    # The 25 highest-scoring jobs (first 25 ids, since score descends with id)
    # were rendered and stamped; the bottom 5 were not.
    top_25 = set(ids[: daily.MAX_DIGEST_JOBS])
    overflow_5 = set(ids[daily.MAX_DIGEST_JOBS :])
    assert stamped_ids == top_25
    assert stamped_ids.isdisjoint(overflow_5)

    # Second run: no new discovery, so pending_jobs() now returns exactly the
    # 5 previously-buried jobs, and this time all 5 fit and get stamped.
    res2 = await daily.run_daily(cfg, conn, send=ok, today=dt.date(2026, 9, 9))
    assert res2.sent
    assert res2.new_jobs == 5
    assert overflow_5 <= {
        r["id"] for r in conn.execute("SELECT id FROM jobs WHERE notified_at<>''").fetchall()
    }
    all_stamped = {
        r["id"] for r in conn.execute("SELECT id FROM jobs WHERE notified_at<>''").fetchall()
    }
    assert all_stamped == set(ids)


@pytest.mark.asyncio
async def test_failed_send_stamps_nothing_even_with_overflow(db_many, monkeypatch):
    cfg, conn, _ids = db_many
    _stub_pipeline(monkeypatch)

    async def boom(text):
        raise httpx.HTTPError("discord down")

    with pytest.raises(httpx.HTTPError):
        await daily.run_daily(cfg, conn, send=boom, today=dt.date(2026, 9, 8))
    stamped = conn.execute("SELECT COUNT(*) n FROM jobs WHERE notified_at<>''").fetchone()["n"]
    assert stamped == 0


@pytest.mark.asyncio
async def test_dry_run_stamps_nothing_even_with_overflow(db_many, monkeypatch):
    cfg, conn, _ids = db_many
    _stub_pipeline(monkeypatch)

    async def spy(text):
        pass

    res = await daily.run_daily(cfg, conn, send=spy, dry_run=True, today=dt.date(2026, 9, 8))
    assert res.digest and not res.sent
    stamped = conn.execute("SELECT COUNT(*) n FROM jobs WHERE notified_at<>''").fetchone()["n"]
    assert stamped == 0


def test_overflow_line_present_and_points_at_all_view_not_root(db_many):
    _, conn, _ids = db_many
    text = daily.build_digest(daily.pending_jobs(conn), {}, [])
    assert f"…and {N_JOBS - daily.MAX_DIGEST_JOBS} more in the cockpit" in text
    assert "<http://127.0.0.1:8765/all>" in text
    assert "<http://127.0.0.1:8765/>" not in text


def test_no_overflow_line_when_everything_fits(db_many):
    _, conn, _ids = db_many
    jobs = daily.pending_jobs(conn)[: daily.MAX_DIGEST_JOBS]  # exactly at the cutoff
    text = daily.build_digest(jobs, {}, [])
    assert "more in the cockpit" not in text
    assert daily.build_digest([], {}, []) == ""  # trivial empty case still has no line

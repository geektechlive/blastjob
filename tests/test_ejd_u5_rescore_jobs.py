"""Employer-JD U5: batched re-scoring of many jobs and the stale-kept retry list."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from blastjob.db import client as dbc
from blastjob.llm.cost import CostTracker
from blastjob.matching import pipeline, rescore
from blastjob.matching.scorer import ScoringResult
from blastjob.models.config import BlastJobConfig
from tests.geo_fixture import apply_region
from tests.profile_fixture import apply_profile

JD = "Director of Technology. " + "Own MSP governance, data strategy and AI. " * 40


@pytest.fixture
def cfg(tmp_path) -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = str(tmp_path)
    apply_region(c)
    apply_profile(c)
    (tmp_path / "work_history.md").write_text("# History\n", encoding="utf-8")
    return c


@pytest.fixture
def conn(cfg) -> sqlite3.Connection:
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(conn, review_state: str, n: int, stale: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (source, external_id, company, title, raw_jd, alignment_score, "
        "rationale, review_state, geo_location_raw, geo_remote, first_seen, last_seen, "
        "score_stale) VALUES ('adzuna', ?, ?, 'Director of Technology', ?, 62, 'old', ?, "
        "'Fairhaven, OH', 0, '2026-10-01', '2026-10-01', ?)",
        (f"x-{n}", f"Acme{n}", JD + str(n), review_state, stale),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _row(conn, jid):
    return conn.execute(
        "SELECT alignment_score, rationale, review_state, score_stale FROM jobs WHERE id=?",
        (jid,),
    ).fetchone()


def _scorer(alignment: int = 84, fail_ids: frozenset[int] = frozenset(), raise_ids=frozenset()):
    async def fake(cfg, wh, candidates, cost_tracker, **kw):
        out = ScoringResult()
        for c in candidates:
            if c["job_id"] in raise_ids:
                raise RuntimeError("provider down")
            if c["job_id"] in fail_ids:
                continue
            out.scores[c["job_id"]] = {
                "alignment": alignment,
                "rationale": "full posting read",
                "strengths": ["a"],
                "gaps": ["b"],
                "comp_min": 0,
                "comp_max": 0,
            }
        return out

    return fake


def test_undecided_rows_cost_one_run_matching_call(cfg, conn, monkeypatch):
    ids = [_job(conn, "new", n) for n in range(3)]
    calls: list[list[int]] = []
    real = pipeline.run_matching

    async def spy(cfg_, conn_, ct, **kw):
        calls.append(list(kw["job_ids"]))
        return await real(cfg_, conn_, ct, **kw)

    monkeypatch.setattr(pipeline, "run_matching", spy)
    monkeypatch.setattr(pipeline, "score_candidates", _scorer())
    counts = asyncio.run(rescore.rescore_jobs(cfg, conn, CostTracker(), ids))
    assert calls == [ids]
    assert counts.undecided == 3 and counts.kept == 0 and counts.failed == []


def test_kept_rows_are_scored_without_touching_review_state(cfg, conn, monkeypatch):
    jid = _job(conn, "kept", 1, stale=1)
    monkeypatch.setattr(rescore, "score_candidates", _scorer(alignment=88))
    counts = asyncio.run(rescore.rescore_jobs(cfg, conn, CostTracker(), [jid]))
    r = _row(conn, jid)
    assert (r["review_state"], r["alignment_score"]) == ("kept", 88)
    assert counts.kept == 1 and counts.undecided == 0 and counts.failed == []


def test_successful_kept_score_clears_score_stale(cfg, conn, monkeypatch):
    jid = _job(conn, "kept", 1, stale=1)
    monkeypatch.setattr(rescore, "score_candidates", _scorer())
    asyncio.run(rescore.rescore_jobs(cfg, conn, CostTracker(), [jid]))
    assert _row(conn, jid)["score_stale"] == 0


def test_archived_rows_are_skipped(cfg, conn, monkeypatch):
    jid = _job(conn, "archived", 1)
    called: list[int] = []

    async def spy(*a, **kw):
        called.append(1)
        return ScoringResult()

    monkeypatch.setattr(rescore, "score_candidates", spy)
    monkeypatch.setattr(pipeline, "score_candidates", spy)
    counts = asyncio.run(rescore.rescore_jobs(cfg, conn, CostTracker(), [jid]))
    assert called == [] and _row(conn, jid)["alignment_score"] == 62
    assert (counts.undecided, counts.kept, counts.failed) == (0, 0, [])


def test_kept_scoring_failure_stays_stale_and_is_listed(cfg, conn, monkeypatch):
    bad = _job(conn, "kept", 1, stale=1)
    boom = _job(conn, "kept", 2, stale=1)
    good = _job(conn, "kept", 3, stale=1)
    monkeypatch.setattr(
        rescore, "score_candidates", _scorer(fail_ids=frozenset({bad}), raise_ids=frozenset({boom}))
    )
    counts = asyncio.run(rescore.rescore_jobs(cfg, conn, CostTracker(), [bad, boom, good]))
    assert counts.failed == [bad, boom] and counts.kept == 1
    assert _row(conn, bad)["score_stale"] == 1 and _row(conn, bad)["alignment_score"] == 62
    assert _row(conn, boom)["score_stale"] == 1
    assert _row(conn, good)["score_stale"] == 0


def test_stale_kept_ids_orders_by_id_and_honours_limit(conn):
    a = _job(conn, "kept", 1, stale=1)
    _job(conn, "kept", 2, stale=0)
    _job(conn, "new", 3, stale=1)  # undecided rows are not this list's business
    b = _job(conn, "kept", 4, stale=1)
    c = _job(conn, "kept", 5, stale=1)
    assert rescore.stale_kept_ids(conn) == [a, b, c]
    assert rescore.stale_kept_ids(conn, limit=2) == [a, b]


def test_kept_rows_refused_by_the_call_budget_are_not_run_not_failed(cfg, conn, monkeypatch):
    from blastjob.llm import gateway

    jid = _job(conn, "kept", 1, stale=1)

    async def refused(cfg_, wh, candidates, cost_tracker, **kw):
        try:
            raise gateway.BudgetExceeded("daily model call limit reached")
        except gateway.BudgetExceeded:
            gateway._mark_refused("budget")
        return ScoringResult()

    monkeypatch.setattr(rescore, "score_candidates", refused)
    with gateway.deadline_scope(600):
        counts = asyncio.run(rescore.rescore_jobs(cfg, conn, CostTracker(), [jid]))

    assert counts.failed == [] and counts.not_run == [jid]


def test_kept_rows_refused_by_the_deadline_are_not_run_not_failed(cfg, conn, monkeypatch):
    """A refused call (the scorer swallows it as a missing score) must not count as a failure
    or stamp the row as attempted, so it keeps its place in the stale-kept rotation."""
    from blastjob.llm import gateway

    jid = _job(conn, "kept", 1, stale=1)

    async def refused(cfg_, wh, candidates, cost_tracker, **kw):
        try:
            gateway.call_limit(900.0)
        except gateway.DeadlineReached:
            pass
        return ScoringResult()  # the scorer's fail-soft answer: no score

    monkeypatch.setattr(rescore, "score_candidates", refused)
    with gateway.deadline_scope(gateway.MIN_START_SECONDS - 1):
        counts = asyncio.run(rescore.rescore_jobs(cfg, conn, CostTracker(), [jid]))

    assert counts.failed == [] and counts.not_run == [jid] and counts.kept == 0
    row = conn.execute(
        "SELECT score_stale, COALESCE(score_stale_attempted_at,'') a FROM jobs WHERE id=?", (jid,)
    ).fetchone()
    assert row["score_stale"] == 1 and row["a"] == ""

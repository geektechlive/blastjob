"""Migration 55: a stale kept row that keeps failing cannot starve the rest (#31)."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from blastjob.db import client as dbc
from blastjob.llm.cost import CostTracker
from blastjob.matching import rescore
from blastjob.matching.scorer import ScoringResult
from blastjob.models.config import BlastJobConfig
from tests.geo_fixture import apply_region
from tests.profile_fixture import apply_profile


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


def test_migration_55_adds_attempt_column(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert "score_stale_attempted_at" in cols


def _stale_job(conn, n: int) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (source, external_id, company, title, raw_jd, alignment_score, "
        "rationale, review_state, geo_location_raw, geo_remote, first_seen, last_seen, "
        "score_stale) VALUES ('adzuna', ?, ?, 'Director of Technology', ?, 62, 'old', 'kept', "
        "'Fairhaven, OH', 0, '2026-10-01', '2026-10-01', 1)",
        (f"x-{n}", f"Acme{n}", f"Own the stack. {n}"),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def test_always_failing_stale_rows_do_not_block_a_later_one(cfg, conn, monkeypatch):
    failing = [_stale_job(conn, n) for n in range(1, 12)]  # 11 rows, limit is 10
    last = _stale_job(conn, 12)

    async def always_fail(cfg, wh, candidates, cost_tracker, **kw):
        return ScoringResult()

    monkeypatch.setattr(rescore, "score_candidates", always_fail)
    # day 1: the first ten slots go to the lowest ids and all fail
    first = rescore.stale_kept_ids(conn)
    assert first == failing[:10]
    asyncio.run(rescore.rescore_jobs(cfg, conn, CostTracker(), first))

    # day 2: the never-attempted rows come first, so the 12th gets a slot
    second = rescore.stale_kept_ids(conn)
    assert last in second and failing[10] in second
    assert second[:2] == [failing[10], last]

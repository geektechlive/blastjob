"""E6 (+E2 wiring): matching writes are ONE conditional UPDATE; counters follow rowcount."""

from __future__ import annotations

import tempfile

import pytest

from blastjob import config as cfg_mod
from blastjob.db import client as dbc
from blastjob.llm.cost import CostTracker
from blastjob.matching import pipeline as mp
from blastjob.matching.scorer import ScoringResult
from blastjob.models.config import BlastJobConfig
from tests.geo_fixture import apply_region
from tests.profile_fixture import apply_profile

VP = "VP of Ops. Lead a team of 20+, own enterprise ops strategy, report to CEO."


@pytest.fixture
def cfg_conn():
    cfg = BlastJobConfig()
    apply_region(cfg)
    apply_profile(cfg)
    cfg.paths.data_dir = tempfile.mkdtemp()
    cfg_mod.data_dir(cfg).mkdir(parents=True, exist_ok=True)
    (cfg_mod.data_dir(cfg) / "work_history.md").write_text("Exec.", encoding="utf-8")
    return cfg, dbc.init_db(cfg)


def _job(conn, **kw):
    base = dict(
        source="t", external_id="1", title="VP, Ops", company="Acme", raw_jd=VP, geo_remote=1
    )
    return dbc.upsert_job(conn, **{**base, **kw})


def _score(alignment=90, comp_min=0, comp_max=0):
    return {
        "alignment": alignment,
        "rationale": "stub",
        "comp_min": comp_min,
        "comp_max": comp_max,
    }


def _row(conn, jid):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


async def test_kept_mid_run_blocks_score_inferred_comp_and_counters(cfg_conn, monkeypatch):
    cfg, conn = cfg_conn
    jid = _job(conn)

    async def fake_score(cfg_, wh, candidates, ct, **kw):
        conn.execute("UPDATE jobs SET review_state='kept' WHERE id=?", (jid,))
        conn.commit()
        return ScoringResult(scores={jid: _score(90, 150_000, 180_000)})

    monkeypatch.setattr(mp, "score_candidates", fake_score)
    res = await mp.run_matching(cfg, conn, CostTracker())

    row = _row(conn, jid)
    assert row["review_state"] == "kept"
    assert row["alignment_score"] is None
    assert (row["comp_min"], row["comp_max"]) == (0, 0)  # inferred comp not written either
    assert row["comp_provenance"] != "inferred"
    assert (res.scored, res.surfaced, res.queued, res.rejected) == (0, 0, 0, 0)


async def test_pasted_jd_mid_run_blocks_the_whole_write(cfg_conn, monkeypatch):
    cfg, conn = cfg_conn
    jid = _job(conn)

    async def fake_score(cfg_, wh, candidates, ct, **kw):
        conn.execute("UPDATE jobs SET raw_jd='a newly pasted description' WHERE id=?", (jid,))
        conn.commit()
        return ScoringResult(scores={jid: _score(90, 150_000, 180_000)})

    monkeypatch.setattr(mp, "score_candidates", fake_score)
    res = await mp.run_matching(cfg, conn, CostTracker())

    row = _row(conn, jid)
    assert row["alignment_score"] is None
    assert row["comp_max"] == 0
    assert res.surfaced == 0


async def test_unraced_run_writes_score_comp_and_state_together(cfg_conn, monkeypatch):
    cfg, conn = cfg_conn
    jid = _job(conn)

    async def fake_score(cfg_, wh, candidates, ct, **kw):
        return ScoringResult(scores={jid: _score(90, 150_000, 180_000)})

    monkeypatch.setattr(mp, "score_candidates", fake_score)
    res = await mp.run_matching(cfg, conn, CostTracker())

    row = _row(conn, jid)
    assert (row["review_state"], row["alignment_score"]) == ("surfaced", 90)
    assert (row["comp_min"], row["comp_max"], row["comp_provenance"]) == (
        150_000,
        180_000,
        "inferred",
    )
    assert row["comp_currency"] == "USD"
    assert res.surfaced == 1


async def test_posted_comp_is_never_overwritten_by_inference(cfg_conn, monkeypatch):
    cfg, conn = cfg_conn
    jid = _job(conn, comp_provenance="posted", comp_min=200_000, comp_max=250_000)

    async def fake_score(cfg_, wh, candidates, ct, **kw):
        return ScoringResult(scores={jid: _score(90, 100_000, 120_000)})

    monkeypatch.setattr(mp, "score_candidates", fake_score)
    await mp.run_matching(cfg, conn, CostTracker())

    row = _row(conn, jid)
    assert (row["comp_min"], row["comp_max"], row["comp_provenance"]) == (
        200_000,
        250_000,
        "posted",
    )


async def test_posted_gbp_below_usd_floor_is_not_hard_rejected(cfg_conn, monkeypatch):
    cfg, conn = cfg_conn
    cfg.discovery.comp_floor_usd = 200_000
    jid = _job(conn, comp_provenance="posted", comp_max=100_000, comp_currency="GBP")

    async def fake_score(cfg_, wh, candidates, ct, **kw):
        return ScoringResult(scores={jid: _score(90)})

    monkeypatch.setattr(mp, "score_candidates", fake_score)
    res = await mp.run_matching(cfg, conn, CostTracker())

    assert _row(conn, jid)["review_state"] == "surfaced"
    assert res.rejected == 0


def test_auto_archive_does_not_archive_a_row_the_user_just_kept(cfg_conn, monkeypatch):
    _cfg, conn = cfg_conn
    racing = _job(conn, external_id="1", company="Fabrikam")
    plain = _job(conn, external_id="2", company="Fabrikam")
    conn.execute(
        "INSERT INTO company_exclusions (company_key, company, reason, created_at) "
        "VALUES ('fabrikam', 'Fabrikam', 'no', '2026-01-01')"
    )
    conn.commit()

    real_key = mp.company_key

    def key_then_user_keeps(name: str) -> str:
        # The user clicks Keep between the SELECT and the UPDATE.
        conn.execute("UPDATE jobs SET review_state='kept' WHERE id=?", (racing,))
        return real_key(name)

    monkeypatch.setattr(mp, "company_key", key_then_user_keeps)
    archived = mp.auto_archive_rejected_companies(conn)

    assert _row(conn, racing)["review_state"] == "kept"
    assert _row(conn, plain)["review_state"] == "archived"
    assert archived == 1  # counts what actually changed

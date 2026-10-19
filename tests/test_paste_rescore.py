"""Issue #4: a pasted full JD must get a fresh score, never the snippet's replay."""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from blastjob.db import client as dbc
from blastjob.llm.cost import CostTracker
from blastjob.matching import pipeline, rescore
from blastjob.matching.scorer import ScoringResult, reuse_key
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN
from tests.geo_fixture import apply_region
from tests.profile_fixture import apply_profile

SNIPPET = "Director of Technology. Lead strategy for an employee-owned holding company…"
FULL_JD = "Director of Technology. " + "Own MSP governance, data strategy and AI. " * 40


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


def _job(conn: sqlite3.Connection, review_state: str) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (source, external_id, company, title, raw_jd, alignment_score, "
        "rationale, match_strengths, match_gaps, review_state, geo_location_raw, geo_remote, "
        "first_seen, last_seen) VALUES ('adzuna', ?, 'Acme', 'Director of Technology', ?, 62, "
        "'the posting gives almost no detail', 's', 'g', ?, 'Fairhaven, OH', 0, "
        "'2026-10-01', '2026-10-01')",
        (f"x-{review_state}", SNIPPET, review_state),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _fake_scorer(seen: list[str], alignment: int = 84):
    async def fake(cfg, wh, candidates, cost_tracker, **kw):
        out = ScoringResult()
        for c in candidates:
            seen.append(c["jd"])
            key = reuse_key(c["company"], c["title"], c["jd"])
            if key in (kw.get("prior_scores") or {}):
                out.scores[c["job_id"]] = kw["prior_scores"][key]  # what reuse would do
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


def _row(conn, jid):
    return conn.execute(
        "SELECT alignment_score, rationale, review_state FROM jobs WHERE id=?", (jid,)
    ).fetchone()


def test_paste_clears_the_snippet_score(conn):
    jid = _job(conn, "new")
    assert dbc.set_pasted_jd(conn, jid, FULL_JD) is True
    r = _row(conn, jid)
    assert r["alignment_score"] is None and r["rationale"] == ""


def test_repasting_the_same_text_keeps_the_score(conn):
    jid = _job(conn, "new")
    dbc.set_pasted_jd(conn, jid, FULL_JD)
    conn.execute("UPDATE jobs SET alignment_score=80 WHERE id=?", (jid,))
    conn.commit()
    dbc.set_pasted_jd(conn, jid, FULL_JD)
    assert _row(conn, jid)["alignment_score"] == 80


def test_repasting_text_stored_with_trailing_whitespace_keeps_the_score(conn):
    jid = _job(conn, "new")
    conn.execute("UPDATE jobs SET raw_jd=?, alignment_score=80 WHERE id=?", (FULL_JD + "\n\n", jid))
    conn.commit()
    dbc.set_pasted_jd(conn, jid, FULL_JD)
    assert _row(conn, jid)["alignment_score"] == 80


def test_paste_on_a_kept_row_keeps_its_score_until_the_rescore_lands(conn):
    """run_matching never picks up a kept row, so a cleared score would never be
    retried if the re-score failed; the kept row keeps its number instead."""
    jid = _job(conn, "kept")
    dbc.set_pasted_jd(conn, jid, FULL_JD)
    assert _row(conn, jid)["alignment_score"] == 62


def test_rescore_does_not_land_a_score_for_a_jd_that_changed_mid_flight(cfg, conn, monkeypatch):
    jid = _job(conn, "kept")
    dbc.set_pasted_jd(conn, jid, FULL_JD)
    inner = _fake_scorer([], alignment=71)

    async def racing(cfg_, wh, candidates, ct, **kw):
        out = await inner(cfg_, wh, candidates, ct, **kw)
        conn.execute("UPDATE jobs SET raw_jd='an even newer paste' WHERE id=?", (jid,))
        conn.commit()
        return out

    monkeypatch.setattr(rescore, "score_candidates", racing)
    asyncio.run(rescore.rescore_job(cfg, conn, CostTracker(), jid))
    assert _row(conn, jid)["alignment_score"] == 62


def test_matching_does_not_land_a_score_for_a_jd_that_changed_mid_flight(cfg, conn, monkeypatch):
    jid = _job(conn, "new")
    dbc.set_pasted_jd(conn, jid, FULL_JD)
    inner = _fake_scorer([], alignment=84)

    async def racing(cfg_, wh, candidates, ct, **kw):
        out = await inner(cfg_, wh, candidates, ct, **kw)
        conn.execute("UPDATE jobs SET raw_jd='an even newer paste' WHERE id=?", (jid,))
        conn.commit()
        return out

    monkeypatch.setattr(pipeline, "score_candidates", racing)
    asyncio.run(pipeline.run_matching(cfg, conn, CostTracker(), job_ids=[jid]))
    assert _row(conn, jid)["alignment_score"] is None


def test_a_pasted_row_is_not_its_own_prior_score(conn):
    jid = _job(conn, "new")
    dbc.set_pasted_jd(conn, jid, FULL_JD)
    key = reuse_key("Acme", "Director of Technology", FULL_JD)
    fp = conn.execute("SELECT score_model_version FROM jobs WHERE id=?", (jid,)).fetchone()[0]
    assert key not in pipeline.load_prior_scores(conn, fp)


def test_rescore_of_an_undecided_row_uses_the_new_jd(cfg, conn, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(pipeline, "score_candidates", _fake_scorer(seen))
    jid = _job(conn, "new")
    dbc.set_pasted_jd(conn, jid, FULL_JD)
    asyncio.run(rescore.rescore_job(cfg, conn, CostTracker(), jid))
    assert seen == [FULL_JD.strip()]
    r = _row(conn, jid)
    assert r["alignment_score"] == 84 and r["review_state"] == "surfaced"


def test_rescore_of_a_kept_row_updates_score_only(cfg, conn, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(rescore, "score_candidates", _fake_scorer(seen, alignment=71))
    jid = _job(conn, "kept")
    dbc.set_pasted_jd(conn, jid, FULL_JD)
    asyncio.run(rescore.rescore_job(cfg, conn, CostTracker(), jid))
    r = _row(conn, jid)
    assert r["alignment_score"] == 71 and r["rationale"] == "full posting read"
    assert r["review_state"] == "kept"


def test_rescore_leaves_an_archived_row_alone(cfg, conn, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(rescore, "score_candidates", _fake_scorer(seen))
    monkeypatch.setattr(pipeline, "score_candidates", _fake_scorer(seen))
    jid = _job(conn, "archived")
    asyncio.run(rescore.rescore_job(cfg, conn, CostTracker(), jid))
    assert seen == []


def test_paste_route_starts_a_scoped_rescore(cfg, monkeypatch):
    started: list[int] = []

    async def fake_rescore(cfg_, conn_, ct, job_id):
        started.append(job_id)
        return 1

    monkeypatch.setattr(rescore, "rescore_job", fake_rescore)
    app = create_app(load_config=lambda: cfg, save_config=lambda c: None)
    c = dbc.init_db(cfg)
    jid = _job(c, "kept")
    c.close()
    client = TestClient(app)
    resp = client.post(f"/jobs/{jid}/jd", data={"jd": FULL_JD}, headers={CSRF_HEADER: LAUNCH_TOKEN})
    assert resp.status_code == 200
    deadline = time.monotonic() + 5
    while not started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert started == [jid]


def test_pasting_unchanged_text_starts_no_rescore(cfg, monkeypatch):
    started: list[int] = []

    async def fake_rescore(cfg_, conn_, ct, job_id):
        started.append(job_id)
        return 1

    monkeypatch.setattr(rescore, "rescore_job", fake_rescore)
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))
    c = dbc.init_db(cfg)
    jid = _job(c, "kept")
    c.execute("UPDATE jobs SET raw_jd=? WHERE id=?", (FULL_JD.strip(), jid))
    c.commit()
    c.close()
    client.post(f"/jobs/{jid}/jd", data={"jd": FULL_JD}, headers={CSRF_HEADER: LAUNCH_TOKEN})
    time.sleep(0.2)
    assert started == []


def test_an_empty_paste_starts_no_rescore(cfg, monkeypatch):
    started: list[int] = []

    async def fake_rescore(cfg_, conn_, ct, job_id):
        started.append(job_id)
        return 1

    monkeypatch.setattr(rescore, "rescore_job", fake_rescore)
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))
    c = dbc.init_db(cfg)
    jid = _job(c, "kept")
    c.close()
    client.post(f"/jobs/{jid}/jd", data={"jd": "  "}, headers={CSRF_HEADER: LAUNCH_TOKEN})
    time.sleep(0.2)
    assert started == []

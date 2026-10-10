"""GET/POST /jobs/new -- adding a posting by hand.

The scoring pass it kicks off runs as a real background asyncio task inside
the TestClient's portal, so tests that need to observe its result poll the DB
with a short timeout rather than assuming synchronous completion.
"""

from __future__ import annotations

import tempfile
import time

import pytest
from fastapi.testclient import TestClient

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN
from tests.profile_fixture import apply_profile

LONG_JD = "Lead the engineering org and own the roadmap. " * 20


@pytest.fixture
def client_cfg():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    apply_profile(cfg)
    saved: dict = {}

    def load():
        return cfg

    def save(c):
        saved["cfg"] = c

    dbc.init_db(cfg).close()
    app = create_app(load_config=load, save_config=save)
    return TestClient(app), cfg


def _tok():
    return {CSRF_HEADER: LAUNCH_TOKEN}


def _stub_scorer(monkeypatch, alignment: int = 90):
    from blastjob.matching import pipeline as mp
    from blastjob.matching.scorer import ScoringResult

    async def fake_score(cfg_, wh, candidates, ct, **kw):
        return ScoringResult(
            scores={
                c["job_id"]: {
                    "alignment": alignment,
                    "rationale": "stub",
                    "seniority_note": "",
                    "comp_min": 0,
                    "comp_max": 0,
                }
                for c in candidates
            }
        )

    monkeypatch.setattr(mp, "score_candidates", fake_score)


def _wait_for_score(cfg, job_id: int, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        conn = dbc.init_db(cfg)
        row = conn.execute(
            "SELECT review_state, alignment_score FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        conn.close()
        if row["alignment_score"] is not None:
            return row
        time.sleep(0.05)
    return row


def test_form_renders(client_cfg):
    client, _cfg = client_cfg
    r = client.get("/jobs/new")
    assert r.status_code == 200
    assert 'name="company"' in r.text
    assert 'name="raw_jd"' in r.text


def test_missing_required_field_shows_error_and_no_row(client_cfg):
    client, cfg = client_cfg
    r = client.post(
        "/jobs/new",
        data={"company": "", "title": "VP Eng", "raw_jd": LONG_JD},
        headers=_tok(),
    )
    assert r.status_code == 200
    assert "required" in r.text.lower()
    conn = dbc.init_db(cfg)
    n = conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
    conn.close()
    assert n == 0


def test_csrf_required(client_cfg):
    client, _cfg = client_cfg
    r = client.post("/jobs/new", data={"company": "NewCo", "title": "VP Eng", "raw_jd": LONG_JD})
    assert r.status_code == 403


def test_happy_path_inserts_and_scores(client_cfg, monkeypatch):
    client, cfg = client_cfg
    _stub_scorer(monkeypatch, alignment=90)

    r = client.post(
        "/jobs/new",
        data={
            "company": "NewCo",
            "title": "VP Engineering",
            "url": "https://example.com/jobs/123",
            "location": "Remote",
            "remote": "on",
            "raw_jd": LONG_JD,
        },
        headers=_tok(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    job_id = int(r.headers["location"].rstrip("/").rsplit("/", 1)[-1])

    conn = dbc.init_db(cfg)
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    assert row["source"] == "manual"
    assert row["company"] == "NewCo"
    assert row["title"] == "VP Engineering"
    assert row["url"] == "https://example.com/jobs/123"
    assert row["geo_remote"] == 1
    assert row["jd_pasted_at"]

    scored = _wait_for_score(cfg, job_id)
    assert scored["alignment_score"] == 90
    assert scored["review_state"] == "surfaced"


def test_duplicate_submit_does_not_create_second_row(client_cfg, monkeypatch):
    client, cfg = client_cfg
    _stub_scorer(monkeypatch, alignment=90)
    payload = {
        "company": "DupeCo",
        "title": "VP Engineering",
        "url": "https://example.com/jobs/dupe",
        "raw_jd": LONG_JD,
    }
    r1 = client.post("/jobs/new", data=payload, headers=_tok(), follow_redirects=False)
    job_id_1 = int(r1.headers["location"].split("?")[0].rstrip("/").rsplit("/", 1)[-1])

    r2 = client.post("/jobs/new", data=payload, headers=_tok(), follow_redirects=False)
    job_id_2 = int(r2.headers["location"].split("?")[0].rstrip("/").rsplit("/", 1)[-1])

    assert job_id_1 == job_id_2
    conn = dbc.init_db(cfg)
    n = conn.execute("SELECT COUNT(*) n FROM jobs WHERE source='manual'").fetchone()["n"]
    conn.close()
    assert n == 1


def test_unsafe_url_is_not_stored(client_cfg, monkeypatch):
    client, cfg = client_cfg
    _stub_scorer(monkeypatch, alignment=90)
    r = client.post(
        "/jobs/new",
        data={
            "company": "SketchCo",
            "title": "VP Engineering",
            "url": "javascript:alert(1)",
            "raw_jd": LONG_JD,
        },
        headers=_tok(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "url_dropped=1" in r.headers["location"]
    job_id = int(r.headers["location"].split("?")[0].rstrip("/").rsplit("/", 1)[-1])

    conn = dbc.init_db(cfg)
    row = conn.execute("SELECT url FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    assert row["url"] == ""

    detail = client.get(f"/jobs/{job_id}?url_dropped=1")
    assert "wasn't saved" in detail.text

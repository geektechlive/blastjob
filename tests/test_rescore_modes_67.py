# ruff: noqa: F811
"""Two re-score buttons (#67): Re-score changed replays current scores, Re-score all
sends every candidate to the model, and the all mode is confirm-first and CSRF-guarded."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from blastjob.llm.cost import CostTracker
from blastjob.matching import pipeline
from blastjob.matching.pipeline import MatchResult
from blastjob.web import app as web_app
from blastjob.web.app import create_app
from blastjob.web.runtime import Run
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN
from tests.test_score_fingerprint import Recorder, _job, _score_once, cfg, conn  # noqa: F401

HEADERS = {CSRF_HEADER: LAUNCH_TOKEN}


def _match(cfg, conn, **kw):
    return asyncio.run(pipeline.run_matching(cfg, conn, CostTracker(), **kw))


def test_default_replays_a_current_score_without_a_model_call(cfg, conn, monkeypatch):
    _score_once(cfg, conn, monkeypatch)
    rec = Recorder()
    monkeypatch.setattr("blastjob.matching.scorer.make_stream", rec)

    _match(cfg, conn)

    assert rec.calls == 0


def test_rescore_all_sends_a_current_score_to_the_model(cfg, conn, monkeypatch):
    jid = _score_once(cfg, conn, monkeypatch)
    rec = Recorder()
    monkeypatch.setattr("blastjob.matching.scorer.make_stream", rec)

    _match(cfg, conn, rescore_all=True)

    assert rec.calls == 1
    row = conn.execute("SELECT rationale FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["rationale"] == "fresh"


def test_count_candidates_matches_what_a_full_pass_loads(conn):
    _job(conn, ext="a")
    _job(conn, ext="b", company="Northwind")
    archived = _job(conn, ext="c", company="Other")
    conn.execute("UPDATE jobs SET review_state='archived' WHERE id=?", (archived,))
    conn.commit()

    n = pipeline.count_candidates(conn)

    assert n == len(pipeline._load_candidates(conn, only_unscored=False)) == 2


@pytest.fixture
def client(cfg):
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))


@pytest.fixture
def started(monkeypatch):
    """Replace the runtime start so no work runs; each start is recorded."""
    calls: list[tuple[str, object]] = []

    def fake_start(name, work, key=None):
        calls.append((name, work))
        return Run(id=f"{name}-{len(calls)}", name=name)

    monkeypatch.setattr(web_app.RUNTIME, "start", fake_start)
    return calls


def test_confirm_fragment_states_the_count_and_starts_nothing(client, cfg, conn, started):
    _job(conn, ext="a")
    _job(conn, ext="b", company="Northwind")

    resp = client.get("/run/matching/all/confirm")

    assert resp.status_code == 200
    assert "Re-score all 2 jobs?" in resp.text
    assert 'hx-post="/run/matching/all"' in resp.text
    assert started == []


def test_post_without_csrf_is_refused(client, started):
    assert client.post("/run/matching/all").status_code == 403
    assert started == []


def test_post_with_csrf_starts_a_rescore_all_run(client, cfg, started, monkeypatch):
    seen: list[dict] = []

    async def fake_run_matching(_cfg, _conn, _cost, **kw):
        seen.append(kw)
        return MatchResult()

    monkeypatch.setattr("blastjob.matching.pipeline.run_matching", fake_run_matching)

    resp = client.post("/run/matching/all", headers=HEADERS)

    assert resp.status_code == 200
    assert [name for name, _ in started] == ["matching"]
    run = Run(id="x", name="matching")
    asyncio.run(started[0][1](run))
    assert seen[0]["rescore_all"] is True
    assert "Re-score all" in run.events[0].data


def test_existing_route_runs_changed_mode_and_logs_it(client, cfg, started, monkeypatch):
    seen: list[dict] = []

    async def fake_run_matching(_cfg, _conn, _cost, **kw):
        seen.append(kw)
        return MatchResult()

    monkeypatch.setattr("blastjob.matching.pipeline.run_matching", fake_run_matching)

    client.post("/run/matching", headers=HEADERS)
    run = Run(id="x", name="matching")
    asyncio.run(started[0][1](run))

    assert not seen[0].get("rescore_all")
    assert "Re-score changed" in run.events[0].data


def test_confirm_shows_the_running_pass_instead_of_a_prompt(client, started, monkeypatch):
    running = Run(id="matching-9", name="matching")
    monkeypatch.setattr(web_app.RUNTIME, "active", lambda names, **kw: running)

    resp = client.get("/run/matching/all/confirm")

    assert "matching-9" in resp.text
    assert "Start" not in resp.text

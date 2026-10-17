"""W1: the employer-jd button's per-job cooldown and global concurrency cap."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import employer_jd as web_ejd
from blastjob.web.app import create_app
from blastjob.web.runtime import Run
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

SNIPPET = "Director of Technology. Lead strategy for an employee-owned holding company…"
HEADERS = {CSRF_HEADER: LAUNCH_TOKEN}


@pytest.fixture
def cfg(tmp_path) -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = str(tmp_path)
    return c


@pytest.fixture
def client(cfg) -> TestClient:
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))


def _job(cfg, company: str = "Acme", lookup_at: str = "") -> int:
    conn = dbc.init_db(cfg)
    cur = conn.execute(
        "INSERT INTO jobs (source, external_id, company, title, raw_jd, jd_lookup_at, "
        "review_state, first_seen, last_seen) VALUES ('adzuna', ?, ?, 'Director of Technology', "
        "?, ?, 'new', '2026-10-01', '2026-10-01')",
        (f"x-{company}", company, SNIPPET, lookup_at),
    )
    conn.commit()
    conn.close()
    return int(cur.lastrowid or 0)


@pytest.fixture
def started(monkeypatch) -> list[str]:
    calls: list[str] = []

    def fake_start(name, work):
        calls.append(name)
        return Run(id=f"{name}-{len(calls)}", name=name)

    monkeypatch.setattr(web_ejd.RUNTIME, "start", fake_start)
    web_ejd.IN_FLIGHT.clear()
    web_ejd.LAST_START.clear()
    return calls


def _post(client, jid):
    return client.post(f"/jobs/{jid}/employer-jd", headers=HEADERS)


def test_recent_recorded_lookup_refuses_the_click(client, cfg, started):
    recent = (datetime.now() - timedelta(minutes=3)).isoformat(timespec="seconds")
    jid = _job(cfg, lookup_at=recent)
    resp = _post(client, jid)
    assert started == []
    assert "minutes" in resp.text
    assert "HX-Trigger" not in resp.headers


def test_old_recorded_lookup_does_not_refuse(client, cfg, started):
    old = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
    jid = _job(cfg, lookup_at=old)
    _post(client, jid)
    assert started == ["score-job"]


def test_recent_start_without_a_recorded_lookup_refuses(client, cfg, started):
    jid = _job(cfg)
    _post(client, jid)
    web_ejd.IN_FLIGHT[jid].status = "done"
    resp = _post(client, jid)
    assert started == ["score-job"]
    assert "minutes" in resp.text


def test_start_older_than_the_cooldown_does_not_refuse(client, cfg, started):
    jid = _job(cfg)
    _post(client, jid)
    web_ejd.IN_FLIGHT[jid].status = "done"
    web_ejd.LAST_START[jid] = time.time() - web_ejd.COOLDOWN_SECONDS - 1
    _post(client, jid)
    assert started == ["score-job", "score-job"]


def test_third_concurrent_run_is_refused(client, cfg, started):
    a, b, c = (_job(cfg, company=n) for n in ("Acme", "Globex", "Initech"))
    _post(client, a)
    _post(client, b)
    resp = _post(client, c)
    assert started == ["score-job", "score-job"]
    assert "running" in resp.text.lower()
    assert "HX-Trigger" not in resp.headers


def test_finished_run_frees_a_slot(client, cfg, started):
    a, b, c = (_job(cfg, company=n) for n in ("Acme", "Globex", "Initech"))
    _post(client, a)
    _post(client, b)
    web_ejd.IN_FLIGHT[a].status = "done"
    _post(client, c)
    assert started == ["score-job"] * 3


def test_double_click_on_a_running_job_still_returns_that_run(client, cfg, started):
    jid = _job(cfg)
    first = _post(client, jid)
    second = _post(client, jid)
    assert started == ["score-job"]
    assert second.headers["HX-Trigger"] == first.headers["HX-Trigger"]

"""U8: POST /jobs/{id}/employer-jd, the cockpit's "Find the employer's posting" run."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from blastjob.core import employer_jd as core_ejd
from blastjob.db import client as dbc
from blastjob.matching import rescore
from blastjob.models.config import BlastJobConfig
from blastjob.web import employer_jd as web_ejd
from blastjob.web.app import create_app
from blastjob.web.runtime import Run
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

SNIPPET = "Director of Technology. Lead strategy for an employee-owned holding company…"
FULL_JD = "Director of Technology. " + "Own MSP governance, data strategy and AI. " * 40
HEADERS = {CSRF_HEADER: LAUNCH_TOKEN}


@pytest.fixture
def cfg(tmp_path) -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = str(tmp_path)
    return c


@pytest.fixture
def client(cfg) -> TestClient:
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))


def _job(
    cfg,
    *,
    source: str = "adzuna",
    company: str = "Acme",
    raw_jd: str = SNIPPET,
    pasted: str = "",
) -> int:
    conn = dbc.init_db(cfg)
    cur = conn.execute(
        "INSERT INTO jobs (source, external_id, company, title, raw_jd, jd_pasted_at, "
        "review_state, first_seen, last_seen) VALUES (?, ?, ?, 'Director of Technology', ?, ?, "
        "'new', '2026-10-01', '2026-10-01')",
        (source, f"x-{source}-{company}-{len(raw_jd)}", company, raw_jd, pasted),
    )
    conn.commit()
    conn.close()
    return int(cur.lastrowid or 0)


@pytest.fixture
def started(monkeypatch) -> list[str]:
    """Replace the runtime start so no work runs; each start is recorded."""
    calls: list[str] = []

    def fake_start(name, work):
        calls.append(name)
        return Run(id=f"{name}-{len(calls)}", name=name)

    monkeypatch.setattr(web_ejd.RUNTIME, "start", fake_start)
    web_ejd.IN_FLIGHT.clear()
    web_ejd.LAST_START.clear()
    return calls


def test_route_requires_csrf_token(client, cfg, started):
    jid = _job(cfg)
    assert client.post(f"/jobs/{jid}/employer-jd").status_code == 403
    assert started == []


def test_route_requires_local_origin(client, cfg, started):
    jid = _job(cfg)
    resp = client.post(
        f"/jobs/{jid}/employer-jd", headers={**HEADERS, "Origin": "http://evil.example.com"}
    )
    assert resp.status_code == 403
    assert started == []


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"raw_jd": FULL_JD}, "snippet"),
        ({"raw_jd": SNIPPET, "pasted": "2026-10-02T00:00:00"}, "snippet"),
        ({"source": "greenhouse:acme"}, "aggregator"),
        ({"source": "manual"}, "aggregator"),
        ({"company": "Acme Staffing"}, "agency"),
        ({"company": ""}, "company"),
    ],
)
def test_refuses_ineligible_rows_without_a_run(client, cfg, started, kwargs, needle):
    jid = _job(cfg, **kwargs)
    resp = client.post(f"/jobs/{jid}/employer-jd", headers=HEADERS)
    assert resp.status_code == 200
    assert 'id="jd-block"' in resp.text
    assert needle in resp.text.lower()
    assert started == []
    assert "HX-Trigger" not in resp.headers


def test_unknown_job_is_404(client, started):
    assert client.post("/jobs/9999/employer-jd", headers=HEADERS).status_code == 404


def test_eligible_click_starts_one_score_job_run_and_triggers_refresh(client, cfg, started):
    jid = _job(cfg)
    resp = client.post(f"/jobs/{jid}/employer-jd", headers=HEADERS)
    assert resp.status_code == 200
    assert started == ["score-job"]
    assert resp.headers["HX-Trigger"] == "refresh-run-status"


def test_second_click_while_in_flight_starts_no_second_run(client, cfg, started):
    jid = _job(cfg)
    first = client.post(f"/jobs/{jid}/employer-jd", headers=HEADERS)
    second = client.post(f"/jobs/{jid}/employer-jd", headers=HEADERS)
    assert started == ["score-job"]
    assert first.headers["HX-Trigger"] == second.headers["HX-Trigger"] == "refresh-run-status"


def test_another_job_is_not_blocked_by_an_in_flight_one(client, cfg, started):
    a = _job(cfg, company="Acme")
    b = _job(cfg, company="Globex")
    client.post(f"/jobs/{a}/employer-jd", headers=HEADERS)
    client.post(f"/jobs/{b}/employer-jd", headers=HEADERS)
    assert started == ["score-job", "score-job"]


def test_a_finished_run_does_not_block_a_retry(client, cfg, started):
    jid = _job(cfg)
    client.post(f"/jobs/{jid}/employer-jd", headers=HEADERS)
    web_ejd.IN_FLIGHT[jid].status = "done"
    web_ejd.LAST_START[jid] -= web_ejd.COOLDOWN_SECONDS + 1  # past the cooldown
    client.post(f"/jobs/{jid}/employer-jd", headers=HEADERS)
    assert started == ["score-job", "score-job"]


def test_run_loads_secrets_upgrades_the_one_job_and_rescores_upgraded(cfg, monkeypatch):
    calls: dict = {}
    jid = _job(cfg)

    async def fake_upgrade(cfg_, conn_, **kw):
        calls["upgrade"] = kw
        return core_ejd.UpgradeResult(
            outcomes=[
                core_ejd.RowOutcome(
                    job_id=jid,
                    company="Acme",
                    title="T",
                    score=None,
                    review_state="new",
                    status="upgraded",
                    via="search",
                )
            ],
            upgraded=[jid],
        )

    async def fake_rescore(cfg_, conn_, ct, ids, **kw):
        calls["rescore"] = list(ids)
        return rescore.RescoreCounts()

    monkeypatch.setattr(core_ejd, "run_upgrade", fake_upgrade)
    monkeypatch.setattr(rescore, "rescore_jobs", fake_rescore)
    monkeypatch.setattr(web_ejd.secrets_mod, "load_secrets_env", lambda: calls.setdefault("sec", 1))
    web_ejd.IN_FLIGHT.clear()
    web_ejd.LAST_START.clear()
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))
    client.post(f"/jobs/{jid}/employer-jd", headers=HEADERS)
    deadline = time.monotonic() + 5
    while "rescore" not in calls and time.monotonic() < deadline:
        time.sleep(0.02)
    assert calls["sec"] == 1
    assert calls["upgrade"]["job_ids"] == [jid]
    assert calls["upgrade"]["network"] is True and calls["upgrade"]["write"] is True
    assert calls["upgrade"]["min_score"] is None
    assert calls["upgrade"]["respect_backoff"] is False
    assert calls["upgrade"]["request_limit"] == 40
    assert calls["rescore"] == [jid]

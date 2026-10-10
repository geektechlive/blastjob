"""Posting closure on the surfaces the user sees: the job page, the shortlist,
the follow-ups on Applied, the daily digest and the shadow-run report."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blastjob import daily
from blastjob.core import closure
from blastjob.core.closure import ClosureResult
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.models.tracking import STATUSES
from blastjob.web import service, stage_lists, stages
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN


@pytest.fixture
def cfg():
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    (Path(c.paths.data_dir) / "work_history.md").write_text("exec", encoding="utf-8")
    return c


@pytest.fixture
def conn(cfg):
    conn = dbc.init_db(cfg)
    yield conn
    conn.close()


def _kept(conn, *, app: str = "", source: str = "linkedin", company: str = "Acme") -> int:
    jid = dbc.upsert_job(
        conn, source=source, external_id=f"x-{company}", company=company, title="VP of Ops"
    )
    conn.execute("UPDATE jobs SET review_state='kept', application_state=? WHERE id=?", (app, jid))
    conn.commit()
    return jid


def _client(cfg) -> TestClient:
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))


def test_closure_auto_defaults_off():
    assert BlastJobConfig().discovery.closure_auto is False


def test_new_states_are_terminal():
    assert {"posting_closed", "filled"} <= set(STATUSES)
    assert {"posting_closed", "filled"} <= set(stages.CLOSED_STATES)
    assert not {"posting_closed", "filled"} & set(stages.LIVE_STATES)
    job = {"review_state": "kept", "build_state": "tailored", "application_state": "posting_closed"}
    assert stages.stage_of(job) == "closed"


@pytest.mark.parametrize("verdict", ["closed", "open"])
def test_manual_posting_routes_require_csrf(cfg, conn, verdict):
    jid = _kept(conn)
    r = _client(cfg).post(f"/jobs/{jid}/posting/{verdict}")
    assert r.status_code == 403
    row = conn.execute("SELECT posting_check_manual FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["posting_check_manual"] == 0


def test_manual_posting_closed_route_stores_verdict_and_renders_badge(cfg, conn):
    jid = _kept(conn, app="drafted")
    r = _client(cfg).post(f"/jobs/{jid}/posting/closed", headers={CSRF_HEADER: LAUNCH_TOKEN})
    assert r.status_code == 200
    assert "posting closed" in r.text
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["posting_check"] == "closed"
    assert row["posting_check_manual"] == 1
    assert row["application_state"] == "posting_closed"
    assert row["closed_prior_state"] == "drafted"


def test_manual_still_open_route_restores(cfg, conn):
    jid = _kept(conn, app="drafted")
    client = _client(cfg)
    client.post(f"/jobs/{jid}/posting/closed", headers={CSRF_HEADER: LAUNCH_TOKEN})
    r = client.post(f"/jobs/{jid}/posting/open", headers={CSRF_HEADER: LAUNCH_TOKEN})
    assert r.status_code == 200
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["posting_check"] == "verified_open"
    assert row["application_state"] == "drafted"


def test_manual_posting_route_404s_for_unknown_job(cfg, conn):
    r = _client(cfg).post("/jobs/4242/posting/closed", headers={CSRF_HEADER: LAUNCH_TOKEN})
    assert r.status_code == 404


def test_job_page_shows_badge_and_safe_link(cfg, conn):
    jid = _kept(conn)
    conn.execute(
        "UPDATE jobs SET posting_check='verified_open', posting_checked_at='2026-09-13T08:00:00', "
        "employer_link_source='greenhouse:acme', employer_link_external_id='g1', "
        "employer_link_url='javascript:alert(1)' WHERE id=?",
        (jid,),
    )
    conn.commit()
    r = _client(cfg).get(f"/jobs/{jid}")
    assert r.status_code == 200
    assert "open · checked 2026-09-13" in r.text
    assert "greenhouse:acme" in r.text
    assert "javascript:alert" not in r.text
    assert f"/jobs/{jid}/posting/closed" in r.text
    assert f"/jobs/{jid}/posting/open" in r.text


def test_kept_list_badges_closed_and_likely_closed(cfg, conn):
    a = _kept(conn, company="Acme")
    b = _kept(conn, company="Beta")
    conn.execute("UPDATE jobs SET posting_check='closed' WHERE id=?", (a,))
    conn.execute("UPDATE jobs SET posting_check='likely_closed' WHERE id=?", (b,))
    conn.commit()
    r = _client(cfg).get("/shortlist")
    assert "posting closed" in r.text
    assert "likely closed" in r.text


def test_applied_job_with_closed_posting_is_on_the_followups_desk(conn):
    jid = _kept(conn, app="applied")
    conn.execute(
        "UPDATE jobs SET applied_at=date('now'), posting_check='closed', "
        "posting_closed_at='2026-09-13T08:00:00' WHERE id=?",
        (jid,),
    )
    conn.commit()
    due = stage_lists.followups_due(conn)
    assert [d["id"] for d in due] == [jid]
    assert "posting closed" in due[0]["followup_reason"]


def test_closed_jobs_are_never_offered_a_followup(conn):
    for state in ("posting_closed", "filled"):
        jid = _kept(conn, app=state, company=state)
        conn.execute(
            "UPDATE jobs SET applied_at='2026-01-01', next_action_due='2026-01-02' WHERE id=?",
            (jid,),
        )
    conn.commit()
    assert stage_lists.followups_due(conn) == []


def test_manual_status_change_clears_the_saved_prior_state(conn):
    jid = _kept(conn, app="posting_closed")
    conn.execute("UPDATE jobs SET closed_prior_state='drafted' WHERE id=?", (jid,))
    conn.commit()
    service.set_application_state(conn, jid, "applied", ["", *STATUSES])
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["application_state"] == "applied"
    assert row["closed_prior_state"] == ""


# --- digest + daily ----------------------------------------------------------


def test_digest_line_names_closed_kept_postings():
    closed = [
        {"id": 1, "company": "Fabrikam Equipment", "title": "Chief Officer"},
        {"id": 2, "company": "Northwind Power", "title": "VP Ops"},
    ]
    text = daily.build_digest([], {}, [], closed_postings=closed)
    assert (
        "2 kept postings closed: Fabrikam Equipment Chief Officer, Northwind Power VP Ops" in text
    )


def test_digest_still_empty_with_nothing_closed():
    assert daily.build_digest([], {}, [], closed_postings=[]) == ""


def _stub_pipeline(monkeypatch, closed: list[dict]):
    from blastjob.discovery import pipeline
    from blastjob.discovery.pipeline import DiscoveryResult
    from blastjob.matching import pipeline as mpipe
    from blastjob.matching.pipeline import MatchResult

    async def fake_discovery(cfg, conn, adapters=None):
        return DiscoveryResult()

    async def fake_matching(cfg, conn, tracker, on_progress=None, **kw):
        return MatchResult()

    async def fake_closure(cfg, conn, *, deps=None, shadow=False, now=None):
        return ClosureResult(closed_kept=closed)

    monkeypatch.setattr(pipeline, "run_discovery", fake_discovery)
    monkeypatch.setattr(mpipe, "run_matching", fake_matching)
    monkeypatch.setattr(closure, "run_closure", fake_closure)


async def test_run_daily_sends_the_closed_postings_line(cfg, conn, monkeypatch):
    _stub_pipeline(
        monkeypatch, [{"id": 1, "company": "Fabrikam Equipment", "title": "Chief Officer"}]
    )
    sent: list[str] = []

    async def send(text):
        sent.append(text)

    res = await daily.run_daily(cfg, conn, send=send)
    assert res.sent is True
    assert "1 kept posting closed: Fabrikam Equipment Chief Officer" in sent[0]


async def test_closure_failure_does_not_sink_the_daily_run(cfg, conn, monkeypatch):
    _stub_pipeline(monkeypatch, [])

    async def boom(cfg, conn, **kw):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(closure, "run_closure", boom)

    async def send(text):
        return None

    res = await daily.run_daily(cfg, conn, send=send)
    assert "closure" in res.errors


def test_closure_report_sends_nothing_and_records_no_run(cfg, conn, monkeypatch, capsys):
    jid = _kept(conn, company="Fabrikam Equipment")
    captured: dict = {}

    async def fake_closure(cfg_, conn_, *, deps=None, shadow=False, now=None):
        captured["shadow"] = shadow
        conn_.execute("UPDATE jobs SET posting_check='unverifiable' WHERE id=?", (jid,))
        conn_.commit()
        return ClosureResult()

    def no_sender():
        raise AssertionError("closure report must never build a Discord sender")

    cfg.discovery.closure_auto = True
    monkeypatch.setattr(closure, "run_closure", fake_closure)
    monkeypatch.setattr(daily, "_discord_sender", no_sender)
    monkeypatch.setattr(daily.cfg_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(daily.applog, "setup", lambda: None)

    assert daily.main(["--closure-report"]) == 0

    out = capsys.readouterr().out
    assert captured["shadow"] is True
    assert "Fabrikam Equipment" in out
    assert "unverifiable" in out
    n = conn.execute("SELECT COUNT(*) AS n FROM daily_runs").fetchone()["n"]
    assert n == 0


# --- Surfaced / Queue show the verdict and sink proven-closed rows -------------


def _listed(conn, review: str, company: str, score: int, check: str = "", when: str = "") -> int:
    jid = dbc.upsert_job(
        conn, source="linkedin", external_id=f"l-{company}", company=company, title="VP of IT"
    )
    conn.execute(
        "UPDATE jobs SET review_state=?, alignment_score=?, posting_check=?, "
        "posting_checked_at=? WHERE id=?",
        (review, score, check, when, jid),
    )
    conn.commit()
    return jid


@pytest.mark.parametrize(
    ("review", "lister"),
    [("surfaced", stage_lists.surfaced_jobs), ("queued", stage_lists.queued_jobs)],
)
def test_closed_postings_sink_on_surfaced_and_queue(conn, review, lister):
    closed = _listed(conn, review, "Closed Co", 95, check="closed")
    likely = _listed(conn, review, "Likely Co", 94, check="likely_closed")
    best = _listed(conn, review, "Best Co", 90, check="verified_open")
    next_ = _listed(conn, review, "Next Co", 85)

    assert [j["id"] for j in lister(conn)] == [best, next_, closed, likely]


def test_comp_sort_on_surfaced_sinks_closed_postings_too(conn):
    closed = _listed(conn, "surfaced", "Closed Co", 95, check="closed")
    open_ = _listed(conn, "surfaced", "Open Co", 80)

    assert [j["id"] for j in stage_lists.surfaced_jobs(conn, sort="comp")] == [open_, closed]


def test_verified_open_rows_carry_a_quiet_checked_badge(cfg, conn):
    _listed(conn, "surfaced", "Acme", 90, check="verified_open", when="2026-10-07T07:31:00")

    page = _client(cfg).get("/").text

    assert "open · checked 2026-10-07" in page

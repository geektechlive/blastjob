"""Tests for `web/routes_outcomes.py`: the outcome report and per-job history.

Built against a standalone FastAPI app (not `web.app.create_app`), the same
pattern `tests/test_web_ingest.py` uses for `routes_settings.py` -- this keeps
the test inside `routes_outcomes.py`'s ownership without editing `web/app.py`;
wiring `create_app` to serve these routes is the INTEGRATION PATCH. `render`
is reused unmodified from `web.app` (an existing, already-tested function).

`core/packet_sends.py`'s module docstring is a HARD CONSTRAINT this route
module exists to respect: descriptive bookkeeping only, sorted by `sent_at`,
never a ranking or comparison across sends. Several tests below assert that
constraint directly against the rendered page, not just against the report
function it wraps.
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blastjob.core import packet_sends as ps
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import routes_outcomes
from blastjob.web.app import render
from blastjob.web.deps import Deps


@pytest.fixture
def cfg() -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


@pytest.fixture
def client(cfg):
    app = FastAPI()
    app.state.nav_counts = None
    deps = Deps(
        conn=lambda: dbc.init_db(cfg),
        load_config=lambda: cfg,
        render=render,
        nav_counts=lambda: {},
        radius=lambda: 0.0,
    )
    routes_outcomes.register(app, deps)
    return TestClient(app)


def _job(conn, *, company="Acme", title="VP Ops") -> int:
    return dbc.upsert_job(conn, source="s1", external_id="e1", company=company, title=title)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# /outcomes
# --------------------------------------------------------------------------


def test_outcomes_page_renders_empty_state(client):
    r = client.get("/outcomes")
    assert r.status_code == 200
    assert "No packet has been recorded" in r.text


def test_outcomes_page_shows_a_sent_packet_and_its_hash(client, conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "content")
    ps.record_send(conn, job_id, str(tmp_path), resume, now=datetime(2026, 9, 1, 9, 0))

    r = client.get("/outcomes")

    assert r.status_code == 200
    assert "Acme" in r.text
    assert "VP Ops" in r.text
    full_sha = ps.outcome_report(conn)[0]["resume_sha256"]
    assert full_sha in r.text, (
        "the full sha256 must be present (e.g. in a title attr) so it is provable"
    )


def test_outcomes_page_shows_fatal_findings_shipped_with_the_packet(client, conn, tmp_path):
    from blastjob.core.packet_report import Artifact, Finding, Severity

    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "content")
    ps.record_send(
        conn,
        job_id,
        str(tmp_path),
        resume,
        fatal_findings=(
            Finding(
                severity=Severity.FATAL,
                code="x",
                message="an unverifiable certification claim",
                artifact=Artifact.RESUME,
            ),
        ),
    )

    r = client.get("/outcomes")

    assert "an unverifiable certification claim" in r.text


def test_outcomes_page_shows_outcomes_and_furthest_stage(client, conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "content")
    send_id = ps.record_send(conn, job_id, str(tmp_path), resume)
    ps.record_outcome(conn, send_id, "screen", note="recruiter call")

    r = client.get("/outcomes")

    assert "screen" in r.text
    assert "recruiter call" in r.text


def test_outcomes_page_scores_are_labelled_uncalibrated(client, conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "content")
    ps.record_send(conn, job_id, str(tmp_path), resume, groundedness=91)

    r = client.get("/outcomes")

    assert "91" in r.text
    assert "ncalibrated" in r.text.lower() or "not a measurement" in r.text.lower()


def test_outcomes_page_has_no_sort_query_control(client, conn, tmp_path):
    """The HARD CONSTRAINT, checked at the HTTP boundary: no sort parameter
    changes the ordering, and the page never asks for one."""
    job_id = _job(conn)
    old_resume = _write(tmp_path, "resume-old.md", "v1")
    ps.record_send(conn, job_id, str(tmp_path), old_resume, now=datetime(2026, 9, 1))
    new_dir = tmp_path / "second"
    new_dir.mkdir()
    new_resume = _write(new_dir, "resume-new.md", "v2")
    ps.record_send(conn, job_id, str(new_dir), new_resume, now=datetime(2026, 9, 10))

    plain = client.get("/outcomes")
    with_sort_param = client.get("/outcomes", params={"sort": "score"})

    assert plain.status_code == with_sort_param.status_code == 200
    assert plain.text == with_sort_param.text, (
        "a sort= param must have no effect -- there is no sort here"
    )


def test_outcomes_page_rows_are_in_sent_at_order_only(client, conn, tmp_path):
    job_id = _job(conn)
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    ps.record_send(
        conn, job_id, str(first_dir), _write(first_dir, "r.md", "v1"), now=datetime(2026, 9, 1)
    )
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second_send = ps.record_send(
        conn, job_id, str(second_dir), _write(second_dir, "r.md", "v2"), now=datetime(2026, 9, 10)
    )
    # Give the SECOND (later-sent) send the furthest stage, so a stage-based
    # sort would visibly reorder these rows relative to a sent_at sort.
    ps.record_outcome(conn, second_send, "interview")

    r = client.get("/outcomes")

    first_pos = r.text.find("2026-09-01")
    second_pos = r.text.find("2026-09-10")
    assert -1 < first_pos < second_pos, (
        "rows must stay in sent_at order, not reorder by outcome quality"
    )


def test_outcomes_page_no_aggregate_success_rate_text(client, conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "content")
    send_id = ps.record_send(conn, job_id, str(tmp_path), resume)
    ps.record_outcome(conn, send_id, "interview")

    r = client.get("/outcomes")

    # Strip the explanatory note itself (found by its own marker class, which
    # legitimately names the things it refuses to compute) before checking the
    # RENDERED DATA never states one.
    note_end = r.text.index("outcomes-note")
    after_note = r.text[r.text.index("</p>", note_end) + 4 :]
    lowered = after_note.lower()
    for banned in ("success rate", "win rate", "% reached", "average score", "best packet"):
        assert banned not in lowered


# --------------------------------------------------------------------------
# /jobs/{job_id}/history
# --------------------------------------------------------------------------


def test_job_history_404s_for_an_unknown_job(client):
    r = client.get("/jobs/999999/history")
    assert r.status_code == 404


def test_job_history_empty_state_for_a_job_with_no_send(client, conn):
    job_id = _job(conn)
    r = client.get(f"/jobs/{job_id}/history")
    assert r.status_code == 200
    assert "No packet has been recorded" in r.text


def test_job_history_shows_the_timeline_oldest_first(client, conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "content")
    send_id = ps.record_send(conn, job_id, str(tmp_path), resume, now=datetime(2026, 9, 1, 9, 0))
    ps.record_outcome(conn, send_id, "screen", note="recruiter call")
    ps.record_outcome(conn, send_id, "interview", note="panel round 1")

    # The order this test actually cares about comes from the data layer, not
    # from stage-name substrings that also appear in this template's own CSS
    # classes (`job-history-{{ e.stage }}`) -- use each event's OWN rendered
    # timestamp as its position marker instead.
    events = ps.job_event_log(conn, job_id)
    assert [e["stage"] for e in events] == ["applied", "screen", "interview"]

    r = client.get(f"/jobs/{job_id}/history")

    assert r.status_code == 200
    markers = [f"{e['at'][:10]} {e['at'][11:16]}" for e in events]
    positions = [r.text.find(m) for m in markers]
    assert all(p != -1 for p in positions), "every event's own rendered timestamp must appear"
    assert positions == sorted(positions), "events must render in job_event_log's own order"
    assert "recruiter call" in r.text
    assert "panel round 1" in r.text


def test_job_history_shows_stage_durations(client, conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "content")
    send_id = ps.record_send(conn, job_id, str(tmp_path), resume, now=datetime(2026, 9, 1, 9, 0))
    ps.record_outcome(conn, send_id, "screen", note="")

    r = client.get(f"/jobs/{job_id}/history")

    assert "Days" in r.text


def test_job_history_is_a_fragment_not_a_full_page(client, conn, tmp_path):
    """No `base.html` chrome -- see the module docstring's fragment note.

    Checked against the POPULATED branch: the empty-state branch could never
    emit `<html`/`<nav` regardless of whether the template extends `base.html`,
    so it would pass even on a template that reached for full-page chrome.
    """
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "content")
    send_id = ps.record_send(conn, job_id, str(tmp_path), resume)
    ps.record_outcome(conn, send_id, "screen")

    r = client.get(f"/jobs/{job_id}/history")

    lowered = r.text.lower()
    assert "screen" in lowered
    assert "<html" not in lowered
    assert "<nav" not in lowered

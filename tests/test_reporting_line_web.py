"""The reporting-line label reaches list rows and the job page, not the score."""

from __future__ import annotations

import sqlite3
import tempfile

import pytest
from fastapi.testclient import TestClient

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig, CandidateConfig
from blastjob.web import service
from blastjob.web.app import create_app


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, company TEXT, raw_jd TEXT)")
    conn.execute("CREATE TABLE connections (company_key TEXT)")
    conn.execute("CREATE TABLE target_companies (company_key TEXT, tier TEXT)")  # dream badge
    return conn


def test_annotate_network_adds_label_from_stored_jd(monkeypatch) -> None:
    monkeypatch.setattr(service.net, "warm_counts", lambda c, k: {})
    conn = _conn()
    conn.execute("INSERT INTO jobs VALUES (1, 'Acme', 'The role reports to the CFO.')")
    conn.execute("INSERT INTO jobs VALUES (2, 'Acme', 'Nothing said.')")
    rows = conn.execute("SELECT id, company FROM jobs ORDER BY id").fetchall()

    out = service.annotate_network(conn, rows, CandidateConfig())

    assert out[0]["reporting_label"] == "Reports to CFO"
    assert out[1]["reporting_label"] == ""


def test_annotate_network_prefers_row_jd_when_present(monkeypatch) -> None:
    monkeypatch.setattr(service.net, "warm_counts", lambda c, k: {})
    conn = _conn()
    conn.execute("INSERT INTO jobs VALUES (1, 'Acme', 'stale')")
    row = conn.execute("SELECT id, company, 'Reports to the CEO' AS raw_jd FROM jobs").fetchall()

    out = service.annotate_network(conn, row, CandidateConfig())

    assert out[0]["reporting_label"] == "Reports to CEO"


# --- rendered pages ----------------------------------------------------------------


@pytest.fixture
def cfg() -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    return c


def _kept_job(cfg: BlastJobConfig, jd: str) -> int:
    conn = dbc.init_db(cfg)
    jid = dbc.upsert_job(
        conn, source="acme-board", external_id="e1", company="Acme", title="VP Technology"
    )
    conn.execute(
        "UPDATE jobs SET review_state='kept', alignment_score=90, raw_jd=? WHERE id=?", (jd, jid)
    )
    conn.commit()
    conn.close()
    return jid


def _client(cfg: BlastJobConfig) -> TestClient:
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda _c: None))


def test_job_page_and_shortlist_row_show_the_label(cfg) -> None:
    jid = _kept_job(cfg, "The role reports to the Chief Financial Officer. " * 3)
    client = _client(cfg)

    assert "Reports to CFO" in client.get(f"/jobs/{jid}").text
    assert "Reports to CFO" in client.get("/shortlist").text


def test_unknown_reporting_line_shows_nothing(cfg) -> None:
    jid = _kept_job(cfg, "Lead the platform group. " * 5)

    assert "Reports to" not in _client(cfg).get(f"/jobs/{jid}").text


def test_label_text_from_the_description_is_escaped(cfg) -> None:
    jid = _kept_job(cfg, "This role reports to the <b>Boss</b> of Things.")

    page = _client(cfg).get(f"/jobs/{jid}").text

    assert "<b>Boss</b>" not in page
    assert "&lt;b&gt;Boss&lt;/b&gt;" in page

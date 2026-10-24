"""How an application started (#80): channel in, optional via, when recorded.

Asked in the same interaction as Applied, skippable (NULL = unknown), editable on
the job page, never inferred and never written over `jobs.source`.
"""

from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

TOKEN = {CSRF_HEADER: LAUNCH_TOKEN}
APPLIED = {"application_state": "applied", "applied_at": "2026-10-01"}


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


def _client(cfg) -> TestClient:
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda _c: None))


def _kept(conn, **cols: str) -> int:
    jid = dbc.upsert_job(
        conn, source="acme-board", external_id="e1", company="Acme", title="VP Technology"
    )
    conn.execute("UPDATE jobs SET review_state='kept', alignment_score=90 WHERE id=?", (jid,))
    for k, v in cols.items():
        conn.execute(f"UPDATE jobs SET {k}=? WHERE id=?", (v, jid))
    conn.commit()
    return jid


def _row(conn, jid: int) -> dict:
    return dict(conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())


def _section(page: str) -> str:
    """The 'How it started' block of the job page."""
    start = page.index("How it started")
    return page[start : page.index("</form>", start)]


def test_row_apply_stores_channel_and_via(cfg, conn):
    jid = _kept(conn)
    r = _client(cfg).post(
        f"/jobs/{jid}/stage/apply",
        data={"channel": "recruiter", "via": "Example Search"},
        headers=TOKEN,
    )
    assert r.status_code == 200
    row = _row(conn, jid)
    assert row["application_state"] == "applied"
    assert row["app_channel"] == "recruiter"
    assert row["app_channel_via"] == "Example Search"
    assert row["app_channel_at"]


def test_row_apply_skip_leaves_channel_null(cfg, conn):
    jid = _kept(conn)
    _client(cfg).post(f"/jobs/{jid}/stage/apply", headers=TOKEN)
    row = _row(conn, jid)
    assert row["application_state"] == "applied"
    assert row["app_channel"] is None
    assert row["app_channel_at"] is None


def test_row_apply_invalid_channel_is_400_and_does_not_move(cfg, conn):
    jid = _kept(conn)
    r = _client(cfg).post(f"/jobs/{jid}/stage/apply", data={"channel": "psychic"}, headers=TOKEN)
    assert r.status_code == 400
    assert not _row(conn, jid)["application_state"]


def test_status_route_applied_stores_channel(cfg, conn):
    jid = _kept(conn)
    r = _client(cfg).post(
        f"/jobs/{jid}/status", data={"status": "applied", "channel": "referral"}, headers=TOKEN
    )
    assert r.status_code == 200
    assert _row(conn, jid)["app_channel"] == "referral"


def test_status_route_invalid_channel_is_400(cfg, conn):
    jid = _kept(conn)
    r = _client(cfg).post(
        f"/jobs/{jid}/status", data={"status": "applied", "channel": "nope"}, headers=TOKEN
    )
    assert r.status_code == 400
    assert not _row(conn, jid)["application_state"]


def test_channel_never_touches_jobs_source(cfg, conn):
    jid = _kept(conn)
    _client(cfg).post(f"/jobs/{jid}/stage/apply", data={"channel": "inbound"}, headers=TOKEN)
    assert _row(conn, jid)["source"] == "acme-board"


def test_edit_route_updates_channel_and_via_then_clears(cfg, conn):
    jid = _kept(conn, **APPLIED)
    client = _client(cfg)
    r = client.post(
        f"/jobs/{jid}/channel", data={"channel": "cold", "via": " careers page "}, headers=TOKEN
    )
    assert r.status_code == 200
    row = _row(conn, jid)
    assert (row["app_channel"], row["app_channel_via"]) == ("cold", "careers page")
    assert row["source"] == "acme-board"
    client.post(f"/jobs/{jid}/channel", data={"channel": "", "via": ""}, headers=TOKEN)
    row = _row(conn, jid)
    assert row["app_channel"] is None and row["app_channel_at"] is None


def test_edit_route_invalid_channel_is_400(cfg, conn):
    jid = _kept(conn, **APPLIED)
    r = _client(cfg).post(f"/jobs/{jid}/channel", data={"channel": "x"}, headers=TOKEN)
    assert r.status_code == 400
    assert _row(conn, jid)["app_channel"] is None


def test_edit_route_overlong_via_is_400(cfg, conn):
    jid = _kept(conn, **APPLIED)
    r = _client(cfg).post(
        f"/jobs/{jid}/channel", data={"channel": "cold", "via": "x" * 500}, headers=TOKEN
    )
    assert r.status_code == 400


def test_edit_route_missing_job_is_404(cfg, conn):
    r = _client(cfg).post("/jobs/9999/channel", data={"channel": "cold"}, headers=TOKEN)
    assert r.status_code == 404


def test_edit_route_without_token_is_rejected(cfg, conn):
    jid = _kept(conn, **APPLIED)
    r = _client(cfg).post(f"/jobs/{jid}/channel", data={"channel": "cold"})
    assert r.status_code == 403
    assert _row(conn, jid)["app_channel"] is None


def test_edit_route_foreign_origin_is_rejected(cfg, conn):
    jid = _kept(conn, **APPLIED)
    r = _client(cfg).post(
        f"/jobs/{jid}/channel",
        data={"channel": "cold"},
        headers={**TOKEN, "origin": "http://evil.example.com"},
    )
    assert r.status_code == 403


def test_apply_without_token_is_rejected_and_stores_nothing(cfg, conn):
    jid = _kept(conn)
    r = _client(cfg).post(f"/jobs/{jid}/stage/apply", data={"channel": "cold"})
    assert r.status_code == 403
    assert _row(conn, jid)["app_channel"] is None


def test_job_page_shows_unknown_for_null(cfg, conn):
    jid = _kept(conn, **APPLIED)
    section = _section(_client(cfg).get(f"/jobs/{jid}").text)
    assert "unknown" in section
    assert "selected" not in section.replace('value="" selected', "")


def test_job_page_shows_recorded_channel_and_via(cfg, conn):
    jid = _kept(conn, **APPLIED)
    _client(cfg).post(
        f"/jobs/{jid}/channel",
        data={"channel": "recruiter", "via": "Example Search"},
        headers=TOKEN,
    )
    section = _section(_client(cfg).get(f"/jobs/{jid}").text)
    assert "Example Search" in section
    assert 'value="recruiter" selected' in section


def test_shortlist_row_offers_channel_buttons(cfg, conn):
    jid = _kept(conn)
    page = _client(cfg).get("/shortlist").text
    assert f"/jobs/{jid}/stage/apply" in page
    for channel in ("cold", "referral", "recruiter", "inbound"):
        assert f'"channel": "{channel}"' in page


def test_migration_adds_nullable_columns(conn):
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(jobs)")}
    assert cols["app_channel"]["dflt_value"] is None
    assert cols["app_channel_at"]["dflt_value"] is None
    assert "app_channel_via" in cols

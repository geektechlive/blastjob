"""A truncated job description must not pass for the real one.

Adzuna's API returns the first 500 characters of a description, and a LinkedIn
alert can carry a single line. Scoring and tailoring used to treat that snippet
as the whole posting, so a packet could be "tailored" to a paragraph. The user can
now paste the full description; a rediscovery must never overwrite it with the
snippet again, and tailoring refuses to run from a snippet.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import build as wbuild
from blastjob.web import packets
from blastjob.web import prep as wprep
from blastjob.web.app import create_app
from blastjob.web.runtime import Run
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

SNIPPET = "Acme Talent Partners is seeking a strategic Vice President of Operations " * 7
FULL_JD = "Full description. " * 200


@pytest.fixture
def cfg():
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    (Path(c.paths.data_dir) / "work_history.md").write_text("Senior Ops exec.", encoding="utf-8")
    return c


@pytest.fixture
def conn(cfg):
    conn = dbc.init_db(cfg)
    yield conn
    conn.close()


def _kept_job(conn, raw_jd: str) -> int:
    jid = dbc.upsert_job(
        conn, source="adzuna", external_id="a1", company="Acme", title="VP Ops", raw_jd=raw_jd
    )
    conn.execute("UPDATE jobs SET review_state='kept' WHERE id=?", (jid,))
    conn.commit()
    return jid


def _row(conn, jid: int):
    return conn.execute(
        "SELECT raw_jd, jd_pasted_at, build_state FROM jobs WHERE id=?", (jid,)
    ).fetchone()


def test_short_description_is_a_snippet():
    assert dbc.is_jd_snippet(SNIPPET[:500], "") is True
    assert dbc.is_jd_snippet("", "") is True


def test_long_description_is_not_a_snippet():
    assert dbc.is_jd_snippet(FULL_JD, "") is False


def test_a_pasted_description_is_never_a_snippet_even_when_short():
    assert dbc.is_jd_snippet("Short but real.", "2026-09-13T08:00:00") is False


def test_pasted_description_is_stored_and_stamped(conn):
    jid = _kept_job(conn, SNIPPET[:500])
    assert dbc.set_pasted_jd(conn, jid, FULL_JD) is True
    row = _row(conn, jid)
    assert row["raw_jd"] == FULL_JD.strip()
    assert row["jd_pasted_at"]


def test_blank_paste_is_rejected_and_changes_nothing(conn):
    jid = _kept_job(conn, SNIPPET[:500])
    assert dbc.set_pasted_jd(conn, jid, "   \n ") is False
    row = _row(conn, jid)
    assert row["raw_jd"] == SNIPPET[:500]
    assert row["jd_pasted_at"] == ""


def test_rediscovery_does_not_overwrite_a_pasted_description(conn):
    jid = _kept_job(conn, SNIPPET[:500])
    dbc.set_pasted_jd(conn, jid, FULL_JD)
    dbc.upsert_job(
        conn,
        source="adzuna",
        external_id="a1",
        company="Acme",
        title="VP Ops",
        raw_jd=SNIPPET[:500],
    )
    assert _row(conn, jid)["raw_jd"] == FULL_JD.strip()


def test_rediscovery_still_refreshes_an_unpasted_description(conn):
    jid = _kept_job(conn, SNIPPET[:500])
    dbc.upsert_job(
        conn, source="adzuna", external_id="a1", company="Acme", title="VP Ops", raw_jd="v2"
    )
    assert _row(conn, jid)["raw_jd"] == "v2"


async def test_tailor_refuses_a_snippet_and_leaves_build_state_alone(cfg, conn, monkeypatch):
    jid = _kept_job(conn, SNIPPET[:500])
    called = False

    async def fake_run_build(**kwargs):
        nonlocal called
        called = True
        return Path("/tmp/out"), None

    monkeypatch.setattr("blastjob.core.build.run_build", fake_run_build)
    run = Run(id="tailor-snip", name="tailor")
    result = await wbuild.make_tailor_work(jid, load_config=lambda: cfg)(run)

    assert result is None
    assert called is False
    assert _row(conn, jid)["build_state"] == "none"
    logs = " ".join(e.data for e in run.events if e.kind == "log")
    assert "paste" in logs.lower()


async def test_tailor_runs_once_the_full_description_is_pasted(cfg, conn, monkeypatch):
    jid = _kept_job(conn, SNIPPET[:500])
    dbc.set_pasted_jd(conn, jid, FULL_JD)
    seen: dict = {}

    async def fake_run_build(**kwargs):
        seen["jd"] = kwargs["job_description"]
        return Path("/tmp/out"), None

    monkeypatch.setattr("blastjob.core.build.run_build", fake_run_build)
    run = Run(id="tailor-full", name="tailor")
    result = await wbuild.make_tailor_work(jid, load_config=lambda: cfg)(run)

    assert result["ok"] is True
    assert seen["jd"] == FULL_JD.strip()


def test_paste_route_saves_and_detail_page_drops_the_snippet_warning(cfg):
    conn = dbc.init_db(cfg)
    jid = _kept_job(conn, SNIPPET[:500])
    conn.close()
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))

    before = client.get(f"/jobs/{jid}")
    assert "Paste the full description" in before.text

    r = client.post(f"/jobs/{jid}/jd", data={"jd": FULL_JD}, headers={CSRF_HEADER: LAUNCH_TOKEN})
    assert r.status_code == 200

    conn = dbc.init_db(cfg)
    assert _row(conn, jid)["raw_jd"] == FULL_JD.strip()
    conn.close()
    after = client.get(f"/jobs/{jid}")
    assert "Paste the full description" not in after.text


def test_retailor_on_a_snippet_keeps_the_existing_packet_state(cfg):
    conn = dbc.init_db(cfg)
    jid = _kept_job(conn, SNIPPET[:500])
    conn.execute("UPDATE jobs SET build_state='tailored' WHERE id=?", (jid,))
    conn.commit()
    conn.close()
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))

    r = client.post(f"/jobs/{jid}/retailor", headers={CSRF_HEADER: LAUNCH_TOKEN})
    assert r.status_code == 200

    conn = dbc.init_db(cfg)
    assert _row(conn, jid)["build_state"] == "tailored"
    conn.close()


def _done_build(conn, jid: int, artifact_dir: Path, built_at: str) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    conn.execute(
        "INSERT INTO build_runs (job_id, run_id, status, lease_expires_at, artifact_dir, "
        "created_at, updated_at) VALUES (?, ?, 'done', ?, ?, ?, ?)",
        (jid, f"run-{built_at}", built_at, str(artifact_dir), built_at, built_at),
    )
    conn.commit()


def test_packet_built_from_a_snippet_is_flagged_for_retailoring(cfg, conn):
    jid = _kept_job(conn, SNIPPET[:500])
    _done_build(conn, jid, Path(cfg.paths.data_dir) / "pkt", datetime.now().isoformat())

    packet = packets.packet_for_job(conn, jid, load_config=lambda: cfg)

    assert packet["stale"] is True
    assert "snippet" in packet["stale_reason"]


def test_packet_built_before_the_paste_is_flagged(cfg, conn):
    jid = _kept_job(conn, SNIPPET[:500])
    _done_build(conn, jid, Path(cfg.paths.data_dir) / "pkt", datetime.now().isoformat())
    dbc.set_pasted_jd(conn, jid, FULL_JD)

    packet = packets.packet_for_job(conn, jid, load_config=lambda: cfg)

    assert packet["stale"] is True
    assert "pasted" in packet["stale_reason"]


def test_packet_rebuilt_after_the_paste_is_not_flagged(cfg, conn):
    jid = _kept_job(conn, SNIPPET[:500])
    dbc.set_pasted_jd(conn, jid, FULL_JD)
    later = (datetime.now() + timedelta(seconds=5)).isoformat()
    _done_build(conn, jid, Path(cfg.paths.data_dir) / "pkt", later)

    packet = packets.packet_for_job(conn, jid, load_config=lambda: cfg)

    assert packet["stale"] is False


async def test_prep_refuses_a_snippet(cfg, conn, monkeypatch):
    jid = _kept_job(conn, SNIPPET[:500])
    called = False

    async def fake_generate_prep(*args, **kwargs):
        nonlocal called
        called = True
        return Path("/tmp/prep.md")

    monkeypatch.setattr("blastjob.core.prep.generate_prep", fake_generate_prep)
    run = Run(id="prep-snip", name="prep")
    result = await wprep.make_prep_work(jid, load_config=lambda: cfg)(run)

    assert result is None
    assert called is False
    logs = " ".join(e.data for e in run.events if e.kind == "log")
    assert "paste" in logs.lower()


def test_paste_route_requires_the_csrf_token(cfg):
    conn = dbc.init_db(cfg)
    jid = _kept_job(conn, SNIPPET[:500])
    conn.close()
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))
    r = client.post(f"/jobs/{jid}/jd", data={"jd": FULL_JD})
    assert r.status_code == 403

"""Releasing a quarantined packet: allowed, but never silent.

The gate quarantines on mechanical failures, and it can be wrong — the user
knows things it does not. So the override exists. What it may not do is happen
without a stated reason, because the audit line is the whole point: a packet
that went out over a failed check should say so, in the user's own words, forever.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blastjob.core import quarantine
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

TOKEN = {CSRF_HEADER: LAUNCH_TOKEN}


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


def _job_with_packet(conn, cfg) -> tuple[int, Path]:
    """A kept job whose packet exists on disk and is quarantined."""
    # Title and file tokens agree: release frees only the job's own role's files.
    jid = dbc.upsert_job(conn, source="s1", external_id="e1", company="Acme", title="VP IT")
    out = Path(cfg.paths.output_dir) / "acme-2026-09-16"
    out.mkdir(parents=True)
    stem = "Alex_Rivera_Resume_-_Acme_VP_IT"
    (out / f"{stem}.md").write_text("# Resume", encoding="utf-8")
    (out / f"{stem}.docx").write_text("docx", encoding="utf-8")
    (out / "vp-it.packet_report.json").write_text('{"state": "quarantined"}', encoding="utf-8")
    conn.execute(
        "INSERT INTO build_runs (job_id, run_id, status, artifact_dir, lease_expires_at, "
        "created_at, updated_at) VALUES (?,?,?,?,datetime('now'),datetime('now'),datetime('now'))",
        (jid, "tailor-1", "done", str(out)),
    )
    conn.commit()
    quarantine.quarantine(out, stems=[stem], reasons=["banned characters in the resume"])
    return jid, out


def test_release_refuses_an_empty_reason(cfg, conn):
    jid, out = _job_with_packet(conn, cfg)
    r = _client(cfg).post(f"/jobs/{jid}/packet/release", data={"reason": "   "}, headers=TOKEN)
    assert r.status_code == 400
    assert quarantine.is_quarantined(out), "documents must stay quarantined"


def test_release_requires_csrf(cfg, conn):
    jid, out = _job_with_packet(conn, cfg)
    r = _client(cfg).post(f"/jobs/{jid}/packet/release", data={"reason": "sending anyway"})
    assert r.status_code == 403
    assert quarantine.is_quarantined(out)


def test_release_moves_the_documents_back_and_records_the_reason(cfg, conn):
    jid, out = _job_with_packet(conn, cfg)
    r = _client(cfg).post(
        f"/jobs/{jid}/packet/release",
        data={"reason": "the flagged character is in the company name"},
        headers=TOKEN,
    )
    assert r.status_code == 200
    assert not quarantine.is_quarantined(out)
    assert (out / "Alex_Rivera_Resume_-_Acme_VP_IT.docx").exists()
    # RELEASED.txt sits at the packet root, not inside quarantine/, so the
    # audit survives the quarantine folder being emptied or removed.
    audit = (out / "RELEASED.txt").read_text(encoding="utf-8")
    assert "the flagged character is in the company name" in audit


def test_release_on_a_packet_that_is_not_quarantined_is_refused(cfg, conn):
    jid, out = _job_with_packet(conn, cfg)
    client = _client(cfg)
    client.post(f"/jobs/{jid}/packet/release", data={"reason": "first"}, headers=TOKEN)
    again = client.post(f"/jobs/{jid}/packet/release", data={"reason": "second"}, headers=TOKEN)
    assert again.status_code == 400


def test_release_on_a_job_with_no_packet_is_a_404(cfg, conn):
    jid = dbc.upsert_job(conn, source="s2", external_id="e2", company="NoPacket", title="VP Ops")
    r = _client(cfg).post(f"/jobs/{jid}/packet/release", data={"reason": "why"}, headers=TOKEN)
    assert r.status_code == 404


def test_release_transitions_the_sidecar_to_acknowledged_override(cfg, conn):
    """The folder and the report must agree. Before this, release moved the
    documents back but the sidecar still read "quarantined", so the page kept
    showing the held badge and offering Release again."""
    import json

    jid, out = _job_with_packet(conn, cfg)
    sidecar = out / "vp-it.packet_report.json"
    sidecar.write_text(
        json.dumps({"state": "quarantined", "sendable": False, "reasons": ["banned characters"]}),
        encoding="utf-8",
    )

    r = _client(cfg).post(
        f"/jobs/{jid}/packet/release",
        data={"reason": "the flagged character is in the company's own name"},
        headers=TOKEN,
    )
    assert r.status_code == 200

    after = json.loads(sidecar.read_text(encoding="utf-8"))
    assert after["state"] == "acknowledged_override"
    assert after["sendable"] is True
    assert after["override"]["reason"] == "the flagged character is in the company's own name"
    # Why it was held stays on the record.
    assert after["reasons"] == ["banned characters"]


def test_release_runs_under_the_shared_filing_lock_on_a_filed_packet(cfg, conn, monkeypatch):
    from contextlib import asynccontextmanager

    from blastjob.core import filing_lock

    jid, out = _job_with_packet(conn, cfg)
    filed = out.parent / "Submitted" / out.name
    filed.parent.mkdir()
    out.rename(filed)
    state = {"held": False, "released_in_lock": None}
    real_shared = filing_lock.shared_async
    real_release = quarantine.release

    @asynccontextmanager
    async def spy_shared(root):
        async with real_shared(root):
            state["held"] = True
            try:
                yield
            finally:
                state["held"] = False

    def spy_release(target, **kw):
        state["released_in_lock"] = (state["held"], target)
        return real_release(target, **kw)

    monkeypatch.setattr(filing_lock, "shared_async", spy_shared)
    monkeypatch.setattr(quarantine, "release", spy_release)
    r = _client(cfg).post(
        f"/jobs/{jid}/packet/release", data={"reason": "sending anyway"}, headers=TOKEN
    )
    assert r.status_code == 200
    held, target = state["released_in_lock"]
    assert held and target == filed.resolve()

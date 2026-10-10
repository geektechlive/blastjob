"""Outcomes reach the send record from every path that ends an application.

`web/stage_moves` covers advance/withdraw/reopen. The two paths that actually
record a rejection are elsewhere: the manual status dropdown
(`POST /jobs/{id}/status`) and the closure lifecycle. Without these, an
application could die and the packet that was sent would show no ending.
"""

from __future__ import annotations

import datetime
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blastjob.core import closure
from blastjob.core import packet_sends as ps
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


def _applied_job_with_send(conn, cfg) -> tuple[int, int]:
    """A kept, applied job whose packet was recorded as sent."""
    jid = dbc.upsert_job(conn, source="s1", external_id="e1", company="Acme", title="VP Ops")
    conn.execute(
        "UPDATE jobs SET review_state='kept', application_state='applied', applied_at=?, "
        "raw_jd='a job description' WHERE id=?",
        (datetime.date.today().isoformat(), jid),
    )
    conn.commit()
    out = Path(cfg.paths.output_dir) / "acme-2026-09-16"
    out.mkdir(parents=True)
    resume = out / "Alex_Rivera_Resume_-_Acme_VP_Ops.md"
    resume.write_text("# Resume", encoding="utf-8")
    send_id = ps.record_send(conn, jid, str(out), resume)
    return jid, send_id


def _outcomes(conn, send_id: int) -> list[str]:
    return [
        r["outcome"]
        for r in conn.execute(
            "SELECT outcome FROM packet_outcomes WHERE send_id=? ORDER BY id", (send_id,)
        )
    ]


def test_marking_rejected_records_the_outcome_against_the_sent_packet(cfg, conn):
    jid, send_id = _applied_job_with_send(conn, cfg)
    r = _client_post(cfg, f"/jobs/{jid}/status", {"status": "rejected"})
    assert r.status_code == 200
    assert _outcomes(conn, send_id) == ["rejected"]


def test_closure_records_posting_closed_against_the_sent_packet(cfg, conn):
    jid, send_id = _applied_job_with_send(conn, cfg)
    conn.execute("UPDATE jobs SET application_state='' WHERE id=?", (jid,))
    conn.commit()
    closure.close_transition(conn, jid, now=datetime.datetime.now(), auto=True)
    assert _outcomes(conn, send_id) == [closure.POSTING_CLOSED]


def test_a_job_with_no_send_still_closes(cfg, conn):
    """Bookkeeping never blocks the lifecycle: no send, no outcome, no error."""
    jid = dbc.upsert_job(conn, source="s2", external_id="e2", company="NoSend", title="VP Ops")
    conn.execute("UPDATE jobs SET review_state='kept', application_state='' WHERE id=?", (jid,))
    conn.commit()
    assert closure.close_transition(conn, jid, now=datetime.datetime.now(), auto=True) is True
    assert conn.execute("SELECT COUNT(*) c FROM packet_outcomes").fetchone()["c"] == 0


def _client_post(cfg, path: str, data: dict):
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda _c: None))
    return client.post(path, data=data, headers=TOKEN)


# ----- the job page must record a send too -----------------------------------
#
# Found 2026-09-16: `record_send` was reachable only from the Shortlist list-row
# button (POST /jobs/{id}/stage/apply -> mark_applied). The job DETAIL page's
# Applied control posts /jobs/{id}/status, which recorded an outcome against a
# send that did not exist. As-sent attribution -- the feature this program was
# built for -- had never fired from the page the user actually uses to read a job.


def test_marking_applied_from_the_job_page_records_a_send(cfg, conn):
    jid = dbc.upsert_job(conn, source="s9", external_id="e9", company="Acme", title="VP Ops")
    conn.execute(
        "UPDATE jobs SET review_state='kept', application_state='', raw_jd='jd' WHERE id=?", (jid,)
    )
    conn.commit()
    out = Path(cfg.paths.output_dir) / "acme-2026-09-16"
    out.mkdir(parents=True)
    (out / "Alex_Rivera_Resume_-_Acme_VP_Ops.docx").write_text("x", encoding="utf-8")
    (out / "vp-ops.packet_report.json").write_text(
        '{"state": "ready", "groundedness": 97}', encoding="utf-8"
    )
    conn.execute(
        "INSERT INTO build_runs (job_id, run_id, status, artifact_dir, lease_expires_at, "
        "created_at, updated_at) VALUES (?,?,?,?,datetime('now'),datetime('now'),datetime('now'))",
        (jid, "tailor-9", "done", str(out)),
    )
    conn.commit()

    r = _client_post(cfg, f"/jobs/{jid}/status", {"status": "applied"})
    assert r.status_code == 200
    sends = conn.execute("SELECT COUNT(*) c FROM packet_sends WHERE job_id=?", (jid,)).fetchone()
    assert sends["c"] == 1, "the job page must snapshot what was sent, like the list row does"


def test_a_job_page_apply_with_no_packet_still_moves_and_says_why(cfg, conn):
    """Bookkeeping never blocks the move, but the silence has to be visible:
    before this, nothing was recorded and nothing was said."""
    jid = dbc.upsert_job(conn, source="s10", external_id="e10", company="NoPack", title="VP Ops")
    conn.execute("UPDATE jobs SET review_state='kept', application_state='' WHERE id=?", (jid,))
    conn.commit()

    r = _client_post(cfg, f"/jobs/{jid}/status", {"status": "applied"})
    assert r.status_code == 200
    assert conn.execute("SELECT COUNT(*) c FROM packet_sends").fetchone()["c"] == 0
    row = conn.execute("SELECT application_state FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["application_state"] == "applied"
    assert "no as-sent snapshot" in r.text.lower() or "not recorded" in r.text.lower()


def test_a_failure_recording_the_send_still_applies_the_job_and_says_so(
    cfg, conn, monkeypatch, caplog
):
    """Deliberately NOT atomic, and the reasoning is worth keeping.

    Rolling the state change back would mean a disk error stops the user recording
    that they applied to a job -- bookkeeping blocking the fact it exists to
    record. This codebase's rule is the opposite: the state change always wins.
    What used to be wrong is that the failure was SILENT. Now the move stands
    and the missing snapshot is reported, so it can be fixed rather than
    discovered months later as a gap in the record.
    """
    import logging

    from blastjob.core import packet_sends as ps
    from blastjob.web import stage_moves as sm

    jid, _send = _applied_job_with_send(conn, cfg)
    conn.execute("UPDATE jobs SET application_state='' WHERE id=?", (jid,))
    conn.execute("DELETE FROM packet_sends WHERE job_id=?", (jid,))
    conn.commit()

    def _boom(*_a, **_kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ps, "record_send", _boom)
    with caplog.at_level(logging.INFO):
        assert sm.mark_applied(conn, jid) is True

    state = conn.execute("SELECT application_state FROM jobs WHERE id=?", (jid,)).fetchone()
    assert state["application_state"] == "applied", "the move must never be blocked"
    assert conn.execute("SELECT COUNT(*) c FROM packet_sends").fetchone()["c"] == 0
    assert "without an as-sent snapshot" in caplog.text, "the gap must not be silent"

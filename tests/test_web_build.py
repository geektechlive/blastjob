"""Phase 4 tests: SSE framing, tailor build (CAS + finish), outreach draft.

The heavy calls (run_build, make_stream) are stubbed so these are deterministic
and offline; the work functions are driven directly to avoid TestClient/event-loop
timing on background tasks.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import build as wbuild
from blastjob.web.runtime import Event, Run


def test_sse_format_multiline():
    ev = Event(id=3, kind="output", data="line1\nline2")
    frame = wbuild.sse_format(ev)
    assert frame == "id: 3\nevent: output\ndata: line1\ndata: line2\n\n"


def test_sse_format_empty_data():
    frame = wbuild.sse_format(Event(id=1, kind="done", data=""))
    assert frame == "id: 1\nevent: done\ndata: \n\n"


@pytest.fixture
def cfg_with_kept_job():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    (Path(cfg.paths.data_dir)).mkdir(parents=True, exist_ok=True)
    (Path(cfg.paths.data_dir) / "work_history.md").write_text("Senior IT exec.", encoding="utf-8")
    conn = dbc.init_db(cfg)
    jid = dbc.upsert_job(
        conn,
        source="t",
        external_id="1",
        company="Acme",
        title="VP Ops",
        # Long enough to be a real description: a snippet is refused (test_jd_snippet).
        raw_jd="Lead Ops. " * 100,
    )
    conn.execute("UPDATE jobs SET review_state='kept' WHERE id=?", (jid,))
    conn.commit()
    conn.close()
    return cfg, jid


async def test_tailor_build_claims_and_finishes(cfg_with_kept_job, monkeypatch):
    cfg, jid = cfg_with_kept_job

    async def fake_run_build(**kwargs):
        kwargs["on_text"]("resume line 1\n")
        kwargs["on_text"]("resume line 2\n")
        return Path("/tmp/out/acme"), None

    monkeypatch.setattr("blastjob.core.build.run_build", fake_run_build)

    run = Run(id="tailor-1", name="tailor")
    result = await wbuild.make_tailor_work(jid, load_config=lambda: cfg)(run)
    assert result["ok"] is True

    kinds = [e.kind for e in run.events]
    assert "output" in kinds
    outputs = "".join(e.data for e in run.events if e.kind == "output")
    assert "resume line 1" in outputs

    conn = dbc.init_db(cfg)
    bs = conn.execute("SELECT build_state FROM jobs WHERE id=?", (jid,)).fetchone()["build_state"]
    conn.close()
    assert bs == "tailored"


async def test_tailor_build_double_claim_blocked(cfg_with_kept_job, monkeypatch):
    cfg, jid = cfg_with_kept_job
    # Put the job into 'tailoring' as if a build already holds the lease.
    conn = dbc.init_db(cfg)
    dbc.claim_for_build(conn, jid, "other-run")
    conn.close()

    async def fake_run_build(**kwargs):  # must NOT be called
        raise AssertionError("run_build should not run when already claimed")

    monkeypatch.setattr("blastjob.core.build.run_build", fake_run_build)
    run = Run(id="tailor-2", name="tailor")
    await wbuild.make_tailor_work(jid, load_config=lambda: cfg)(run)
    assert any("already in progress" in e.data for e in run.events)


async def test_outreach_draft_streams_unaddressed(cfg_with_kept_job, monkeypatch):
    cfg, jid = cfg_with_kept_job

    class FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        @property
        def text_stream(self):
            async def gen():
                for t in ["Dear Hiring Manager,\n", "I am interested.\n"]:
                    yield t

            return gen()

    async def fake_make_stream(cfg_, system, messages, max_tokens=1024):
        return FakeStream()

    monkeypatch.setattr(wbuild, "make_stream", fake_make_stream)
    run = Run(id="outreach-1", name="outreach")
    result = await wbuild.make_outreach_work(jid, load_config=lambda: cfg)(run)
    assert "Dear Hiring Manager" in result["email"]


async def test_a_hold_that_could_not_complete_fails_the_build_but_keeps_its_folder(
    cfg_with_kept_job, monkeypatch, tmp_path
):
    """run_build raises PacketNotQuarantinedError when a held document stayed
    public. The build is failed (never 'tailored'), but its folder is still
    recorded so the job page can point at the packet instead of orphaning it."""
    from blastjob.core.packet_verify import PacketNotQuarantinedError

    cfg, jid = cfg_with_kept_job

    async def fake_run_build(**kwargs):
        raise PacketNotQuarantinedError("resume.docx stayed public", tmp_path)

    monkeypatch.setattr("blastjob.core.build.run_build", fake_run_build)
    run = Run(id="tailor-hold", name="tailor")
    result = await wbuild.make_tailor_work(jid, load_config=lambda: cfg)(run)

    assert result["ok"] is False
    conn = dbc.init_db(cfg)
    row = conn.execute(
        "SELECT status, artifact_dir FROM build_runs WHERE run_id='tailor-hold'"
    ).fetchone()
    bs = conn.execute("SELECT build_state FROM jobs WHERE id=?", (jid,)).fetchone()["build_state"]
    conn.close()
    assert row["artifact_dir"] == str(tmp_path)
    assert bs != "tailored"
    assert any(e.kind == "error" and "stayed public" in e.data for e in run.events)

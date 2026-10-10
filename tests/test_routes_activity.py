"""The Activity + Files panel on the job page (`web/routes_activity.py`).

The Files list may link a document only when the existing download route
(`GET /jobs/{id}/files/{name}`) would serve THAT file -- not merely a file of
the same name. Both sides go through `web/downloads.resolve_download`.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blastjob.core import artifacts, events
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.downloads import resolve_download
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

H = {CSRF_HEADER: LAUNCH_TOKEN}


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
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))


def _job(conn, ext: str = "e1") -> int:
    jid = dbc.upsert_job(conn, source="s1", external_id=ext, company="Acme", title="VP IT")
    conn.execute("UPDATE jobs SET review_state='kept' WHERE id=?", (jid,))
    conn.commit()
    return jid


def _packet(conn, cfg, jid: int, name: str, run_id: str) -> Path:
    folder = Path(cfg.paths.output_dir) / name
    folder.mkdir()
    dbc.claim_for_build(conn, jid, run_id)
    dbc.finish_build(conn, run_id, artifact_dir=str(folder), ok=True)
    conn.execute("UPDATE jobs SET build_state='none' WHERE id=?", (jid,))
    conn.commit()
    return folder


def _file(folder: Path, name: str, text: str = "x") -> Path:
    p = folder / name
    p.write_text(text, encoding="utf-8")
    return p


# --- events routes ---------------------------------------------------------------


def test_add_event_route_records_a_manual_event(client, conn):
    jid = _job(conn)
    r = client.post(
        f"/jobs/{jid}/events",
        data={"kind": "thank_you", "at": "2026-10-08", "note": "sent to panel"},
        headers=H,
    )
    assert r.status_code == 200
    assert "Thank-you sent" in r.text
    got = events.events_for(conn, jid)
    assert [(e["kind"], e["at"], e["source"]) for e in got] == [
        ("thank_you", "2026-10-08", "manual")
    ]


@pytest.mark.parametrize(
    "form",
    [
        {"kind": "lunch", "at": "2026-10-08"},
        {"kind": "note", "at": "next tuesday"},
        {"kind": "note", "at": ""},
        {"kind": "note", "at": "2026-10-08", "note": "x" * 2001},
    ],
)
def test_add_event_route_rejects_bad_input(client, conn, form):
    jid = _job(conn)
    assert client.post(f"/jobs/{jid}/events", data=form, headers=H).status_code == 400
    assert events.events_for(conn, jid) == []


def test_add_event_route_requires_csrf_and_a_real_job(client, conn):
    jid = _job(conn)
    form = {"kind": "note", "at": "2026-10-08"}
    assert client.post(f"/jobs/{jid}/events", data=form).status_code == 403
    bad_origin = {**H, "origin": "https://evil.example"}
    assert client.post(f"/jobs/{jid}/events", data=form, headers=bad_origin).status_code == 403
    assert client.post("/jobs/9999/events", data=form, headers=H).status_code == 404
    assert events.events_for(conn, jid) == []


def test_delete_route_only_deletes_this_jobs_manual_events(client, conn):
    one, two = _job(conn, "one"), _job(conn, "two")
    theirs = events.add_event(conn, two, "note", "2026-10-01")
    state = events.add_event(conn, one, "applied", "2026-10-01", source="state")
    mine = events.add_event(conn, one, "note", "2026-10-01")
    assert client.post(f"/jobs/{one}/events/{theirs}/delete", headers=H).status_code == 404
    assert client.post(f"/jobs/{one}/events/{state}/delete", headers=H).status_code == 404
    assert client.post(f"/jobs/{one}/events/{mine}/delete").status_code == 403
    assert client.post(f"/jobs/{one}/events/{mine}/delete", headers=H).status_code == 200
    assert [e["id"] for e in events.events_for(conn, one)] == [state]
    assert [e["id"] for e in events.events_for(conn, two)] == [theirs]


def test_activity_panel_lists_events_newest_first_with_the_caption(client, conn):
    jid = _job(conn)
    events.add_event(conn, jid, "applied", "2026-09-01", source="backfill")
    events.add_event(conn, jid, "interview", "2026-09-20", note="with the CIO")
    page = client.get(f"/jobs/{jid}/activity").text
    assert "dates before 2026-10-09 reconstructed from the record" in page
    assert page.index("Interview") < page.index("Applied")
    assert "with the CIO" in page
    # manual events get a delete button, reconstructed ones do not
    assert page.count("/delete") == 1


def test_activity_panel_renders_a_ref_only_when_it_is_a_safe_url(client, conn):
    jid = _job(conn)
    events.add_event(conn, jid, "note", "2026-10-01", ref="javascript:alert(1)")
    events.add_event(conn, jid, "note", "2026-10-02", ref="https://example.com/thread")
    page = client.get(f"/jobs/{jid}/activity").text
    assert 'href="https://example.com/thread"' in page
    assert "javascript:" not in page


def test_detail_page_lazy_loads_the_panel_for_kept_jobs(client, conn):
    jid = _job(conn)
    page = client.get(f"/jobs/{jid}").text
    assert f'hx-get="/jobs/{jid}/activity"' in page


# --- files list ------------------------------------------------------------------


def test_files_link_a_packet_document_the_route_serves(client, conn, cfg):
    jid = _job(conn)
    folder = _packet(conn, cfg, jid, "acme-2026-09-01", "run-1")
    doc = _file(folder, "vp-it.interview_prep.md")
    artifacts.register(conn, doc, "interview_prep", job_id=jid)
    page = client.get(f"/jobs/{jid}/activity").text
    assert f'href="/jobs/{jid}/files/vp-it.interview_prep.md"' in page
    assert client.get(f"/jobs/{jid}/files/vp-it.interview_prep.md").status_code == 200


def test_files_never_link_a_basename_collision(client, conn, cfg, tmp_path):
    """The index holds /outside/transcript.md; the packet holds a DIFFERENT
    transcript.md. The route would serve the packet copy, so no link."""
    jid = _job(conn)
    folder = _packet(conn, cfg, jid, "acme-2026-09-01", "run-1")
    _file(folder, "transcript.md", "packet copy")
    outside = _file(tmp_path, "transcript.md", "the real transcript")
    artifacts.register(conn, outside, "transcript", job_id=jid)
    page = client.get(f"/jobs/{jid}/activity").text
    assert f"/jobs/{jid}/files/transcript.md" not in page
    assert str(outside.resolve()) in page


def test_files_never_link_an_older_packet_copy_the_newer_one_shadows(client, conn, cfg):
    jid = _job(conn)
    old = _packet(conn, cfg, jid, "acme-2026-09-01", "run-1")
    new = _packet(conn, cfg, jid, "acme-2026-09-20", "run-2")
    old_doc = _file(old, "vp-it.fit_score.md", "old")
    _file(new, "vp-it.fit_score.md", "new")
    artifacts.register(conn, old_doc, "fit_score", job_id=jid)
    assert client.get(f"/jobs/{jid}/files/vp-it.fit_score.md").text == "new"
    page = client.get(f"/jobs/{jid}/activity").text
    assert f"/jobs/{jid}/files/vp-it.fit_score.md" not in page


def test_files_never_link_a_quarantined_document(client, conn, cfg):
    jid = _job(conn)
    folder = _packet(conn, cfg, jid, "acme-2026-09-01", "run-1")
    (folder / "quarantine").mkdir()
    held = _file(folder / "quarantine", "Alex_Resume_-_Acme_VP_IT.docx")
    artifacts.register(conn, held, "resume", job_id=jid)
    page = client.get(f"/jobs/{jid}/activity").text
    assert "/files/Alex_Resume_-_Acme_VP_IT.docx" not in page


def test_resolve_download_matches_the_route(conn, cfg):
    jid = _job(conn)
    folder = _packet(conn, cfg, jid, "acme-2026-09-01", "run-1")
    doc = _file(folder, "a.md")
    assert resolve_download(conn, cfg, jid, "a.md").resolve() == doc.resolve()
    assert resolve_download(conn, cfg, jid, "../a.md") is None
    assert resolve_download(conn, cfg, jid, "missing.md") is None

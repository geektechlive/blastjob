"""Packets must lead somewhere.

The Packets page was a static table: no link to the job, no way to open the
resume, and nothing another device could use, because "Reveal in Finder" only works on
the computer running blastjob. Each packet row now links to its job and to its documents, and every
file in a job's packet can be opened from the browser, but only files that
really are in that job's packet folder under the configured output directory.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import packets
from blastjob.web.app import create_app

TITLE = "Vice President, Business Governance"
RESUME = "Alex_Rivera_Resume_-_Acme_Vice_President_Business_Governance.docx"
COVER = "Alex_Rivera_Cover_Letter_-_Acme_Vice_President_Business_Governance.docx"
OTHER_RESUME = "Alex_Rivera_Resume_-_Acme_Director_of_Data.docx"


@pytest.fixture
def cfg():
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    return c


def _packet(cfg, *, title: str = TITLE) -> tuple[int, Path]:
    conn = dbc.init_db(cfg)
    jid = dbc.upsert_job(conn, source="s", external_id="e1", company="Acme", title=title)
    conn.execute("UPDATE jobs SET review_state='kept' WHERE id=?", (jid,))
    conn.commit()
    folder = Path(cfg.paths.output_dir) / "acme-2026-09-12"
    folder.mkdir(parents=True)
    for name in (RESUME, COVER, OTHER_RESUME):
        (folder / name).write_bytes(b"docx-bytes")
    (folder / "resume-notes.md").write_text("# Resume\nplain text", encoding="utf-8")
    meta = {
        "company": "Acme",
        "role": title,
        "timestamp": "2026-09-12T11:22:00",
        "formats": ["docx"],
    }
    (folder / "vice-president-it-governance.metadata.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    dbc.claim_for_build(conn, jid, "run-1")
    dbc.finish_build(conn, "run-1", artifact_dir=str(folder), ok=True)
    conn.close()
    return jid, folder


@pytest.fixture
def client(cfg):
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))


def test_a_packet_file_can_be_opened_from_the_browser(cfg, client):
    jid, _ = _packet(cfg)
    r = client.get(f"/jobs/{jid}/files/{RESUME}")
    assert r.status_code == 200
    assert r.content == b"docx-bytes"


def test_markdown_opens_as_readable_text(cfg, client):
    jid, _ = _packet(cfg)
    r = client.get(f"/jobs/{jid}/files/resume-notes.md")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "plain text" in r.text


def test_a_file_outside_the_packet_folder_is_refused(cfg, client):
    jid, folder = _packet(cfg)
    (folder.parent / "secret.txt").write_text("nope", encoding="utf-8")
    assert client.get(f"/jobs/{jid}/files/..%2Fsecret.txt").status_code == 404
    assert client.get(f"/jobs/{jid}/files/secret.txt").status_code == 404


def test_a_file_for_a_job_with_no_packet_is_404(cfg, client):
    conn = dbc.init_db(cfg)
    jid = dbc.upsert_job(conn, source="s", external_id="e9", company="Acme", title="VP")
    conn.close()
    assert client.get(f"/jobs/{jid}/files/{RESUME}").status_code == 404


def test_job_page_lists_packet_files_as_links(cfg, client):
    jid, _ = _packet(cfg)
    page = client.get(f"/jobs/{jid}").text
    assert f'href="/jobs/{jid}/files/{RESUME}"' in page


def test_role_documents_pick_this_roles_resume_in_a_shared_folder(cfg):
    _, folder = _packet(cfg)
    docs = packets.role_documents(packets.packet_files(folder), TITLE)
    assert docs["resume"] == RESUME
    assert docs["cover"] == COVER


def test_role_documents_match_a_filename_the_builder_truncated(tmp_path):
    long_title = (
        "Group Vice President Business Operations and Enterprise Transformation- "
        "Defense & Space Division"
    )
    resume = "Alex_Rivera_Resume_-_Meridian_Group_Vice_President_Business_Operations_and_Ent.docx"
    other = "Alex_Rivera_Resume_-_Meridian_Director_of_Supply_Chain.docx"
    for name in (resume, other):
        (tmp_path / name).write_bytes(b"x")
    docs = packets.role_documents(packets.packet_files(tmp_path), long_title)
    assert docs == {"resume": resume}


def test_role_documents_read_the_legacy_layout(tmp_path):
    for name in ("resume.docx", "cover_letter.docx", "resume.md"):
        (tmp_path / name).write_bytes(b"x")
    docs = packets.role_documents(packets.packet_files(tmp_path), "Chief Operations Officer")
    assert docs == {"resume": "resume.docx", "cover": "cover_letter.docx"}


def test_packets_page_links_each_row_to_its_job_and_documents(cfg, client):
    jid, _ = _packet(cfg)
    page = client.get("/history").text
    assert f'href="/jobs/{jid}"' in page
    assert f'href="/jobs/{jid}/files/{RESUME}"' in page
    assert f'href="/jobs/{jid}/files/{COVER}"' in page
    assert OTHER_RESUME not in page


def test_packets_page_row_without_a_job_is_still_listed(cfg, client):
    folder = Path(cfg.paths.output_dir) / "2026-04-21" / "old-co" / "vp"
    folder.mkdir(parents=True)
    meta = {"company": "Old Co", "role": "VP", "timestamp": "2026-04-21T09:00:00"}
    (folder / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    page = client.get("/history").text
    assert "Old Co" in page
    assert "not linked to a job" in page


def test_a_running_refine_claim_is_not_shown_as_the_latest_build(cfg):
    """A refine claims the job through a `refine:` build_runs row; that is a lock,
    not a build, so the job page must keep showing the packet as current."""
    jid, folder = _packet(cfg)
    conn = dbc.init_db(cfg)
    assert dbc.claim_for_refine(conn, jid, "r-1")
    packet = packets.packet_for_job(conn, jid, load_config=lambda: cfg)
    conn.close()
    assert packet is not None
    assert packet["dir"] == str(folder)
    assert not packet.get("previous"), "a refine lock is not a newer build attempt"

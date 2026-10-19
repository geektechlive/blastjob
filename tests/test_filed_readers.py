"""Every reader of a stored packet path finds the packet after it is filed
(issue #20), and every write stores the ROOT form. Invented data only."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blastjob.core import artifacts, followup
from blastjob.core import packet_filing as pf
from blastjob.core.paths import SUBMITTED, document_token, person_slug, slugify
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import downloads, packets
from blastjob.web import stage_moves as sm
from blastjob.web.app import create_app
from blastjob.web.runtime import Run
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

H = {CSRF_HEADER: LAUNCH_TOKEN}

COMPANY = "Contoso Widgets"
TITLE = "VP IT"
PERSON = person_slug("Alex J. Rivera")
TOKEN = document_token(COMPANY, TITLE)
RESUME = f"{PERSON}_Resume_-_{TOKEN}.docx"
COVER = f"{PERSON}_Cover_Letter_-_{TOKEN}.docx"
ENTRY = "contoso-widgets-2026-09-12"


@pytest.fixture
def cfg() -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    return c


def _root(cfg) -> Path:
    return Path(cfg.paths.output_dir)


def _packet(cfg, *, state: str = "", applied_at: str = "") -> tuple[int, Path]:
    conn = dbc.init_db(cfg)
    jid = dbc.upsert_job(conn, source="s", external_id="e1", company=COMPANY, title=TITLE)
    conn.execute(
        "UPDATE jobs SET review_state='kept', application_state=?, applied_at=? WHERE id=?",
        (state, applied_at, jid),
    )
    conn.commit()
    folder = _root(cfg) / ENTRY
    folder.mkdir()
    for name in (RESUME, COVER):
        (folder / name).write_bytes(b"docx-bytes")
    (folder / f"{slugify(TITLE)}.metadata.json").write_text(
        json.dumps({"company": COMPANY, "role": TITLE, "timestamp": "2026-09-12T10:00:00"})
    )
    dbc.claim_for_build(conn, jid, "run-1")
    dbc.finish_build(conn, "run-1", artifact_dir=str(folder), ok=True)
    files = artifacts.role_files(folder, company=COMPANY, role=TITLE, job_id=jid)
    artifacts.register_paths(conn, files, job_id=jid, output_root=_root(cfg))
    conn.close()
    return jid, folder


def _file(cfg, folder: Path) -> Path:
    dst = _root(cfg) / SUBMITTED / folder.name
    dst.parent.mkdir(exist_ok=True)
    folder.rename(dst)
    return dst


def _rows(cfg, sql: str) -> list:
    conn = dbc.init_db(cfg)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


@pytest.fixture
def client(cfg):
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))


def test_packet_for_job_and_latest_packet_find_a_filed_folder(cfg) -> None:
    jid, folder = _packet(cfg)
    filed = _file(cfg, folder)
    conn = dbc.init_db(cfg)
    try:
        packet = packets.packet_for_job(conn, jid, load_config=lambda: cfg)
        assert packet is not None and packet["exists"]
        assert Path(packet["dir"]) == filed
        assert {f["name"] for f in packet["files"]} >= {RESUME, COVER}
        assert artifacts.latest_packet(conn, jid, output_root=_root(cfg)) == (1, filed)
        assert artifacts.run_for_dir(conn, jid, filed, _root(cfg)) == 1
    finally:
        conn.close()


def test_downloads_serve_and_link_a_filed_file(cfg, client) -> None:
    jid, folder = _packet(cfg)
    _file(cfg, folder)
    assert client.get(f"/jobs/{jid}/files/{RESUME}").content == b"docx-bytes"
    conn = dbc.init_db(cfg)
    try:
        stored = Path(
            conn.execute(
                "SELECT path FROM artifacts WHERE path LIKE ?", (f"%{RESUME}",)
            ).fetchone()["path"]
        )
        assert SUBMITTED not in stored.parts
        assert downloads.is_downloadable(conn, cfg, jid, stored)
    finally:
        conn.close()
    page = client.get(f"/jobs/{jid}/activity").text
    assert f"/jobs/{jid}/files/{RESUME}" in page


def test_mark_applied_records_root_form_paths_and_files_the_entry(cfg, client) -> None:
    jid, folder = _packet(cfg)
    r = client.post(f"/jobs/{jid}/stage/apply", headers=H)
    assert r.status_code == 200
    sends = _rows(cfg, "SELECT artifact_dir, resume_path FROM packet_sends")
    assert len(sends) == 1
    assert Path(sends[0]["artifact_dir"]) == folder
    assert Path(sends[0]["resume_path"]).name == RESUME
    # The post-move hook filed the entry.
    assert not folder.exists()
    assert (_root(cfg) / SUBMITTED / ENTRY / RESUME).is_file()


def test_record_send_after_filing_still_stores_root_form(cfg) -> None:
    jid, folder = _packet(cfg)
    _file(cfg, folder)
    conn = dbc.init_db(cfg)
    try:
        assert sm.record_send_for_job(conn, jid, load_config=lambda: cfg) == ""
        row = conn.execute("SELECT artifact_dir, resume_path FROM packet_sends").fetchone()
    finally:
        conn.close()
    assert Path(row["artifact_dir"]) == folder
    assert Path(row["resume_path"]) == folder / RESUME


def test_status_route_files_an_application(cfg, client) -> None:
    jid, folder = _packet(cfg)
    r = client.post(f"/jobs/{jid}/status", data={"status": "applied"}, headers=H)
    assert r.status_code == 200
    assert (_root(cfg) / SUBMITTED / ENTRY).is_dir()


def test_followup_written_after_its_folder_was_filed_lands_in_the_filed_folder(cfg) -> None:
    jid, folder = _packet(cfg)
    # The route resolved the folder before streaming; filing ran meanwhile.
    filed = _file(cfg, folder)
    path = followup.write_followup(folder, TITLE, "body", job_id=jid, output_root=_root(cfg))
    assert path.parent == filed
    assert not folder.exists()


def test_prep_pack_goes_into_the_filed_packet_folder(cfg) -> None:
    from blastjob.core import prep

    jid, folder = _packet(cfg)
    filed = _file(cfg, folder)
    conn = dbc.init_db(cfg)
    try:
        path = prep.save_prep_pack(conn, cfg, jid, COMPANY, TITLE, "# prep")
        stored = conn.execute("SELECT path FROM artifacts WHERE kind='interview_prep'").fetchone()[
            "path"
        ]
    finally:
        conn.close()
    assert path.parent == filed
    assert Path(stored) == (folder / path.name).resolve()


async def test_refine_relocates_a_filed_packet(cfg, monkeypatch) -> None:
    from blastjob.web import refine as wrefine

    jid, folder = _packet(cfg)
    filed = _file(cfg, folder)
    seen: list[Path] = []

    async def fake_refine(run_dir, feedback, cfg_, cost, on_text=None, **who):
        seen.append(run_dir)
        return run_dir / RESUME, None

    monkeypatch.setattr("blastjob.core.refine.run_refine", fake_refine)
    run = Run(id="refine-1", name="refine")
    result = await wrefine.make_refine_work(str(folder), "fix", lambda: cfg, job_id=jid)(run)
    assert result is not None
    assert seen == [filed]


async def test_tailor_hook_files_a_rebuild_of_a_submitted_job(cfg, monkeypatch) -> None:
    from blastjob.core.paths import application_dir
    from blastjob.web import build as wbuild

    jid, folder = _packet(cfg, state="applied", applied_at="2026-09-12")
    conn = dbc.init_db(cfg)
    conn.execute(
        "UPDATE jobs SET raw_jd=?, build_state='none' WHERE id=?", ("Lead IT. " * 100, jid)
    )
    conn.commit()
    conn.close()

    async def fake_run_build(**kwargs):
        out = application_dir(_root(cfg), COMPANY)
        (out / RESUME).write_bytes(b"new")
        return out, None

    monkeypatch.setattr("blastjob.core.build.run_build", fake_run_build)
    result = await wbuild.make_tailor_work(jid, load_config=lambda: cfg)(Run(id="t1", name="t"))
    assert result["ok"] is True
    built = Path(result["artifact_dir"])
    assert built.parent == _root(cfg)  # recorded in root form
    assert (_root(cfg) / SUBMITTED / built.name / RESUME).is_file()
    assert not built.exists()


def test_history_page_links_a_filed_packet(cfg, client) -> None:
    jid, folder = _packet(cfg)
    _file(cfg, folder)
    entries = __import__("blastjob.core.history", fromlist=["x"]).scan_history(_root(cfg))
    conn = dbc.init_db(cfg)
    try:
        ids = packets.history_job_ids(conn, entries, output_root=_root(cfg))
    finally:
        conn.close()
    assert set(ids.values()) == {jid}
    assert client.get("/history").status_code == 200


def test_hook_scopes_to_the_job_and_reports(cfg) -> None:
    jid, folder = _packet(cfg, state="applied", applied_at="2026-09-12")
    conn = dbc.init_db(cfg)
    try:
        res = pf.file_after_change(conn, _root(cfg), jid)
    finally:
        conn.close()
    assert res is not None and [m.entry for m in res.moves] == [ENTRY]


def test_artifact_backfill_finds_a_filed_packet_and_writes_root_form(cfg) -> None:
    from blastjob.core import backfill

    jid, folder = _packet(cfg)
    _file(cfg, folder)
    conn = dbc.init_db(cfg)
    try:
        conn.execute("DELETE FROM artifacts")
        conn.commit()
        text = backfill.run(conn, apply=True, output_root=_root(cfg))
        stored = [Path(r["path"]) for r in conn.execute("SELECT path FROM artifacts")]
    finally:
        conn.close()
    assert "folder" not in text  # no "missing folder" line
    assert stored and all(SUBMITTED not in p.parts for p in stored)


def test_workup_path_is_stored_in_root_form_and_shown_where_it_is(cfg, client) -> None:
    from blastjob.core import company as co

    jid, folder = _packet(cfg)
    (folder / "company_workup.md").write_text("# workup")
    filed = _file(cfg, folder)
    conn = dbc.init_db(cfg)
    try:
        co.upsert_profile(
            conn,
            co.CompanyProfile(company=COMPANY, workup_path=str(filed / "company_workup.md")),
            output_root=_root(cfg),
        )
        stored = co.get_profile(conn, COMPANY)["workup_path"]
    finally:
        conn.close()
    assert Path(stored) == folder / "company_workup.md"
    page = client.get(f"/companies/{co.company_key(COMPANY)}").text
    assert str(filed / "company_workup.md") in page


async def test_the_build_never_holds_the_filing_lock_while_it_streams(cfg, monkeypatch) -> None:
    from blastjob.core import filing_lock
    from blastjob.core.paths import application_dir
    from blastjob.web import build as wbuild

    jid, folder = _packet(cfg)
    conn = dbc.init_db(cfg)
    conn.execute(
        "UPDATE jobs SET raw_jd=?, build_state='none' WHERE id=?", ("Lead IT. " * 100, jid)
    )
    conn.commit()
    conn.close()
    free: list[bool] = []

    async def fake_run_build(**kwargs):
        # Mid-stream: nothing holds the lock, so a filing pass is not "busy".
        with filing_lock.exclusive(_root(cfg)) as ok:
            free.append(ok)
        return application_dir(_root(cfg), COMPANY), None

    monkeypatch.setattr("blastjob.core.build.run_build", fake_run_build)
    await wbuild.make_tailor_work(jid, load_config=lambda: cfg)(Run(id="t2", name="t"))
    assert free == [True]


async def test_research_stores_a_workup_path_in_root_form(cfg, monkeypatch) -> None:
    from blastjob.core import company as co
    from blastjob.web import research

    jid, folder = _packet(cfg)
    filed = _file(cfg, folder)

    async def fake_research(cfg_, company, emit=None, **_kw):
        return co.CompanyProfile(company=COMPANY, workup_path=str(filed / "company_workup.md"))

    monkeypatch.setattr(research, "research_company", fake_research)
    await research.make_research_work(COMPANY, lambda: cfg)(Run(id="r1", name="research"))
    conn = dbc.init_db(cfg)
    try:
        stored = co.get_profile(conn, COMPANY)["workup_path"]
    finally:
        conn.close()
    assert Path(stored) == folder / "company_workup.md"

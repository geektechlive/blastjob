"""Group C (cockpit) review fixes, items C1-C9."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import packets
from blastjob.web import stage_moves as sm
from blastjob.web.app import create_app
from blastjob.web.build import sse_format
from blastjob.web.runtime import Event, Runtime
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

TOK = {CSRF_HEADER: LAUNCH_TOKEN}


@pytest.fixture
def env(tmp_path):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    conn = dbc.init_db(cfg)
    app = create_app(load_config=lambda: cfg, save_config=lambda c: None)
    yield TestClient(app), cfg, conn
    conn.close()


def _job(conn: sqlite3.Connection, ext: str, review: str, app_state: str = "", applied_at=""):
    jid = dbc.upsert_job(conn, source="t", external_id=ext, company="Acme", title=f"VP {ext}")
    conn.execute(
        "UPDATE jobs SET review_state=?, application_state=?, applied_at=? WHERE id=?",
        (review, app_state, applied_at, jid),
    )
    conn.commit()
    return jid


# ---- C1 ------------------------------------------------------------------


def test_status_dropdown_records_send_only_when_first_stamping_applied_at(env, monkeypatch):
    client, _, conn = env
    sends: list[int] = []
    monkeypatch.setattr(sm, "record_send_for_job", lambda c, j: sends.append(j) or "")
    first = _job(conn, "a", "kept")
    again = _job(conn, "b", "kept", "screen", "2026-10-01")

    client.post(f"/jobs/{first}/status", data={"status": "applied"}, headers=TOK)
    client.post(f"/jobs/{again}/status", data={"status": "applied"}, headers=TOK)

    assert sends == [first]


# ---- C2 / C4 -------------------------------------------------------------


def test_archive_refuses_a_kept_job(env):
    client, _, conn = env
    jid = _job(conn, "k", "kept")
    client.post(f"/jobs/{jid}/archive", data={"from_list": "1"}, headers=TOK)
    state = conn.execute("SELECT review_state FROM jobs WHERE id=?", (jid,)).fetchone()[0]
    assert state == "kept"


def test_archive_still_works_for_a_new_job(env):
    client, _, conn = env
    jid = _job(conn, "n", "new")
    client.post(f"/jobs/{jid}/archive", data={}, headers=TOK)
    state = conn.execute("SELECT review_state FROM jobs WHERE id=?", (jid,)).fetchone()[0]
    assert state == "archived"


def test_detail_page_offers_keep_and_archive_for_a_new_job(env):
    client, _, conn = env
    jid = _job(conn, "n2", "new")
    html = client.get(f"/jobs/{jid}").text
    assert f"/jobs/{jid}/keep" in html and f"/jobs/{jid}/archive" in html


# ---- C3 ------------------------------------------------------------------


def test_list_row_advance_refreshes_because_the_row_stays_on_applied(env):
    client, _, conn = env
    jid = _job(conn, "adv", "kept", "applied", "2026-10-01")
    r = client.post(f"/jobs/{jid}/stage/advance", headers=TOK)
    assert r.status_code == 200
    assert r.headers.get("HX-Refresh") == "true"
    state = conn.execute("SELECT application_state FROM jobs WHERE id=?", (jid,)).fetchone()[0]
    assert state == "screen"


def test_list_row_withdraw_still_removes_the_row(env):
    client, _, conn = env
    jid = _job(conn, "wd", "kept", "applied", "2026-10-01")
    r = client.post(f"/jobs/{jid}/stage/withdraw", headers=TOK)
    assert r.text == "" and "HX-Refresh" not in r.headers


# ---- C5 ------------------------------------------------------------------


def test_discovery_and_matching_share_one_flight():
    async def scenario() -> tuple[str, str]:
        rt = Runtime()
        gate = asyncio.Event()

        async def work(run):
            await gate.wait()

        first = rt.start("discovery", work)
        second = rt.start("matching", work)
        gate.set()
        await asyncio.sleep(0)
        return first.id, second.id

    first, second = asyncio.run(scenario())
    assert first == second


def test_score_job_is_not_blocked_by_a_full_pass():
    async def scenario() -> bool:
        rt = Runtime()
        gate = asyncio.Event()

        async def work(run):
            await gate.wait()

        a = rt.start("matching", work)
        b = rt.start("score-job", work)
        gate.set()
        await asyncio.sleep(0)
        return a.id != b.id

    assert asyncio.run(scenario())


# ---- C6 ------------------------------------------------------------------


def test_paste_response_triggers_a_run_status_refresh(env, monkeypatch):
    client, _, conn = env
    from blastjob.web import paste_rescore

    started: list[int] = []
    monkeypatch.setattr(paste_rescore, "start_rescore", lambda lc, j: started.append(j))
    jid = _job(conn, "p", "surfaced")
    r = client.post(f"/jobs/{jid}/jd", data={"jd": "A full description. " * 30}, headers=TOK)
    assert started == [jid]
    assert r.headers.get("HX-Trigger") == "refresh-run-status"


def test_base_page_banner_listens_for_the_refresh_event(env):
    client, _, _ = env
    assert "refresh-run-status from:body" in client.get("/").text


# ---- C7 ------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["$180,000", "180000", "180k", " 180,000 "])
def test_comp_signal_accepts_formatted_money(env, raw):
    client, _, conn = env
    jid = _job(conn, "m", "kept")
    r = client.post(
        f"/jobs/{jid}/comp-signal", data={"kind": "posted_range", "low_usd": raw}, headers=TOK
    )
    assert r.status_code == 200
    low = conn.execute("SELECT low_usd FROM comp_signals").fetchone()[0]
    assert low == 180000


def test_comp_signal_bad_money_rerenders_with_a_message_not_a_500(env):
    client, _, conn = env
    jid = _job(conn, "m2", "kept")
    r = client.post(
        f"/jobs/{jid}/comp-signal", data={"kind": "posted_range", "low_usd": "lots"}, headers=TOK
    )
    assert r.status_code == 200
    assert "Could not read" in r.text
    assert conn.execute("SELECT COUNT(*) FROM comp_signals").fetchone()[0] == 0


# ---- C8 ------------------------------------------------------------------


def test_security_headers_on_every_response(env):
    client, _, _ = env
    bad_host = client.get("/", headers={"host": "evil.example.com"})
    for r in (client.get("/"), client.get("/runs/active"), bad_host):
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["Referrer-Policy"] == "same-origin"


# ---- C9 ------------------------------------------------------------------


def test_sse_data_carriage_returns_do_not_split_a_frame():
    frame = sse_format(Event(1, "log", "one\rtwo\r\nthree"))
    assert "\r" not in frame
    assert frame.count("data: ") == 3


def test_runtime_error_text_is_redacted():
    async def scenario() -> str:
        rt = Runtime()

        async def work(run):
            raise RuntimeError("GET https://x.example/?api_key=SECRET123 failed")

        run = rt.start("tailor", work)
        for _ in range(5):
            await asyncio.sleep(0)
        return next(e.data for e in run.events if e.kind == "error")

    text = asyncio.run(scenario())
    assert "SECRET123" not in text


def test_reveal_refuses_a_path_that_is_a_file(env, monkeypatch, tmp_path):
    client, cfg, conn = env
    from blastjob.web import routes_packets

    opened: list[list[str]] = []
    monkeypatch.setattr(routes_packets.subprocess, "run", lambda a, **k: opened.append(a))
    out = tmp_path / "out"
    out.mkdir()
    cfg.paths.output_dir = str(out)
    afile = out / "notes.txt"
    afile.write_text("x")
    jid = _job(conn, "rv", "kept")
    monkeypatch.setattr(packets, "packet_for_job", lambda *a, **k: {"dir": str(afile)})
    monkeypatch.setattr(packets, "is_local_request", lambda r: True)
    r = client.post(f"/jobs/{jid}/reveal", headers=TOK)
    assert r.status_code == 404 and opened == []


def test_packet_file_path_refuses_a_symlink_that_leaves_the_folder(tmp_path):
    cfg = BlastJobConfig()
    cfg.paths.output_dir = str(tmp_path / "out")
    folder = tmp_path / "out" / "acme-2026-10-16"
    folder.mkdir(parents=True)
    secret = tmp_path / "secret.md"
    secret.write_text("private")
    (folder / "leak.md").symlink_to(secret)
    (folder / "ok.md").write_text("fine")
    assert packets.packet_file_path(cfg, str(folder), "leak.md") is None
    assert packets.packet_file_path(cfg, str(folder), "ok.md") is not None

"""Tests for `web/routes_settings.py`'s `/work-history/ingest` route.

Built against a standalone FastAPI app (not `web.app.create_app`) so this test
file stays inside `routes_settings.py`'s ownership without editing
`web/app.py` -- the integration wiring that would let `create_app` serve this
route is delivered as a separate patch (see the ingest work-unit report).
`render` is reused unmodified from `web.app` (an existing, already-tested
function); doing so does not modify that module.

The background-run body (`_make_ingest_work`) is driven directly, the same
way `tests/test_web_build.py` drives `make_tailor_work` -- avoiding
TestClient/event-loop timing on background tasks. HTTP-level tests only cover
behavior that completes before a run is scheduled (validation, CSRF, and the
synchronous backup-then-schedule ordering).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blastjob import config as cfg_mod
from blastjob.models.config import BlastJobConfig
from blastjob.web import routes_settings
from blastjob.web.app import render
from blastjob.web.deps import Deps
from blastjob.web.runtime import RUNTIME, Run
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN
from blastjob.web.upload_guard import MAX_NETWORK_UPLOAD_BYTES, MaxBodySizeMiddleware


def _tok() -> dict[str, str]:
    return {CSRF_HEADER: LAUNCH_TOKEN}


@pytest.fixture
def client_cfg():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    saved: dict = {}

    def load() -> BlastJobConfig:
        return cfg

    def save(c: BlastJobConfig) -> None:
        saved["cfg"] = c

    def no_conn():
        raise AssertionError("routes_settings never needs a DB connection")

    app = FastAPI()
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=MAX_NETWORK_UPLOAD_BYTES)
    app.state.nav_counts = None
    deps = Deps(
        conn=no_conn, load_config=load, render=render, nav_counts=lambda: {}, radius=lambda: 0.0
    )
    routes_settings.register(app, deps, save)

    return TestClient(app), cfg, saved


def _data_dir(cfg: BlastJobConfig) -> Path:
    return cfg_mod.data_dir(cfg)


@pytest.fixture(autouse=True)
def _clean_runtime_ingest_runs():
    """`RUNTIME` is a process-wide singleton and `ingest` is now single-flight
    (it overwrites work_history.md). Without this, a run left behind by one
    test would make the SINGLE_FLIGHT refusal test in another test flaky."""
    yield
    for rid in [rid for rid, r in list(RUNTIME._runs.items()) if r.name == "ingest"]:
        del RUNTIME._runs[rid]


def test_ingest_get_renders(client_cfg):
    client, *_ = client_cfg
    r = client.get("/work-history/ingest")
    assert r.status_code == 200
    assert "Ingest" in r.text


def test_ingest_post_requires_csrf(client_cfg):
    client, *_ = client_cfg
    r = client.post(
        "/work-history/ingest",
        files={"files": ("resume.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 403


def test_ingest_rejects_unsupported_extension(client_cfg):
    client, *_ = client_cfg
    r = client.post(
        "/work-history/ingest",
        files={"files": ("resume.xlsx", b"junk", "application/octet-stream")},
        headers=_tok(),
    )
    assert r.status_code == 200
    assert "Unsupported" in r.text
    assert RUNTIME.active(("ingest",), recent_seconds=0) is None


def test_ingest_backs_up_existing_work_history_before_run(client_cfg, monkeypatch):
    client, cfg, _ = client_cfg
    data_dir = _data_dir(cfg)
    data_dir.mkdir(parents=True, exist_ok=True)
    wh_path = data_dir / "work_history.md"
    wh_path.write_text("---\nname: Existing\n---\n", encoding="utf-8")

    async def fake_run_ingestion(path, app_config, cost_tracker, on_text=None):
        return data_dir / "work_history.md"

    monkeypatch.setattr("blastjob.core.ingest.run_ingestion", fake_run_ingestion)

    r = client.post(
        "/work-history/ingest",
        files={"files": ("resume.txt", b"hello world", "text/plain")},
        headers=_tok(),
    )
    assert r.status_code == 200

    backups_dir = data_dir / "backups"
    backups = list(backups_dir.glob("work_history-*-ingest.md"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "---\nname: Existing\n---\n"
    # The original must be untouched by the synchronous backup step itself
    # (only the background run, stubbed here, would later overwrite it).
    assert wh_path.exists()


def test_ingest_no_backup_when_no_existing_work_history(client_cfg, monkeypatch):
    client, cfg, _ = client_cfg
    data_dir = _data_dir(cfg)

    async def fake_run_ingestion(path, app_config, cost_tracker, on_text=None):
        return data_dir / "work_history.md"

    monkeypatch.setattr("blastjob.core.ingest.run_ingestion", fake_run_ingestion)

    r = client.post(
        "/work-history/ingest",
        files={"files": ("resume.txt", b"hello world", "text/plain")},
        headers=_tok(),
    )
    assert r.status_code == 200
    backups_dir = data_dir / "backups"
    assert not backups_dir.exists() or not list(backups_dir.glob("work_history-*.md"))


def test_ingest_refuses_second_concurrent_run(client_cfg):
    client, *_ = client_cfg
    already = Run(id="ingest-already", name="ingest", status="running")
    RUNTIME._runs[already.id] = already
    try:
        r = client.post(
            "/work-history/ingest",
            files={"files": ("resume.txt", b"hello", "text/plain")},
            headers=_tok(),
        )
        assert r.status_code == 200
        assert "already running" in r.text.lower()
    finally:
        del RUNTIME._runs[already.id]


async def test_make_ingest_work_runs_ingestion_and_cleans_spool_dir(monkeypatch):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    spool_dir = Path(tempfile.mkdtemp(prefix="blastjob-ingest-test-"))
    (spool_dir / "resume.txt").write_text("hi", encoding="utf-8")

    calls: list[Path] = []

    async def fake_run_ingestion(path, app_config, cost_tracker, on_text=None):
        calls.append(path)
        if on_text:
            on_text("chunk\n")
        out = _data_dir(cfg) / "work_history.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("---\nname: New\n---\n", encoding="utf-8")
        return out

    monkeypatch.setattr("blastjob.core.ingest.run_ingestion", fake_run_ingestion)

    run = Run(id="ingest-1", name="ingest")
    result = await routes_settings._make_ingest_work(spool_dir, lambda: cfg)(run)

    assert calls == [spool_dir]
    assert not spool_dir.exists(), "spool dir must be cleaned up after the run"
    assert result is not None and "work_history.md" in result["path"]
    assert any(e.kind == "output" and e.data == "chunk\n" for e in run.events)
    assert any(e.kind == "log" and "saved" in e.data.lower() for e in run.events)


async def test_make_ingest_work_cleans_spool_dir_even_on_error(monkeypatch):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    spool_dir = Path(tempfile.mkdtemp(prefix="blastjob-ingest-test-"))

    async def boom(path, app_config, cost_tracker, on_text=None):
        raise ValueError("no supported files")

    monkeypatch.setattr("blastjob.core.ingest.run_ingestion", boom)

    run = Run(id="ingest-2", name="ingest")
    with pytest.raises(ValueError):
        await routes_settings._make_ingest_work(spool_dir, lambda: cfg)(run)

    assert not spool_dir.exists()


# ----- temp-dir leaks (#54) -----


@pytest.fixture
def spooled(monkeypatch):
    """Record every blastjob-ingest-* dir the route creates."""
    made: list[Path] = []
    real = tempfile.mkdtemp

    def recording(*args, **kwargs):
        path = real(*args, **kwargs)
        if kwargs.get("prefix") == "blastjob-ingest-":
            made.append(Path(path))
        return path

    monkeypatch.setattr(routes_settings.tempfile, "mkdtemp", recording)
    return made


def test_ingest_removes_spool_dir_when_a_concurrent_run_won_the_race(
    client_cfg, spooled, monkeypatch
):
    client, *_ = client_cfg
    winner = Run(id="ingest-winner", name="ingest")
    RUNTIME._runs[winner.id] = winner
    # The pre-check passed before the winner started; only start() sees it.
    monkeypatch.setattr(routes_settings.RUNTIME, "active", lambda *a, **k: None)

    r = client.post(
        "/work-history/ingest",
        files={"files": ("resume.txt", b"hello", "text/plain")},
        headers=_tok(),
    )

    assert r.status_code == 200
    assert len(spooled) == 1
    assert not spooled[0].exists()


def test_ingest_removes_spool_dir_when_scheduling_fails(client_cfg, spooled, monkeypatch):
    client, cfg, _ = client_cfg
    data_dir = _data_dir(cfg)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "work_history.md").write_text("x", encoding="utf-8")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(routes_settings.wh, "backup_work_history", boom)

    with pytest.raises(OSError):
        client.post(
            "/work-history/ingest",
            files={"files": ("resume.txt", b"hello", "text/plain")},
            headers=_tok(),
        )

    assert len(spooled) == 1
    assert not spooled[0].exists()

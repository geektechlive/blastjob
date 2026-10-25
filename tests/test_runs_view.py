"""The Running now / Pause view and the cockpit's run lifecycle (#96, ADR 0060):
the model-slot semaphore and "queued", Stop that cancels honestly and reaps the child,
the PAUSE switch, and CSRF/Origin/Host on each new POST. No real model is contacted."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from blastjob import config as cfg_mod
from blastjob.db import client as dbc
from blastjob.llm import gateway, ledger, providers
from blastjob.models.config import LimitsConfig, PricingConfig
from blastjob.web.app import create_app
from blastjob.web.runtime import RUNTIME, Run, Runtime
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

TOKEN = {CSRF_HEADER: LAUNCH_TOKEN}

_HANGING_CLAUDE = """
import os, subprocess, sys, time
grandchild = subprocess.Popen(["sleep", "60"])
with open(sys.argv[1], "w") as fh:
    fh.write(f"{os.getpid()} {grandchild.pid}")
sys.stdin.read()
time.sleep(60)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _until(predicate, seconds: float = 10.0) -> None:
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        await asyncio.sleep(0.02)
    assert predicate()


def _install(tmp_path, **limits):
    path = tmp_path / "ledger.db"
    dbc.init_db_at(path).close()
    ledger.install(path, LimitsConfig(**limits), PricingConfig())


# --- the model-slot semaphore ---------------------------------------------------------------


async def test_the_semaphore_limits_parallel_runs_and_marks_the_rest_queued(tmp_path):
    _install(tmp_path, cockpit_concurrency=2)
    rt = Runtime()
    release = asyncio.Event()
    entered: list[str] = []

    def work(name: str):
        async def _work(run: Run):
            entered.append(name)
            await release.wait()

        return _work

    runs = [rt.start(f"job{i}", work(f"job{i}")) for i in range(4)]
    await _until(lambda: len(entered) == 2)
    await asyncio.sleep(0.05)
    assert len(entered) == 2  # the other two wait for a slot
    assert [r.queued for r in runs] == [False, False, True, True]
    assert all(r.status == "running" for r in runs)  # queued is not a status

    release.set()
    await _until(lambda: all(r.status == "done" for r in runs))
    assert len(entered) == 4 and not any(r.queued for r in runs)


async def test_a_run_cancelled_while_queued_never_releases_a_slot_it_did_not_hold(tmp_path):
    _install(tmp_path, cockpit_concurrency=1)
    rt = Runtime()
    release = asyncio.Event()

    async def hold(_run: Run):
        await release.wait()

    first = rt.start("a", hold)
    waiting = rt.start("b", hold)
    await _until(lambda: waiting.queued)
    assert rt.stop(waiting.id)
    await _until(lambda: waiting.status == "cancelled")
    third = rt.start("c", hold)
    await asyncio.sleep(0.05)
    assert third.queued  # the slot is still held by the first run: no extra permit appeared
    release.set()
    await _until(lambda: first.status == "done" and third.status == "done")


async def test_the_concurrency_gate_follows_a_changed_setting(tmp_path):
    rt = Runtime()
    _install(tmp_path, cockpit_concurrency=1)
    first = rt._semaphore()
    _install(tmp_path, cockpit_concurrency=3)
    assert rt._semaphore() is not first and rt._semaphore()._value == 3


# --- Stop -------------------------------------------------------------------------------------


async def test_stop_cancels_the_run_marks_it_cancelled_and_reaps_the_child(tmp_path):
    script = tmp_path / "hang.py"
    script.write_text(_HANGING_CLAUDE)
    pidfile = tmp_path / "pids.txt"
    rt = Runtime()

    async def work(_run: Run):
        box = providers._CLIUsageBox()
        async for _ in providers._run_cli([sys.executable, str(script), str(pidfile)], "x", box):
            pass

    run = rt.start("tailor", work)
    await _until(lambda: pidfile.exists() and " " in pidfile.read_text())
    child, grandchild = (int(p) for p in pidfile.read_text().split())
    assert _alive(child) and _alive(grandchild)

    assert rt.stop(run.id) is True
    assert rt.stop(run.id) is True  # a second Stop is harmless: it does not cancel again
    assert run.task is not None
    await asyncio.wait([run.task], timeout=15)

    assert run.status == "cancelled" and run.finished_at is not None
    assert run.events[-1].kind == "error" and run.events[-1].data == "Stopped."
    await _until(lambda: not _alive(child) and not _alive(grandchild))


async def test_a_run_cancelled_before_it_began_is_still_marked_cancelled():
    rt = Runtime()

    async def work(_run: Run):
        raise AssertionError("never started")

    run = rt.start("x", work)
    assert rt.stop(run.id)
    await _until(lambda: run.status == "cancelled")


async def test_stop_on_a_finished_or_unknown_run_does_nothing():
    rt = Runtime()

    async def work(_run: Run):
        return 1

    run = rt.start("x", work)
    await _until(lambda: run.status == "done")
    assert rt.stop(run.id) is False and rt.stop("nope") is False


async def test_a_clicked_run_calls_the_model_as_clicked_with_its_name_as_the_caller(tmp_path):
    _install(tmp_path)
    rt = Runtime()
    seen: list = []

    async def work(_run: Run):
        seen.append(gateway.reserve("site"))

    with gateway.origin_scope("scheduled"):  # whatever the creator's context says
        run = rt.start("prep", work)
    await _until(lambda: run.status == "done")
    assert (seen[0].origin, seen[0].caller) == ("clicked", "prep")


# --- the page and its POSTs ---------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(tmp_path / "data"))
    return TestClient(create_app())


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(tmp_path / "data"))
    return cfg_mod.resolve_data_dir()


def test_the_page_lists_runs_with_stop_and_marks_queued_ones(client):
    RUNTIME._runs["r1"] = Run(id="r1", name="tailor", key="42", task=SimpleNamespace(cancel=None))
    RUNTIME._runs["r2"] = Run(id="r2", name="prep", queued=True, task=SimpleNamespace())
    body = client.get("/runs").text
    assert "Running now" in body and "tailor" in body and "(42)" in body
    assert 'hx-post="/runs/r1/stop"' in body and 'hx-post="/runs/r2/stop"' in body
    assert "Queued" in body and "waiting for a free model slot" in body
    assert "Pause scheduled runs" in body


def test_the_page_shows_calls_by_caller_with_unknown_cost_as_unknown(client, data_dir):
    cfg_mod.load_config()  # installs the ledger for this data dir
    gateway.finish(gateway.reserve("scoring"), "ok")  # usage never reported
    body = client.get("/runs").text
    assert "Model calls today" in body and "unknown" in body
    assert "$0.0000" not in body


def test_abandoned_calls_are_listed_apart_from_live_ones(client, data_dir):
    cfg_mod.load_config()
    conn = dbc.init_db_at(data_dir / cfg_mod.DB_NAME)
    conn.execute(
        "INSERT INTO model_calls (started_at, caller, origin, outcome) VALUES (?, 'old', "
        "'clicked', 'abandoned')",
        (ledger.now_iso(),),
    )
    conn.commit()
    conn.close()
    body = client.get("/runs").text
    assert "Abandoned calls" in body and "cost is unknown" in body


def test_stop_cancels_the_task_once_even_when_clicked_twice(client):
    cancels: list[int] = []
    task = SimpleNamespace(cancel=lambda: cancels.append(1))
    RUNTIME._runs["r1"] = Run(id="r1", name="tailor", task=task)
    assert client.post("/runs/r1/stop", headers=TOKEN).status_code == 200
    assert client.post("/runs/r1/stop", headers=TOKEN).status_code == 200
    assert cancels == [1]
    assert "stopping" in client.get("/runs").text


def test_pause_creates_and_resume_deletes_the_pause_file_and_the_nav_says_so(client, data_dir):
    assert "Scheduled runs paused" not in client.get("/runs").text
    r = client.post("/runs/pause", headers=TOKEN)
    assert r.status_code == 200 and (data_dir / "PAUSE").exists()
    assert "Resume scheduled runs" in r.text and "Paused since" in r.text
    for path in ("/runs", "/matches"):
        assert "Scheduled runs paused" in client.get(path).text
    r = client.post("/runs/resume", headers=TOKEN)
    assert r.status_code == 200 and not (data_dir / "PAUSE").exists()
    assert "Scheduled runs paused" not in client.get("/runs").text


def test_resume_when_not_paused_is_harmless(client, data_dir):
    assert client.post("/runs/resume", headers=TOKEN).status_code == 200


@pytest.mark.parametrize("path", ["/runs/pause", "/runs/resume", "/runs/some-run/stop"])
def test_every_new_post_refuses_a_missing_token_a_foreign_origin_and_a_foreign_host(
    client, data_dir, path
):
    assert client.post(path).status_code == 403
    assert (
        client.post(path, headers={**TOKEN, "origin": "http://evil.example.com"}).status_code == 403
    )
    assert client.post(path, headers={**TOKEN, "host": "evil.example.com"}).status_code == 421
    assert not (data_dir / "PAUSE").exists()


def test_the_runs_link_is_in_the_more_menu(client):
    assert 'href="/runs">Running now' in client.get("/matches").text


def test_the_runs_fixed_paths_win_over_the_run_id_route(client):
    assert client.get("/runs/panel").text.lstrip().startswith('<div id="runs-panel"')


def test_cockpit_startup_marks_calls_of_a_dead_owner_abandoned(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(tmp_path / "data"))
    conn = dbc.init_db_at(cfg_mod.resolve_data_dir() / cfg_mod.DB_NAME)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    conn.execute(
        "INSERT INTO model_calls (started_at, caller, origin, outcome, owner_pid, owner_started) "
        "VALUES (?, 'x', 'clicked', 'started', ?, 'old')",
        (ledger.now_iso(), dead.pid),
    )
    conn.commit()
    with TestClient(create_app()):  # entering the context runs the startup hooks
        pass
    outcome = conn.execute("SELECT outcome FROM model_calls").fetchone()[0]
    conn.close()
    assert outcome == "abandoned"

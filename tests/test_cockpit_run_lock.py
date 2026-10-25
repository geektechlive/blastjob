# ruff: noqa: F811
"""The cockpit's discovery, matching and Re-score starters fail fast while another run holds
the lock (#95): no queueing, a clear message, the lock held inside the task for its life."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from blastjob.core import run_lock
from blastjob.matching.pipeline import MatchResult
from blastjob.web import app as web_app
from blastjob.web.app import create_app
from blastjob.web.runtime import Run, Runtime
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN
from tests.test_rescore_modes_67 import started  # noqa: F401
from tests.test_run_lock import _spawn_holder
from tests.test_score_fingerprint import cfg, conn  # noqa: F401

HEADERS = {CSRF_HEADER: LAUNCH_TOKEN}


@pytest.fixture
def client(cfg):
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))


@pytest.fixture
def other_run_holds_the_lock(cfg):
    proc = _spawn_holder(cfg_data_dir(cfg))
    yield
    proc.kill()
    proc.join()


def cfg_data_dir(cfg):
    from blastjob import config as cfg_mod

    return cfg_mod.data_dir(cfg)


@pytest.mark.parametrize(
    "path,name",
    [
        ("/run/discovery", "discovery"),
        ("/run/matching", "matching"),
        ("/run/matching/all", "matching"),
    ],
)
def test_starters_fail_fast_while_another_run_holds_the_lock(
    client, started, other_run_holds_the_lock, path, name
):
    assert client.post(path, headers=HEADERS).status_code == 200
    assert [n for n, _ in started] == [name]

    with pytest.raises(run_lock.RunLockHeld, match=r"Another run is in progress \(daily run"):
        asyncio.run(started[0][1](Run(id="x", name=name)))


def test_discovery_task_holds_the_lock_for_its_whole_life(client, cfg, started, monkeypatch):
    held: list[object] = []

    async def fake_discovery(_cfg, _conn, adapters=None):
        held.append(run_lock.holder_of(cfg_data_dir(cfg)))
        raise RuntimeError("stop here")

    monkeypatch.setattr("blastjob.discovery.pipeline.run_discovery", fake_discovery)
    monkeypatch.setattr(
        "blastjob.matching.pipeline.run_matching", lambda *a, **k: _async(MatchResult())
    )
    client.post("/run/discovery", headers=HEADERS)

    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(started[0][1](Run(id="x", name="discovery")))

    assert held and held[0] is not None
    assert held[0].what.startswith("discovery")
    assert run_lock.holder_of(cfg_data_dir(cfg)) is None


async def _async(value):
    return value


def test_a_refused_run_is_shown_as_failed_with_just_the_message():
    async def go() -> Run:
        rt = Runtime()

        async def work(_run: Run):
            raise run_lock.RunLockHeld(run_lock.Holder(pid=1, what="daily run"))

        run = rt.start("matching", work)
        await asyncio.gather(*rt._tasks)
        return run

    run = asyncio.run(go())

    assert run.status == "failed"
    assert run.events[-1].kind == "error"
    assert run.events[-1].data == "Another run is in progress (daily run)"


def test_module_imports_web_app():
    assert web_app.create_app is create_app


def test_clicked_full_passes_run_under_the_daily_time_limit(client, cfg, started, monkeypatch):
    """A clicked full pass must not hold the run lock indefinitely: same limit as the daily run."""
    from blastjob.llm import gateway

    cfg.limits.daily_deadline_minutes = 7
    left: list[float | None] = []

    async def fake_run_matching(_cfg, _conn, _cost, **kw):
        left.append(gateway.remaining())
        return MatchResult()

    async def fake_run_discovery(_cfg, _conn, adapters=None):
        from blastjob.discovery.pipeline import DiscoveryResult

        left.append(gateway.remaining())
        return DiscoveryResult()

    monkeypatch.setattr("blastjob.matching.pipeline.run_matching", fake_run_matching)
    monkeypatch.setattr("blastjob.discovery.pipeline.run_discovery", fake_run_discovery)
    for path in ("/run/discovery", "/run/matching", "/run/matching/all"):
        client.post(path, headers=HEADERS)
    for _name, work in started:
        asyncio.run(work(Run(id="x", name="x")))

    assert len(left) == 4  # discovery, its scoring, then the two re-score passes
    assert all(v is not None and 7 * 60 - 30 < v <= 7 * 60 for v in left)


def test_a_clicked_pass_that_hits_the_limit_says_so(client, cfg, started, monkeypatch):
    from blastjob.llm import gateway

    cfg.limits.daily_deadline_minutes = 7

    async def cut_off(_cfg, _conn, _cost, **kw):
        with pytest.raises(gateway.DeadlineReached):
            gateway._deadline.set(0.0)
            gateway.call_limit(900.0)
        return MatchResult()

    monkeypatch.setattr("blastjob.matching.pipeline.run_matching", cut_off)
    client.post("/run/matching", headers=HEADERS)
    run = Run(id="x", name="matching")
    asyncio.run(started[0][1](run))

    assert any("Stopped at the 7-minute limit" in e.data for e in run.events)

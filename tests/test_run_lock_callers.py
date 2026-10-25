"""Every discovery or matching pass takes the run lock (#95).

The guarantee is structural: `run_discovery` and `run_matching` take the lock themselves, so
no caller can forget. These tests check that the locking lines are still there, that the only
callers able to skip it are the id-scoped cockpit clicks listed here, and that a held lock
really refuses both entry points.
"""

from __future__ import annotations

import ast
import asyncio
import contextvars
from pathlib import Path

import pytest

from blastjob.core import run_lock
from blastjob.db import client as dbc
from blastjob.discovery.pipeline import run_discovery
from blastjob.llm.cost import CostTracker
from blastjob.matching import pipeline as mpipe
from blastjob.models.config import BlastJobConfig

SRC = Path(__file__).resolve().parents[1] / "src" / "blastjob"
GUARDED = {"run_discovery", "run_matching"}
SCOPED = {"run_matching", "rescore_jobs"}

# Callers that may pass exclusive=False: a cockpit click scoped to named jobs, which must not be
# blocked by a running daily pass (paste and single-job rescore, manual job, employer-description
# re-score and split re-score).
UNLOCKED_OK = {
    "matching/rescore.py",
    "web/manual_job.py",
    "web/employer_jd.py",
}


def _calls(tree: ast.AST, names: set[str]):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
        if name in names:
            yield name, node


def _sources() -> list[tuple[str, ast.AST]]:
    return [(str(p.relative_to(SRC)), ast.parse(p.read_text())) for p in sorted(SRC.rglob("*.py"))]


def test_the_two_entry_points_take_the_lock_themselves():
    found = set()
    for rel, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name in GUARDED:
                assert list(_calls(node, {"hold"})), f"{rel}:{node.name} no longer takes the lock"
                found.add(node.name)
    assert found == GUARDED


def test_only_listed_callers_can_skip_the_lock():
    offenders = []
    for rel, tree in _sources():
        for name, call in _calls(tree, SCOPED):
            skips = any(
                k.arg == "exclusive"
                and not (isinstance(k.value, ast.Constant) and k.value.value is True)
                for k in call.keywords
            )
            if skips and rel not in UNLOCKED_OK:
                offenders.append(f"{rel}: {name}(exclusive=...)")
    assert not offenders, offenders


def test_the_daily_run_and_hand_commands_never_skip_the_lock():
    for rel in ("daily.py", "daily_tools.py", "daily_employer_jd.py", "web/app.py"):
        tree = ast.parse((SRC / rel).read_text())
        for _, call in _calls(tree, SCOPED | GUARDED):
            assert not any(k.arg == "exclusive" for k in call.keywords), rel


@pytest.fixture
def cfg_conn(tmp_path):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    conn = dbc.init_db(cfg)
    yield cfg, conn
    conn.close()


def _in_other_run(coro):
    """Run `coro` as a different run than the lock holder (a fresh, empty context)."""

    async def go():
        return await asyncio.get_running_loop().create_task(coro, context=contextvars.Context())

    return asyncio.run(go())


def test_run_discovery_is_refused_while_another_run_holds_the_lock(cfg_conn):
    cfg, conn = cfg_conn
    with run_lock.hold(cfg.paths.data_dir, "daily run"):
        with pytest.raises(run_lock.RunLockHeld, match="daily run"):
            _in_other_run(run_discovery(cfg, conn, adapters=[]))


def test_run_matching_is_refused_while_held_but_an_unlocked_scoped_pass_is_not(cfg_conn):
    cfg, conn = cfg_conn
    with run_lock.hold(cfg.paths.data_dir, "daily run"):
        with pytest.raises(run_lock.RunLockHeld):
            _in_other_run(mpipe.run_matching(cfg, conn, CostTracker(), job_ids=[]))
        _in_other_run(mpipe.run_matching(cfg, conn, CostTracker(), job_ids=[], exclusive=False))


def test_the_lock_is_released_when_a_pass_finishes(cfg_conn):
    cfg, conn = cfg_conn
    asyncio.run(run_discovery(cfg, conn, adapters=[]))
    assert run_lock.holder_of(cfg.paths.data_dir) is None


def test_an_unlocked_matching_pass_must_name_its_jobs(cfg_conn):
    cfg, conn = cfg_conn
    with pytest.raises(ValueError, match="job_ids"):
        asyncio.run(mpipe.run_matching(cfg, conn, CostTracker(), exclusive=False))


def test_hand_run_commands_are_refused_before_their_first_write(cfg_conn, monkeypatch):
    from blastjob import config as cfg_mod
    from blastjob import daily_tools
    from blastjob.core import employer_jd
    from blastjob.discovery import workday_locations as wl
    from tests.test_run_lock import _spawn_holder

    cfg, conn = cfg_conn
    wrote: list[str] = []

    async def plan(_conn, _cfg):
        return []

    async def upgrade(*a, **k):
        wrote.append("employer_jd")

    monkeypatch.setattr(wl, "plan", plan)
    monkeypatch.setattr(wl, "apply", lambda *a, **k: wrote.append("workday") or [])
    monkeypatch.setattr(employer_jd, "run_upgrade", upgrade)
    holder = _spawn_holder(cfg_mod.data_dir(cfg))
    try:
        with pytest.raises(run_lock.RunLockHeld):
            daily_tools.refetch_workday_locations_cli(cfg, conn, apply=True)
        with pytest.raises(run_lock.RunLockHeld):
            asyncio.run(
                daily_tools.employer_jd_cli(cfg, conn, apply=True, min_score=None, max_requests=1)
            )
    finally:
        holder.kill()
        holder.join()
    assert wrote == []

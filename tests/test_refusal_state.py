# ruff: noqa: F811
"""A refused call is "not run" even with no deadline scope (security review of #96): cockpit
runs and hand-run commands have none, yet after the first budget refusal the scorer must stop
trying, count no failed batch, and re-score must stamp no attempted row. Also the ledger's
hardening: deduped refusals, an honest Stop, PID reuse, never-entered streams."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time

import pytest

from blastjob import daily
from blastjob.db import client as dbc
from blastjob.llm import gateway, ledger
from blastjob.llm.cost import CostTracker
from blastjob.matching import rescore, scorer
from blastjob.models.config import BlastJobConfig, LimitsConfig, PricingConfig
from blastjob.web.runtime import Run, Runtime
from tests.test_ejd_u5_rescore_jobs import _job, cfg, conn  # noqa: F401


def _spend_the_clicked_cap(cfg, conn):
    cfg.limits.clicked_calls_per_day = 1
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO model_calls (started_at, ended_at, caller, origin, outcome) "
        "VALUES (?, ?, 'earlier', 'clicked', 'ok')",
        (stamp, stamp),
    )
    conn.commit()
    ledger.install(dbc.db_path(cfg), LimitsConfig(clicked_calls_per_day=1), PricingConfig())


async def _until(predicate, seconds: float = 10.0) -> None:
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        await asyncio.sleep(0.02)
    assert predicate()


async def test_a_cockpit_run_without_a_deadline_stops_the_scorer_at_the_first_refusal(
    cfg, conn, monkeypatch
):
    _spend_the_clicked_cap(cfg, conn)
    attempts: list[int] = []

    async def real_gateway_stream(*_a, **_k):
        attempts.append(1)
        await gateway.reserve_async("scoring")  # over the cap: BudgetExceeded
        raise AssertionError("unreachable")

    monkeypatch.setattr(scorer, "make_stream", real_gateway_stream)
    candidates = [
        {"job_id": i, "title": "VP", "company": f"Co{i}", "jd": "Lead a team. " * 20}
        for i in range(1, 7)
    ]
    out: list[scorer.ScoringResult] = []

    async def work(_run: Run):
        out.append(
            await scorer.score_candidates(
                BlastJobConfig(), "history", candidates, CostTracker(), batch_size=2
            )
        )

    run = Runtime().start("matching", work)  # a clicked run: no deadline scope anywhere
    await _until(lambda: run.status != "running")

    assert run.status == "done"
    assert attempts == [1]  # no retry, no later batch
    assert out[0].failed_batches == 0 and out[0].scores == {}
    refused = conn.execute("SELECT COUNT(*) FROM model_calls WHERE outcome='refused_budget'")
    assert refused.fetchone()[0] == 1


async def test_a_cockpit_rescore_hitting_the_cap_stamps_no_attempted_row(cfg, conn, monkeypatch):
    _spend_the_clicked_cap(cfg, conn)
    jid = _job(conn, "kept", 1, stale=1)

    async def refused(cfg_, wh, candidates, cost_tracker, **kw):
        try:
            await gateway.reserve_async("scoring")
        except gateway.BudgetExceeded:
            pass  # the scorer swallows it as a missing score
        return scorer.ScoringResult()

    monkeypatch.setattr(rescore, "score_candidates", refused)
    counts: list[rescore.RescoreCounts] = []

    async def work(_run: Run):
        counts.append(await rescore.rescore_jobs(cfg, conn, CostTracker(), [jid]))

    run = Runtime().start("matching", work)
    await _until(lambda: run.status != "running")

    assert counts[0].failed == [] and counts[0].not_run == [jid]
    row = conn.execute(
        "SELECT COALESCE(score_stale_attempted_at,'') FROM jobs WHERE id=?", (jid,)
    ).fetchone()
    assert row[0] == ""


def test_a_hand_run_daily_command_records_refusals_without_a_deadline(monkeypatch):
    seen: dict = {}

    def fake_dispatch(_args):
        seen["origin"] = gateway.current_origin()
        with pytest.raises(gateway.BudgetExceeded):
            ledger.install(
                dbc.db_path(None), LimitsConfig(scheduled_calls_per_day=1), PricingConfig()
            )
            gateway.reserve("a")
            gateway.reserve("b")
        seen["refused"] = gateway.deadline_hit()
        return 0

    monkeypatch.setattr(daily, "_dispatch", fake_dispatch)
    daily.main(["--status"])
    assert seen == {"origin": "scheduled", "refused": True}


# --- hardening ------------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "ledger.db"
    dbc.init_db_at(path).close()
    return path


def test_refusal_rows_are_one_per_origin_and_caller_per_day(db):
    ledger.install(db, LimitsConfig(clicked_calls_per_day=1), PricingConfig())
    gateway.finish(gateway.reserve("x"), "ok")
    for _ in range(5):
        with pytest.raises(gateway.BudgetExceeded):
            gateway.reserve("loop")
    with pytest.raises(gateway.BudgetExceeded):
        gateway.reserve("other")
    conn = dbc.connect_at(db)
    n = conn.execute("SELECT COUNT(*) FROM model_calls WHERE outcome='refused_budget'").fetchone()
    conn.close()
    assert n[0] == 2  # 'loop' once, 'other' once


async def test_reserve_async_runs_off_the_loop_and_a_cancelled_one_is_closed(db):
    ledger.install(db, LimitsConfig(), PricingConfig())
    res = await gateway.reserve_async("a")
    assert res.row_id is not None
    task = asyncio.ensure_future(gateway.reserve_async("b"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)
    conn = dbc.connect_at(db)
    outcomes = [r[0] for r in conn.execute("SELECT outcome FROM model_calls WHERE caller='b'")]
    conn.close()
    assert outcomes in ([], ["cancelled"])  # never left 'started'


async def test_stop_after_the_run_finished_normally_is_recorded_as_too_late():
    rt = Runtime()

    async def work(_run: Run):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            return "swallowed"  # does not unwind: finishes normally

    run = rt.start("x", work)
    await asyncio.sleep(0.05)
    assert rt.stop(run.id)
    await _until(lambda: run.status != "running")
    assert run.status == "done" and run.stopped_too_late
    assert any("too late" in e.data for e in run.events)


def test_pause_into_a_symlink_is_a_clear_error_not_a_500(tmp_path):
    from blastjob.core import pause

    (tmp_path / "elsewhere").write_text("x")
    os.symlink(tmp_path / "elsewhere", tmp_path / "PAUSE")
    with pytest.raises(pause.PauseError, match="Could not create the PAUSE file"):
        pause.set_paused(tmp_path, True)


def test_the_same_pid_with_another_start_time_is_a_reused_pid(db):
    ledger.install(db, LimitsConfig(), PricingConfig())
    assert ledger.owner_alive(os.getpid(), ledger.own_start_time()) is True
    assert ledger.owner_alive(os.getpid(), "Mon Jan  1 00:00:00 2001") is False


def test_an_old_row_with_no_recorded_start_time_does_not_look_live_forever(db):
    ledger.install(db, LimitsConfig(), PricingConfig())
    child = __import__("subprocess").Popen(["sleep", "30"])
    try:
        old = (dt.datetime.now() - dt.timedelta(days=2)).isoformat(timespec="seconds")
        assert ledger.owner_alive(child.pid, "", since=old) is False
        assert ledger.owner_alive(child.pid, "", since=ledger.now_iso()) is True
    finally:
        child.kill()
        child.wait()


async def test_a_stream_that_is_never_entered_is_closed_as_abandoned(db):
    import gc

    ledger.install(db, LimitsConfig(), PricingConfig())
    stream = gateway.BoundedStream(object(), 5.0, await gateway.reserve_async("never-entered"))
    del stream
    gc.collect()
    conn = dbc.connect_at(db)
    outcome = conn.execute("SELECT outcome FROM model_calls").fetchone()[0]
    conn.close()
    assert outcome == "abandoned"

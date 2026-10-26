# ruff: noqa: F811
"""The daily call budget (#96): a run that reaches `limits.scheduled_calls_per_day` stops like a
deadline cut (honest status, named steps, digest sent), the scorer stops at the first refusal,
and the "model calls yesterday" line only rides on a digest that is sent anyway."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from blastjob import daily
from blastjob.llm import gateway
from blastjob.llm.cost import CostTracker
from blastjob.matching import scorer
from blastjob.models.config import BlastJobConfig
from tests.test_daily_deadline import TODAY, Matched, _last, _run, _stub, clock, db  # noqa: F401


def _rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM model_calls ORDER BY id")]


def _use_the_whole_budget(conn):
    """One scheduled call already made today, with a cap of one."""
    conn.execute(
        "INSERT INTO model_calls (started_at, ended_at, caller, origin, outcome) "
        "VALUES (?, ?, 'earlier', 'scheduled', 'ok')",
        (dt.datetime.now().isoformat(timespec="seconds"),) * 2,
    )
    conn.commit()


@pytest.mark.asyncio
async def test_a_refused_call_stops_the_run_as_budget_with_an_honest_status(db, monkeypatch, clock):
    cfg, conn = db
    cfg.limits.scheduled_calls_per_day = 1
    _use_the_whole_budget(conn)

    def scoring():
        gateway.reserve("scoring")  # over the cap: BudgetExceeded
        raise AssertionError("unreachable")

    calls = _stub(monkeypatch, matching=scoring)
    sent: list[str] = []

    res = await _run(cfg, conn, sent)

    assert res.stopped_reason == "budget"
    assert "matching" not in res.errors and not res.errors  # a cap is not a failure
    assert calls["targets"] == 0  # nothing new after the cap
    assert res.sent and len(sent) == 1  # a budget stop always sends its digest
    assert "Stopped at the daily limit of 1 scheduled model call" in sent[0]
    assert "up to 2 jobs left for the next run" in sent[0]
    assert "Not run (daily call limit):" in sent[0] and "target scans" in sent[0]
    assert "time limit" not in sent[0]
    row = _last(conn)
    assert row["stopped_reason"] == "budget"
    status = daily._format_status(row, today=TODAY)
    assert status.startswith("STOPPED EARLY: the model call budget was reached")
    refused = [r for r in _rows(conn) if r["outcome"] == "refused_budget"]
    assert [r["origin"] for r in refused] == ["scheduled"]


@pytest.mark.asyncio
async def test_a_refusal_the_scorer_swallowed_still_marks_the_stop_as_budget(
    db, monkeypatch, clock
):
    cfg, conn = db
    cfg.limits.scheduled_calls_per_day = 1
    _use_the_whole_budget(conn)

    def swallowed():
        with pytest.raises(gateway.BudgetExceeded):
            gateway.reserve("scoring")
        m = Matched()
        m.scoring_failures = 1
        return m

    _stub(monkeypatch, matching=swallowed)
    sent: list[str] = []

    res = await _run(cfg, conn, sent)

    assert res.stopped_reason == "budget" and "scoring" not in res.errors
    assert "Stopped at the daily limit" in sent[0]


@pytest.mark.asyncio
async def test_daily_calls_are_recorded_as_scheduled_under_the_step_name(db, monkeypatch, clock):
    cfg, conn = db

    def scoring():
        gateway.finish(gateway.reserve("make_stream"), "ok")
        return Matched()

    _stub(monkeypatch, matching=scoring)
    await _run(cfg, conn, [])

    [row] = _rows(conn)
    assert (row["origin"], row["caller"]) == ("scheduled", "daily scoring")


@pytest.mark.asyncio
async def test_a_run_inside_the_budget_is_complete(db, monkeypatch, clock):
    cfg, conn = db
    _stub(monkeypatch)
    res = await _run(cfg, conn, [])
    assert res.stopped_reason == ""


def _yesterdays_calls(conn):
    stamp = (dt.date.today() - dt.timedelta(days=1)).isoformat() + "T09:00:00"
    for origin in ("scheduled", "scheduled", "clicked"):
        conn.execute(
            "INSERT INTO model_calls (started_at, ended_at, caller, origin, outcome, "
            "cost_usd, cost_known) VALUES (?, ?, 'x', ?, 'ok', 0.5, 1)",
            (stamp, stamp, origin),
        )
    conn.commit()


@pytest.mark.asyncio
async def test_the_calls_line_rides_on_a_digest_that_is_sent_anyway(db, monkeypatch, clock):
    cfg, conn = db
    _yesterdays_calls(conn)
    _stub(monkeypatch)
    sent: list[str] = []

    await daily.run_daily(cfg, conn, send=lambda t: _collect(sent, t), today=dt.date.today())

    assert sent, "two unscored jobs make a digest"
    assert "Model calls yesterday: 2 scheduled, 1 clicked (est. $1.50)" in sent[0]


async def _collect(sent: list[str], text: str) -> None:
    sent.append(text)


@pytest.mark.asyncio
async def test_the_calls_line_never_makes_a_quiet_day_send(db, monkeypatch, clock):
    cfg, conn = db
    conn.execute("UPDATE jobs SET review_state='archived'")  # nothing to announce
    conn.commit()
    _yesterdays_calls(conn)
    _stub(monkeypatch)
    # Not the weekly heartbeat day, which sends on purpose (this test failed every Monday).
    monkeypatch.setattr(daily, "HEARTBEAT_WEEKDAY", (dt.date.today().weekday() + 1) % 7)
    sent: list[str] = []

    res = await daily.run_daily(cfg, conn, send=lambda t: _collect(sent, t), today=dt.date.today())

    assert sent == [] and not res.sent and res.digest == ""


@pytest.mark.asyncio
async def test_unknown_cost_is_said_to_be_unknown_in_the_calls_line(db, monkeypatch, clock):
    cfg, conn = db
    stamp = (dt.date.today() - dt.timedelta(days=1)).isoformat() + "T09:00:00"
    conn.execute(
        "INSERT INTO model_calls (started_at, ended_at, caller, origin, outcome, cost_known) "
        "VALUES (?, ?, 'x', 'clicked', 'ok', 0)",
        (stamp, stamp),
    )
    conn.commit()
    _stub(monkeypatch)
    sent: list[str] = []

    await daily.run_daily(cfg, conn, send=lambda t: _collect(sent, t), today=dt.date.today())

    assert "Model calls yesterday: 0 scheduled, 1 clicked (unknown)" in sent[0]


# --- the scorer -----------------------------------------------------------------------------


def test_the_scorer_makes_no_further_attempt_after_a_budget_refusal(monkeypatch):
    calls: list[int] = []

    async def refusing(*_a, **_k):
        calls.append(1)
        gateway._mark_refused("budget")  # what reserve() does before it raises
        raise gateway.BudgetExceeded("daily model call limit reached")

    monkeypatch.setattr(scorer, "make_stream", refusing)
    candidates = [
        {"job_id": i, "title": "VP", "company": f"Co{i}", "jd": "Lead a team. " * 20}
        for i in range(1, 7)
    ]

    async def go() -> scorer.ScoringResult:
        with gateway.deadline_scope(600):
            return await scorer.score_candidates(
                BlastJobConfig(), "history", candidates, CostTracker(), batch_size=2
            )

    out = asyncio.run(go())

    assert calls == [1]  # one refused call: no retry, no later batch
    assert out.scores == {} and out.failed_batches == 0


@pytest.mark.asyncio
async def test_a_budget_stop_sends_its_digest_even_when_there_is_nothing_new(
    db, monkeypatch, clock
):
    cfg, conn = db
    cfg.limits.scheduled_calls_per_day = 1
    conn.execute("UPDATE jobs SET review_state='archived'")  # a quiet day: no role to announce
    conn.commit()
    _use_the_whole_budget(conn)

    def scoring():
        gateway.reserve("scoring")

    _stub(monkeypatch, matching=scoring)
    sent: list[str] = []

    res = await _run(cfg, conn, sent)

    assert res.stopped_reason == "budget" and res.sent
    assert len(sent) == 1 and "Stopped at the daily limit" in sent[0]

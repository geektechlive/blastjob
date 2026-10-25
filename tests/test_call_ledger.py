"""The model-call ledger and the atomic daily budget (#96, ADR 0060).

Nothing here reaches a model: `providers._open_stream` is faked, so the real `make_stream`,
`reserve`, `BoundedStream` and `finish` run against a temp database."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import subprocess
import sys
from datetime import date
from types import SimpleNamespace

import pytest

from blastjob.db import client as dbc
from blastjob.llm import gateway, ledger, ledger_stats, providers
from blastjob.models.config import BlastJobConfig, LimitsConfig, PricingConfig


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "ledger.db"
    dbc.init_db_at(path).close()
    return path


def _install(db, **limits):
    ledger.install(db, LimitsConfig(**limits), PricingConfig())


def _rows(db):
    conn = dbc.connect_at(db)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM model_calls ORDER BY id")]
    finally:
        conn.close()


class _Live:
    def __init__(self, usage):
        self._usage = usage

    @property
    def text_stream(self):
        return self._gen()

    async def _gen(self):
        yield "hi"

    async def get_final_message(self):
        return SimpleNamespace(usage=self._usage)


class _Manager:
    def __init__(self, usage=None, *, fail: BaseException | None = None):
        self._live = _Live(usage)
        self._fail = fail

    async def __aenter__(self):
        if self._fail is not None:
            raise self._fail
        return self._live

    async def __aexit__(self, *_a):
        return False


@pytest.fixture
def fake_stream(monkeypatch):
    """`make_stream` for a fake provider whose usage is set per test."""
    box = SimpleNamespace(usage=None, fail=None)

    async def _open(*_a, **_k):
        return _Manager(box.usage, fail=box.fail)

    monkeypatch.setattr(providers, "_open_stream", _open)
    monkeypatch.setattr(providers, "detect_provider", lambda _llm: "anthropic")
    return box


async def _call():
    # conftest replaces each module's own `make_stream` binding; providers' own is the real one.
    stream = await providers.make_stream(BlastJobConfig(), [], [], 10)
    async with stream:
        async for _ in stream.text_stream:
            pass
        await stream.get_final_message()


# --- ledger rows --------------------------------------------------------------------------


async def test_a_call_writes_one_closed_row_with_origin_tokens_and_cost(db, fake_stream):
    _install(db)
    fake_stream.usage = SimpleNamespace(input_tokens=1000, output_tokens=200)
    with gateway.origin_scope("scheduled"), gateway.caller_scope("scoring"):
        await _call()
    [row] = _rows(db)
    assert (row["origin"], row["caller"], row["outcome"]) == ("scheduled", "scoring", "ok")
    assert (row["input_tokens"], row["output_tokens"], row["cost_known"]) == (1000, 200, 1)
    assert row["ended_at"] and row["cost_usd"] == pytest.approx(0.006)
    assert row["owner_pid"] == os.getpid() and row["owner_started"]


async def test_the_default_origin_is_clicked_and_the_caller_falls_back_to_the_call_site(
    db, fake_stream
):
    _install(db)
    fake_stream.usage = SimpleNamespace(input_tokens=1, output_tokens=1)
    await _call()
    [row] = _rows(db)
    assert (row["origin"], row["caller"]) == ("clicked", "make_stream")


async def test_unknown_usage_is_recorded_as_unknown_never_zero(db, fake_stream):
    _install(db)
    fake_stream.usage = None
    await _call()
    [row] = _rows(db)
    assert row["outcome"] == "ok" and row["cost_known"] == 0
    assert row["cost_usd"] is None and row["input_tokens"] is None


async def test_a_failed_open_closes_the_row_as_an_error(db, fake_stream):
    _install(db)
    fake_stream.fail = gateway.CLIError("boom")
    with pytest.raises(gateway.CLIError):
        await _call()
    assert [r["outcome"] for r in _rows(db)] == ["error"]


async def test_a_cancelled_call_closes_the_row_as_cancelled(db, fake_stream, monkeypatch):
    _install(db)
    fake_stream.fail = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await _call()
    assert [r["outcome"] for r in _rows(db)] == ["cancelled"]


async def test_a_timed_out_call_closes_the_row_as_a_timeout(db, fake_stream):
    _install(db)
    fake_stream.fail = gateway.CallTimeout("slow")
    with pytest.raises(gateway.CLIError):
        await _call()
    assert [r["outcome"] for r in _rows(db)] == ["timeout"]


def test_with_nothing_installed_the_ledger_and_budget_are_a_no_op(tmp_path):
    ledger.uninstall()
    reservation = gateway.reserve("x")
    gateway.finish(reservation, "ok")
    assert reservation.row_id is None
    assert not list(tmp_path.iterdir())


# --- the budget -----------------------------------------------------------------------------


def test_over_the_cap_writes_a_refused_row_and_raises_budget_exceeded(db):
    _install(db, clicked_calls_per_day=2)
    gateway.finish(gateway.reserve("a"), "ok")
    gateway.finish(gateway.reserve("b"), "ok")
    with pytest.raises(gateway.BudgetExceeded, match="2 of 2 clicked calls"):
        gateway.reserve("c")
    assert [r["outcome"] for r in _rows(db)] == ["ok", "ok", "refused_budget"]
    # A refusal never counts toward the cap, so it does not lock the day further.
    with pytest.raises(gateway.BudgetExceeded):
        gateway.reserve("d")
    assert len(_rows(db)) == 4


def test_budget_exceeded_is_a_cli_error_and_marks_the_deadline_scope_as_refused(db):
    _install(db, scheduled_calls_per_day=1)
    with gateway.origin_scope("scheduled"), gateway.deadline_scope(600) as state:
        gateway.finish(gateway.reserve("a"), "ok")
        with pytest.raises(gateway.CLIError):
            gateway.reserve("b")
        assert state.hit and state.reason == "budget" and gateway.deadline_hit()


def test_each_origin_has_its_own_cap(db):
    _install(db, scheduled_calls_per_day=1, clicked_calls_per_day=1)
    with gateway.origin_scope("scheduled"):
        gateway.finish(gateway.reserve("a"), "ok")
        with pytest.raises(gateway.BudgetExceeded):
            gateway.reserve("b")
    gateway.finish(gateway.reserve("c"), "ok")  # clicked is untouched


def test_yesterdays_calls_do_not_count_toward_today(db):
    _install(db, clicked_calls_per_day=1)
    conn = dbc.connect_at(db)
    conn.execute(
        "INSERT INTO model_calls (started_at, caller, origin, outcome) "
        "VALUES ('2000-01-01T10:00:00','old','clicked','ok')"
    )
    conn.commit()
    conn.close()
    gateway.finish(gateway.reserve("today"), "ok")


def test_an_unusable_ledger_refuses_the_call_instead_of_waving_it_through(tmp_path):
    ledger.install(tmp_path / "no-such-dir" / "x" / "ledger.db", LimitsConfig(), PricingConfig())
    # Make the path unopenable: a file where the parent directory would be.
    (tmp_path / "no-such-dir").write_text("not a directory")
    with pytest.raises(gateway.CLIError, match="ledger unavailable"):
        gateway.reserve("x")


def _race_worker(path: str, barrier, out) -> None:
    ledger.install(
        __import__("pathlib").Path(path), LimitsConfig(clicked_calls_per_day=5), PricingConfig()
    )
    barrier.wait()
    try:
        out.put(ledger.reserve("race", "clicked").refused)
    except Exception as e:  # noqa: BLE001
        out.put(f"error: {e}")


def test_racing_processes_at_the_cap_minus_one_start_exactly_one_call(db):
    _install(db, clicked_calls_per_day=5)
    for _ in range(4):
        gateway.finish(gateway.reserve("pre"), "ok")
    ctx = multiprocessing.get_context("spawn")
    workers = 6
    barrier, out = ctx.Barrier(workers), ctx.Queue()
    procs = [ctx.Process(target=_race_worker, args=(str(db), barrier, out)) for _ in range(workers)]
    for p in procs:
        p.start()
    results = [out.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=30)
    assert sorted(map(str, results)) == ["False"] + ["True"] * (workers - 1), results
    started = [r for r in _rows(db) if r["caller"] == "race" and r["outcome"] == "started"]
    assert len(started) == 1


# --- abandoned rows ---------------------------------------------------------------------------


def _insert_started(db, pid, started, origin="clicked"):
    conn = dbc.connect_at(db)
    conn.execute(
        "INSERT INTO model_calls (started_at, caller, origin, outcome, owner_pid, owner_started) "
        "VALUES (?, 'x', ?, 'started', ?, ?)",
        (ledger.now_iso(), origin, pid, started),
    )
    conn.commit()
    conn.close()


def _outcomes(db):
    return [r["outcome"] for r in _rows(db)]


def test_a_started_row_whose_owner_is_dead_becomes_abandoned_with_unknown_cost(db):
    _install(db)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _insert_started(db, dead.pid, "Mon Jan  1 00:00:00 2001")
    assert ledger.reconcile_abandoned(db) == 1
    [row] = _rows(db)
    assert row["outcome"] == "abandoned" and row["cost_known"] == 0 and row["ended_at"]


def test_a_live_owner_with_the_same_start_time_is_left_alone(db):
    _install(db)
    child = subprocess.Popen(["sleep", "30"])
    try:
        _insert_started(db, child.pid, ledger.process_start_time(child.pid))
        assert ledger.reconcile_abandoned(db) == 0
        assert _outcomes(db) == ["started"]
    finally:
        child.kill()
        child.wait()


def test_a_reused_pid_with_a_different_start_time_is_not_the_owner(db):
    _install(db)
    child = subprocess.Popen(["sleep", "30"])  # alive, but not the process that made the call
    try:
        _insert_started(db, child.pid, "Mon Jan  1 00:00:00 2001")
        assert ledger.reconcile_abandoned(db) == 1
        assert _outcomes(db) == ["abandoned"]
    finally:
        child.kill()
        child.wait()


def test_this_process_own_started_rows_are_never_abandoned(db):
    _install(db)
    _insert_started(db, os.getpid(), ledger.own_start_time())
    assert ledger.reconcile_abandoned(db) == 0


def test_the_gateway_reconciles_on_first_use_and_abandoned_rows_still_count(db):
    _install(db, clicked_calls_per_day=1)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _insert_started(db, dead.pid, "x")
    with pytest.raises(gateway.BudgetExceeded):  # the abandoned call may have spent
        gateway.reserve("next")
    assert _outcomes(db) == ["abandoned", "refused_budget"]


def test_process_start_time_reads_the_os_value_for_this_process():
    assert ledger.process_start_time(os.getpid()) != ""
    assert ledger.process_start_time(2**22 + 12345) == ""


# --- read side --------------------------------------------------------------------------------


def test_stats_group_by_caller_and_origin_and_never_show_unknown_cost_as_zero(db):
    _install(db, clicked_calls_per_day=10)
    known = SimpleNamespace(input_tokens=1_000_000, output_tokens=0)
    gateway.finish(gateway.reserve("a"), "ok", known)
    gateway.finish(gateway.reserve("a"), "ok")  # usage unknown
    conn = dbc.connect_at(db)
    try:
        totals = {t.origin: t for t in ledger_stats.origin_totals(conn, date.today())}
        clicked = totals["clicked"]
        assert (clicked.calls, clicked.unknown_cost_calls) == (2, 1)
        assert ledger_stats.describe_spend(clicked) == "$3.0000 known + 1 call of unknown cost"
        only_unknown = ledger_stats.OriginTotal("clicked", 1, 0.0, 1, 0)
        assert ledger_stats.describe_spend(only_unknown) == "unknown"
        assert "Model calls yesterday: 0 scheduled, 2 clicked" in ledger_stats.calls_line(
            conn, date.today()
        )
        assert ledger_stats.calls_line(conn, date(2000, 1, 1)) == ""
    finally:
        conn.close()

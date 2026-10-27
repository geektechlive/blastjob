"""The category search's place in the daily run, and its offline guard (#91)."""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from blastjob import daily, daily_steps
from blastjob.core import category_store as cs
from blastjob.db import client as dbc
from blastjob.discovery import category_search as cat
from blastjob.llm import gateway
from blastjob.models.config import BlastJobConfig

WEDNESDAY = dt.date(2026, 10, 28)
THURSDAY = dt.date(2026, 10, 29)


@pytest.fixture
def cfg(tmp_path) -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = str(tmp_path)
    c.discovery.search_firm_weekday = WEDNESDAY.weekday()
    return c


@pytest.fixture
def conn(cfg) -> sqlite3.Connection:
    c = dbc.init_db(cfg)
    yield c
    c.close()


async def test_the_default_search_and_read_fail_loudly_in_tests():
    with pytest.raises(pytest.fail.Exception):
        await cat._search("q", "k")
    with pytest.raises(pytest.fail.Exception):
        await cat._read("https://example.com/jobs/1", "k")
    assert cat._jina_key() == ""


class _StubRun:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, cfg, conn, **kw):
        self.calls += 1
        return cat.CategoryRunResult(
            ran=True, errors={"category-web:x": "boom"}, notes={"category:x": "capped"}
        )


async def _step(cfg, conn, monkeypatch, today, *, hit=False):
    stub = _StubRun()
    monkeypatch.setattr(cat, "run_category_searches", stub)
    res = daily.DailyResult()
    state = gateway.DeadlineState(hit=hit)
    await daily_steps.category_step(cfg, conn, res, state, today=today)
    return stub, res


def _category(conn) -> int:
    return cs.add_category(conn, label="Example robotics firms", terms="x", is_dream=False)


async def test_the_step_does_nothing_off_the_weekday_or_without_categories(cfg, conn, monkeypatch):
    _category(conn)
    stub, res = await _step(cfg, conn, monkeypatch, THURSDAY)
    assert stub.calls == 0 and res.skipped_steps == []
    conn.execute("DELETE FROM category_searches")
    conn.commit()
    stub, res = await _step(cfg, conn, monkeypatch, WEDNESDAY, hit=True)
    assert stub.calls == 0 and res.skipped_steps == []


async def test_the_step_runs_on_the_weekday_and_reports_errors_and_notes(cfg, conn, monkeypatch):
    _category(conn)
    stub, res = await _step(cfg, conn, monkeypatch, WEDNESDAY)
    assert stub.calls == 1
    assert res.errors["category-web:x"] == "boom" and res.notes["category:x"] == "capped"


async def test_the_step_names_itself_when_the_deadline_cut_it(cfg, conn, monkeypatch):
    _category(conn)
    stub, res = await _step(cfg, conn, monkeypatch, WEDNESDAY, hit=True)
    assert stub.calls == 0 and res.skipped_steps == ["category searches"]


async def test_a_crash_in_the_run_is_reported_not_raised(cfg, conn, monkeypatch):
    _category(conn)

    async def boom(*_a, **_k):
        raise RuntimeError("search exploded")

    monkeypatch.setattr(cat, "run_category_searches", boom)
    res = daily.DailyResult()
    await daily_steps.category_step(cfg, conn, res, gateway.DeadlineState(), today=WEDNESDAY)
    assert "search exploded" in res.errors["category searches"]

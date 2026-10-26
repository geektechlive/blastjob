"""The "Recruiters to touch" digest block and its stamping (#98, ADR 0062)."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from blastjob import daily
from blastjob.core import recruiter_status as rs
from blastjob.daily_digest import build_digest
from blastjob.db import client as dbc
from blastjob.notify import discord
from tests.asks_fixture import add_recruiter, make_cfg
from tests.test_outreach_asks_digest import Sink, _stub_pipeline

TUE = dt.date(2026, 10, 27)


@pytest.fixture
def env(monkeypatch):
    _stub_pipeline(monkeypatch)
    cfg = make_cfg()
    conn = dbc.init_db(cfg)
    yield cfg, conn
    conn.close()


def _spoke(conn, name, days_ago, messages=1):
    when = (TUE - dt.timedelta(days=days_ago)).isoformat()
    return add_recruiter(conn, name, messages=messages, last_message_at=when)


def _reminded(conn, rid):
    return conn.execute(
        "SELECT recruiter_reminded_for FROM network_contacts WHERE id=?", (rid,)
    ).fetchone()[0]


@pytest.mark.asyncio
async def test_quiet_day_without_a_due_recruiter_sends_nothing(env):
    cfg, conn = env
    _spoke(conn, "Ria Fox", 10)
    sink = Sink()
    await daily.run_daily(cfg, conn, send=sink, today=TUE)
    assert sink.sent == []


@pytest.mark.asyncio
async def test_quiet_day_with_a_due_recruiter_still_sends_and_stamps(env):
    cfg, conn = env
    rid = _spoke(conn, "Ria Fox", 60)
    sink = Sink()
    res = await daily.run_daily(cfg, conn, send=sink, today=TUE)
    assert res.sent and len(sink.sent) == 1
    text = sink.sent[0]
    assert "**Recruiters to touch**" in text and "Ria Fox" in text
    assert "Northwind Search" in text and "60 days" in text and "/network>" in text
    assert _reminded(conn, rid) == (TUE - dt.timedelta(days=60)).isoformat()


@pytest.mark.asyncio
async def test_not_sent_again_until_a_new_touch(env):
    cfg, conn = env
    rid = _spoke(conn, "Ria Fox", 60)
    sink = Sink()
    await daily.run_daily(cfg, conn, send=sink, today=TUE)
    await daily.run_daily(cfg, conn, send=sink, today=TUE + dt.timedelta(days=1))
    assert len(sink.sent) == 1
    conn.execute("UPDATE network_contacts SET last_message_at=? WHERE id=?", ("2026-09-10", rid))
    conn.commit()
    await daily.run_daily(cfg, conn, send=sink, today=TUE + dt.timedelta(days=2))
    assert len(sink.sent) == 2


@pytest.mark.asyncio
async def test_the_touch_days_setting_is_used(env):
    cfg, conn = env
    _spoke(conn, "Ria Fox", 20)
    sink = Sink()
    await daily.run_daily(cfg, conn, send=sink, today=TUE)
    assert sink.sent == []
    cfg.notify.recruiter_touch_days = 14
    await daily.run_daily(cfg, conn, send=sink, today=TUE)
    assert len(sink.sent) == 1


@pytest.mark.asyncio
async def test_seven_due_only_the_five_rendered_are_stamped(env):
    cfg, conn = env
    ids = [_spoke(conn, f"Pat {i} Lee", 60, messages=i + 1) for i in range(7)]
    sink = Sink()
    await daily.run_daily(cfg, conn, send=sink, today=TUE)
    stamped = [i for i in ids if _reminded(conn, i)]
    assert sorted(stamped) == sorted(ids[2:])  # the five warmest; the two coolest lead next time
    assert sink.sent[0].count("Lee") == 5


@pytest.mark.asyncio
async def test_a_failed_send_and_a_dry_run_stamp_nothing(env):
    cfg, conn = env
    rid = _spoke(conn, "Ria Fox", 60)
    with pytest.raises(RuntimeError):
        await daily.run_daily(cfg, conn, send=Sink(fail=True), today=TUE)
    assert _reminded(conn, rid) == ""
    await daily.run_daily(cfg, conn, send=Sink(), dry_run=True, today=TUE)
    assert _reminded(conn, rid) == ""


@pytest.mark.asyncio
async def test_a_later_chunk_failing_stamps_nothing(env, monkeypatch):
    cfg, conn = env
    ids = [_spoke(conn, f"Pat {i} Lee", 60, messages=i + 1) for i in range(5)]
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, json={"id": "1"})
        return httpx.Response(403, json={"message": "Missing Access"})

    original = discord.chunk_message
    monkeypatch.setattr(discord, "chunk_message", lambda text, limit=120: original(text, limit))

    async def send(text: str) -> None:
        await discord.send_message(
            text,
            token="t",
            channel_id="c",
            max_retries=1,
            transport=httpx.MockTransport(handler),
        )

    with pytest.raises(httpx.HTTPStatusError):
        await daily.run_daily(cfg, conn, send=send, today=TUE)
    assert len(calls) >= 2  # the first chunk went out, a later one failed
    assert all(_reminded(conn, i) == "" for i in ids)


@pytest.mark.asyncio
async def test_off_mode_stamps_like_the_other_blocks(env):
    cfg, conn = env
    cfg.notify.mode = "off"
    rid = _spoke(conn, "Ria Fox", 60)
    send = daily.sender_for(cfg, dry_run=False)
    res = await daily.run_daily(cfg, conn, send=send, today=TUE)
    assert res.sent
    assert _reminded(conn, rid) != ""


def test_block_lines_defang_names_and_link_the_network_page(env):
    _, conn = env
    rid = _spoke(conn, "Ria <@everyone> https://evil.example.com/x", 60)
    reminders = rs.due_reminders(conn, TUE, touch_days=42, lookback_days=365)
    text = build_digest([], {}, [], recruiter_touches=reminders, base_url="http://h")
    assert rid and "@everyone" not in text and "evil.example.com" not in text
    assert "<http://h/network>" in text

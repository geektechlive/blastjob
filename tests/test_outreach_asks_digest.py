"""The "Asks to follow up" digest block and its stamping (#97)."""

from __future__ import annotations

import datetime as dt

import pytest

from blastjob import daily
from blastjob.core import outreach_asks as oa
from blastjob.daily_digest import build_digest
from blastjob.db import client as dbc
from blastjob.discovery.pipeline import DiscoveryResult
from tests.asks_fixture import add_job, add_network, make_cfg

TUE = dt.date(2026, 10, 27)  # not the heartbeat Monday
WED = dt.date(2026, 10, 28)


def _stub_pipeline(monkeypatch) -> None:
    """Discovery and matching do nothing, so only the asks decide whether a digest goes out."""
    import blastjob.discovery.pipeline as dp
    import blastjob.matching.pipeline as mp

    async def fake_discovery(cfg, conn, adapters=None):
        return DiscoveryResult(fetched=0, per_source={}, errors={})

    async def fake_matching(cfg, conn, tracker, on_progress=None, **kw):
        return mp.MatchResult()

    monkeypatch.setattr(dp, "run_discovery", fake_discovery)
    monkeypatch.setattr(mp, "run_matching", fake_matching)


@pytest.fixture
def env(monkeypatch):
    _stub_pipeline(monkeypatch)
    cfg = make_cfg()
    conn = dbc.init_db(cfg)
    yield cfg, conn
    conn.close()


class Sink:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def __call__(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("down")
        self.sent.append(text)


def _due_ask(conn, name="Ann Lee", follow_up="2026-10-20", job=None, kind="advice"):
    n = add_network(conn, name)
    return oa.log_ask(
        conn,
        job_id=job,
        network_contact_id=n,
        contact_id=None,
        kind=kind,
        channel="email",
        asked_on="2026-10-10",
        follow_up_on=follow_up,
        today=dt.date(2026, 10, 26),
    )


def _notified(conn, ask):
    return conn.execute(
        "SELECT follow_up_notified_on FROM outreach_asks WHERE id=?", (ask,)
    ).fetchone()[0]


@pytest.mark.asyncio
async def test_quiet_day_without_asks_sends_nothing(env):
    cfg, conn = env
    sink = Sink()
    await daily.run_daily(cfg, conn, send=sink, today=TUE)
    assert sink.sent == []


@pytest.mark.asyncio
async def test_quiet_day_with_due_ask_still_sends_block(env):
    cfg, conn = env
    job = add_job(conn, "1")
    ask = _due_ask(conn, job=job)
    sink = Sink()
    res = await daily.run_daily(cfg, conn, send=sink, today=TUE)
    assert res.sent and len(sink.sent) == 1
    text = sink.sent[0]
    assert "**Asks to follow up**" in text and "Ann Lee" in text
    assert f"/jobs/{job}>" in text and "7 days overdue" in text
    assert _notified(conn, ask) == "2026-10-20"


@pytest.mark.asyncio
async def test_quiet_the_day_after_a_send_and_rearmed_by_a_new_date(env):
    cfg, conn = env
    ask = _due_ask(conn)
    sink = Sink()
    await daily.run_daily(cfg, conn, send=sink, today=TUE)
    await daily.run_daily(cfg, conn, send=sink, today=WED)
    assert len(sink.sent) == 1  # nothing new the next day
    oa.update_ask(conn, ask, status="waiting", follow_up_on="2026-10-28", note="")
    await daily.run_daily(cfg, conn, send=sink, today=WED)
    assert len(sink.sent) == 2 and "Asks to follow up" in sink.sent[1]


@pytest.mark.asyncio
async def test_failed_send_and_dry_run_stamp_nothing(env):
    cfg, conn = env
    ask = _due_ask(conn)
    with pytest.raises(RuntimeError):
        await daily.run_daily(cfg, conn, send=Sink(fail=True), today=TUE)
    assert _notified(conn, ask) == ""
    await daily.run_daily(cfg, conn, send=Sink(), dry_run=True, today=TUE)
    assert _notified(conn, ask) == ""


@pytest.mark.asyncio
async def test_eleven_due_only_ten_stamped(env):
    cfg, conn = env
    ids = [_due_ask(conn, f"P{i} Lee", follow_up=f"2026-10-{10 + i:02d}") for i in range(11)]
    sink = Sink()
    await daily.run_daily(cfg, conn, send=sink, today=TUE)
    stamped = [i for i in ids if _notified(conn, i)]
    assert len(stamped) == 10 and ids[10] not in stamped  # the newest date is the 11th
    assert sink.sent[0].count("Lee") == 10


def test_compare_and_set_leaves_a_moved_date_armed(env):
    _, conn = env
    ask = _due_ask(conn)
    picked = [(ask, "2026-10-20")]
    conn.execute("UPDATE outreach_asks SET follow_up_on='2026-10-25' WHERE id=?", (ask,))
    conn.commit()
    oa.mark_followups_notified(conn, picked)
    assert _notified(conn, ask) == ""
    assert [r["id"] for r in oa.due_followups(conn, dt.date(2026, 10, 26))] == [ask]


def test_jobless_line_names_company_kind_and_links_network(env):
    _, conn = env
    _due_ask(conn, "Cy Moe", kind="recruiter")
    rows = oa.rendered_due_followups(conn, TUE)
    text = build_digest([], {}, [], ask_followups=rows, base_url="http://h")
    assert "Cy Moe" in text and "Acme" in text and "recruiter note" in text
    assert "<http://h/network>" in text


def test_block_follows_existing_followups_block(env):
    _, conn = env
    _due_ask(conn)
    rows = oa.rendered_due_followups(conn, TUE)
    fups = [{"id": 5, "company": "Zed", "title": "T", "followup_reason": "r"}]
    text = build_digest([], {}, fups, ask_followups=rows)
    assert text.index("**Follow-ups due**") < text.index("**Asks to follow up**")


def test_untrusted_name_is_defanged(env):
    _, conn = env
    _due_ask(conn, "@everyone [x](http://evil) Lee")
    text = build_digest([], {}, [], ask_followups=oa.rendered_due_followups(conn, TUE))
    assert "@everyone" not in text and "](http" not in text

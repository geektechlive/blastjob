"""Target news in the daily run and its digest (#85).

The block lists events found since the last digest, stamps only the ones it rendered
and only after the send succeeded, and says so when scans failed instead of staying
silent. Search, reader and model are fakes; the pipeline is stubbed as in the other
daily tests.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from blastjob import daily
from blastjob.core import network as net
from blastjob.core import target_store as ts
from blastjob.core import target_watch as tw
from blastjob.daily_digest import TargetNewsItem, build_digest
from blastjob.db import client as dbc
from blastjob.discovery.pipeline import DiscoveryResult
from blastjob.llm.cost import CallCost
from blastjob.models.config import BlastJobConfig
from tests.test_target_watch import World

TODAY = dt.date(2026, 10, 24)
NOW = dt.datetime(2026, 10, 24, 7, 30, 0)


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    cfg.discovery.home_state = "WI"
    conn = dbc.init_db(cfg)

    async def fake_discovery(cfg, conn, adapters=None):
        return DiscoveryResult(fetched=0, per_source={}, errors={})

    class M:
        cost = CallCost()
        scored = 0
        surfaced = 0
        queued = 0
        scoring_failures = 0
        notes: list[str] = []

    async def fake_matching(cfg, conn, tracker, on_progress=None, **kw):
        return M()

    import blastjob.discovery.pipeline as dp
    import blastjob.matching.pipeline as mp

    monkeypatch.setattr(dp, "run_discovery", fake_discovery)
    monkeypatch.setattr(mp, "run_matching", fake_matching)
    yield cfg, conn
    conn.close()


def _wire(monkeypatch, world: World, key: str = "k") -> None:
    monkeypatch.setattr(tw, "_jina_key", lambda: key)
    monkeypatch.setattr(tw, "_search", world.search)
    monkeypatch.setattr(tw, "_read", world.read)


def _seed_event(
    conn, name="Acme Widgets", span="named Dana Roe as its new chief executive officer"
):
    ts.add_targets(conn, [ts.TargetInput(name)], now=NOW)
    key = ts.list_targets(conn)[0]["company_key"]
    event = ts.NewEvent(
        "new_ceo", "https://news.example.com/a", "t", span, "2026-10-20", "news_report",
        "located_mention",
    )  # fmt: skip
    ts.record_scan(conn, key, ok=True, status="ok", events=[event], now=NOW)
    return key


class Sink:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> None:
        self.sent.append(text)


def _item_for(**kw) -> TargetNewsItem:
    base = {
        "id": 1,
        "company_key": "acme widgets",
        "company": "Acme Widgets",
        "event_type": "new_ceo",
        "published_on": "2026-10-20",
        "span": "named Dana Roe as its new chief executive officer",
        "host": "news.example.com",
        "basis": "news_report",
        "contacts": 2,
    }
    return TargetNewsItem(**{**base, **kw})


# ------------------------------------------------------------------ formatting


def test_the_target_news_block_names_event_date_source_contacts_and_link():
    text = build_digest([], {}, [], base_url="http://127.0.0.1:8765", target_news=[_item_for()])
    assert "**Target news**" in text
    assert "Acme Widgets" in text and "New CEO" in text
    assert "published 2026-10-20" in text
    assert "news report" in text and "news.example.com" in text
    assert "2 contacts" in text
    assert "http://127.0.0.1:8765/targets/acme%20widgets" in text
    assert "confirmed" not in text.lower()


def test_no_target_news_means_no_block_and_an_empty_digest():
    assert build_digest([], {}, []) == ""
    assert "Target news" not in build_digest([], {"x": "y"}, [])


def test_a_digest_with_only_target_news_is_not_empty():
    assert build_digest([], {}, [], target_news=[_item_for()]) != ""


def test_failed_scans_get_one_line_not_silence():
    text = build_digest([], {}, [], target_failed=3, base_url="http://127.0.0.1:8765")
    assert "3 target scans failed" in text
    assert "1 target scan failed" in build_digest([], {}, [], target_failed=1)


def test_span_text_cannot_inject_mentions_markup_or_links():
    nasty = "@everyone **bold** [x](https://evil.example) <https://evil.example> `code`"
    text = build_digest([], {}, [], target_news=[_item_for(span=nasty)])
    assert "@everyone" not in text and "evil.example" not in text
    assert "<https://" not in text.split("cockpit")[0]


def test_only_the_cap_is_rendered_with_an_overflow_line():
    items = [_item_for(id=i) for i in range(1, 13)]
    text = build_digest([], {}, [], target_news=items, base_url="http://127.0.0.1:8765")
    assert text.count("New CEO") == daily.MAX_TARGET_NEWS
    assert "and 4 more" in text and "http://127.0.0.1:8765/targets" in text


# ------------------------------------------------------------------- the run


@pytest.mark.asyncio
async def test_a_run_scans_reports_and_stamps_the_event(env, monkeypatch):
    cfg, conn = env
    ts.add_targets(conn, [ts.TargetInput("Acme Widgets")], now=NOW)
    conn.execute(
        "INSERT INTO network_contacts (full_name, company, company_key, position) "
        "VALUES ('Pat Lane','Acme Widgets','acme widgets','Director')"
    )
    conn.commit()
    _wire(monkeypatch, World(monkeypatch))
    sink = Sink()
    res = await daily.run_daily(cfg, conn, send=sink, today=TODAY)
    assert res.sent and "**Target news**" in sink.sent[0]
    assert "published 2026-10-20" in sink.sent[0] and "1 contact" in sink.sent[0]
    assert conn.execute("SELECT notified_at FROM company_events").fetchone()["notified_at"]

    again = Sink()
    await daily.run_daily(cfg, conn, send=again, today=TODAY + dt.timedelta(days=1))
    assert not again.sent or "Target news" not in again.sent[0]


@pytest.mark.asyncio
async def test_a_failed_send_leaves_events_unnotified_and_they_lead_the_next_digest(
    env, monkeypatch
):
    cfg, conn = env
    _seed_event(conn)

    async def boom(text: str) -> None:
        raise httpx.HTTPError("discord down")

    with pytest.raises(httpx.HTTPError):
        await daily.run_daily(cfg, conn, send=boom, today=TODAY)
    assert conn.execute("SELECT notified_at FROM company_events").fetchone()["notified_at"] == ""
    sink = Sink()
    await daily.run_daily(cfg, conn, send=sink, today=TODAY + dt.timedelta(days=1))
    assert "New CEO" in sink.sent[0]


@pytest.mark.asyncio
async def test_overflow_events_stay_unnotified_for_the_next_digest(env):
    cfg, conn = env
    ts.add_targets(conn, [ts.TargetInput("Acme Widgets")], now=NOW)
    events = [
        ts.NewEvent(
            "new_ceo",
            f"https://news.example.com/{i}",
            "t",
            f"Acme Widgets item number {i} here",
            f"2026-10-{10 + i:02d}",
            "news_report",
            "located_mention",
        )  # fmt: skip
        for i in range(12)
    ]
    ts.record_scan(conn, "acme widgets", ok=True, status="ok", events=events, now=NOW)
    sink = Sink()
    await daily.run_daily(cfg, conn, send=sink, today=TODAY)
    stamped = conn.execute(
        "SELECT COUNT(*) n FROM company_events WHERE notified_at<>''"
    ).fetchone()["n"]
    assert stamped == daily.MAX_TARGET_NEWS
    sink2 = Sink()
    await daily.run_daily(cfg, conn, send=sink2, today=TODAY + dt.timedelta(days=1))
    assert sink2.sent[0].count("New CEO") == 12 - daily.MAX_TARGET_NEWS


@pytest.mark.asyncio
async def test_a_dry_run_scans_but_stamps_and_stores_nothing(env, monkeypatch):
    cfg, conn = env
    ts.add_targets(conn, [ts.TargetInput("Acme Widgets")], now=NOW)
    world = World(monkeypatch)
    _wire(monkeypatch, world)
    before = list(conn.iterdump())
    sink = Sink()
    await daily.run_daily(cfg, conn, send=sink, dry_run=True, today=TODAY)
    assert world.queries
    assert [ln for ln in conn.iterdump() if "daily_runs" not in ln] == [
        ln for ln in before if "daily_runs" not in ln
    ]


@pytest.mark.asyncio
async def test_a_failed_scan_is_reported_in_the_digest(env, monkeypatch):
    cfg, conn = env
    ts.add_targets(conn, [ts.TargetInput("Acme Widgets")], now=NOW)
    world = World(monkeypatch)
    world.search_error = RuntimeError("503")
    _wire(monkeypatch, world)
    sink = Sink()
    res = await daily.run_daily(cfg, conn, send=sink, today=TODAY)
    assert res.target_failed == 1
    assert "1 target scan failed" in sink.sent[0]
    assert "Source errors" not in sink.sent[0]


@pytest.mark.asyncio
async def test_no_targets_means_no_network_and_no_block(env, monkeypatch):
    cfg, conn = env
    world = World(monkeypatch)
    _wire(monkeypatch, world)
    sink = Sink()
    await daily.run_daily(cfg, conn, send=sink, today=TODAY)
    assert world.queries == [] and world.reads == []
    assert not sink.sent


@pytest.mark.asyncio
async def test_targets_without_a_jina_key_say_so_in_a_note(env, monkeypatch):
    cfg, conn = env
    ts.add_targets(conn, [ts.TargetInput("Acme Widgets")], now=NOW)
    sink = Sink()
    res = await daily.run_daily(cfg, conn, send=sink, today=TODAY)
    assert "JINA_API_KEY" in res.notes["target_watch"]


@pytest.mark.asyncio
async def test_the_scan_cost_joins_the_run_total(env, monkeypatch):
    cfg, conn = env
    ts.add_targets(conn, [ts.TargetInput("Acme Widgets")], now=NOW)
    _wire(monkeypatch, World(monkeypatch))
    res = await daily.run_daily(cfg, conn, send=Sink(), today=TODAY)
    assert res.target_cost.input_tokens > 0
    assert res.has_cost


# ------------------------------------------------------------ network helpers


def test_decision_maker_counts_are_batched_and_use_the_enrichment_flag(env):
    _cfg, conn = env
    rows = [
        ("A One", "acme widgets", 1),
        ("B Two", "acme widgets", 0),
        ("C Three", "northwind", 1),
    ]
    for name, key, dm in rows:
        conn.execute(
            "INSERT INTO network_contacts (full_name, company, company_key, enrich_decision_maker) "
            "VALUES (?,?,?,?)",
            (name, key, key, dm),
        )
    conn.commit()
    counts = net.decision_maker_counts(conn, ["acme widgets", "northwind", "nobody"])
    assert counts == {"acme widgets": 1, "northwind": 1}
    assert [r["full_name"] for r in net.contacts_for_key(conn, "acme widgets")] == [
        "A One",
        "B Two",
    ]

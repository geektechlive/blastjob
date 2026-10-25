"""The target list and its events (#85): bulk add, due selection, lease, event dedupe."""

from __future__ import annotations

import datetime as dt

import pytest

from blastjob.core import target_store as ts
from blastjob.core.company import company_key
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig

NOW = dt.datetime(2026, 10, 24, 12, 0, 0)


@pytest.fixture
def conn(tmp_path):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _event(span: str = "Acme Widgets named Dana Roe as its new chief executive officer.", **kw):
    base = {
        "event_type": "new_ceo",
        "source_url": "https://news.example.com/a",
        "page_title": "Acme names a CEO",
        "source_span": span,
        "published_on": "2026-10-20",
        "source_basis": "news_report",
        "identity_basis": "located_mention",
    }
    return ts.NewEvent(**{**base, **kw})


def test_parse_bulk_reads_name_domain_and_state():
    parsed = ts.parse_bulk("Acme Widgets | acme.example.com\nNorthwind Traders | | wi\n\n")
    assert [(i.name, i.domain, i.state) for i in parsed.items] == [
        ("Acme Widgets", "acme.example.com", ""),
        ("Northwind Traders", "", "WI"),
    ]
    assert parsed.errors == ()


def test_parse_bulk_reports_a_malformed_domain_instead_of_dropping_it():
    parsed = ts.parse_bulk("Acme Widgets | https://acme.example.com/about\nContoso")
    assert [i.name for i in parsed.items] == ["Contoso"]
    assert len(parsed.errors) == 1 and "Acme Widgets" in parsed.errors[0]


def test_parse_bulk_rejects_an_ats_domain_and_a_bad_state():
    parsed = ts.parse_bulk("Acme | boards.greenhouse.io\nContoso | | Atlantis")
    assert parsed.items == ()
    assert len(parsed.errors) == 2


def test_parse_bulk_caps_the_number_of_lines():
    text = "\n".join(f"Company {i}" for i in range(ts.MAX_BULK_LINES + 5))
    parsed = ts.parse_bulk(text)
    assert len(parsed.items) == ts.MAX_BULK_LINES
    assert any("first" in e for e in parsed.errors)


def test_add_targets_dedupes_by_company_key(conn):
    items = ts.parse_bulk("Acme Widgets, Inc.\nACME WIDGETS\nNorthwind").items
    first = ts.add_targets(conn, items, now=NOW)
    again = ts.add_targets(conn, ts.parse_bulk("acme widgets llc").items, now=NOW)
    assert len(first.added) == 2 and len(first.existing) == 1
    assert again.added == () and len(again.existing) == 1
    assert len(ts.list_targets(conn)) == 2


def test_add_targets_rejects_a_name_with_no_key(conn):
    result = ts.add_targets(conn, [ts.TargetInput(name="!!!")], now=NOW)
    assert result.added == () and result.rejected


def test_remove_hides_events_and_readding_shows_them_again(conn):
    ts.add_targets(conn, [ts.TargetInput("Acme Widgets")], now=NOW)
    key = ts.list_targets(conn)[0]["company_key"]
    ts.record_scan(conn, key, ok=True, status="ok", events=[_event()], now=NOW)
    assert len(ts.events_for(conn, key)) == 1
    assert ts.remove_target(conn, key) is True
    assert ts.events_for(conn, key) == []
    assert conn.execute("SELECT COUNT(*) FROM company_events").fetchone()[0] == 1
    ts.add_targets(conn, [ts.TargetInput("Acme Widgets")], now=NOW)
    assert len(ts.events_for(conn, key)) == 1


def test_set_tier_only_accepts_known_tiers(conn):
    ts.add_targets(conn, [ts.TargetInput("Acme Widgets")], now=NOW)
    key = ts.list_targets(conn)[0]["company_key"]
    assert ts.set_tier(conn, key, "dream") is True
    assert ts.get_target(conn, key)["tier"] == "dream"
    assert ts.set_tier(conn, key, "bogus") is False
    assert ts.set_tier(conn, "nobody", "core") is False


def test_canonical_url_drops_tracking_and_fragment_and_lowercases_host():
    url = "https://News.Example.com/a/b?id=7&utm_source=x&fbclid=y#top"
    assert ts.canonical_url(url) == "https://news.example.com/a/b?id=7"


def test_fingerprint_ignores_whitespace_and_case_but_not_type():
    a = ts.fingerprint("acme", "new_ceo", "Named  Dana Roe\nCEO")
    assert a == ts.fingerprint("acme", "new_ceo", "named dana roe ceo")
    assert a != ts.fingerprint("acme", "new_cfo", "named dana roe ceo")


def test_record_scan_dedupes_across_runs_and_allows_two_events_from_one_page(conn):
    ts.add_targets(conn, [ts.TargetInput("Acme Widgets")], now=NOW)
    key = ts.list_targets(conn)[0]["company_key"]
    ceo = _event()
    cfo = _event(
        span="Acme Widgets also hired Lee Park as chief financial officer.", event_type="new_cfo"
    )
    first = ts.record_scan(conn, key, ok=True, status="ok", events=[ceo, cfo], now=NOW)
    second = ts.record_scan(conn, key, ok=True, status="ok", events=[ceo, cfo], now=NOW)
    assert (first, second) == (2, 0)
    assert len(ts.events_for(conn, key)) == 2


def test_failed_scan_keeps_last_ok_and_counts_failures(conn):
    ts.add_targets(conn, [ts.TargetInput("Acme Widgets")], now=NOW)
    key = ts.list_targets(conn)[0]["company_key"]
    ts.record_scan(conn, key, ok=True, status="ok", events=[], now=NOW)
    later = NOW + dt.timedelta(days=8)
    ts.record_scan(conn, key, ok=False, status="failed: search error", events=[], now=later)
    row = ts.get_target(conn, key)
    assert row["last_scan_ok_at"] == NOW.isoformat(timespec="seconds")
    assert row["last_scan_at"] == later.isoformat(timespec="seconds")
    assert row["last_scan_status"] == "failed: search error"
    assert row["scan_failures"] == 1
    ts.record_scan(conn, key, ok=True, status="ok", events=[], now=later)
    assert ts.get_target(conn, key)["scan_failures"] == 0


def test_dismiss_hides_an_event(conn):
    ts.add_targets(conn, [ts.TargetInput("Acme Widgets")], now=NOW)
    key = ts.list_targets(conn)[0]["company_key"]
    ts.record_scan(conn, key, ok=True, status="ok", events=[_event()], now=NOW)
    event_id = ts.events_for(conn, key)[0]["id"]
    assert ts.dismiss_event(conn, event_id, now=NOW) is True
    assert ts.events_for(conn, key) == []
    assert len(ts.events_for(conn, key, include_dismissed=True)) == 1
    assert ts.unnotified_events(conn) == []


def test_due_targets_only_core_older_than_seven_days_or_never_scanned(conn):
    ts.add_targets(conn, [ts.TargetInput("Fresh Co"), ts.TargetInput("Stale Co")], now=NOW)
    ts.add_targets(conn, [ts.TargetInput("Dream Co")], tier="dream", now=NOW)
    ts.add_targets(conn, [ts.TargetInput("New Co")], now=NOW)
    ts.record_scan(conn, "fresh", ok=True, status="ok", events=[], now=NOW - dt.timedelta(days=3))
    ts.record_scan(conn, "stale", ok=True, status="ok", events=[], now=NOW - dt.timedelta(days=8))
    due = [r["company_key"] for r in ts.due_targets(conn, now=NOW)]
    assert sorted(due) == ["new", "stale"]


def test_due_targets_rotate_least_recently_attempted_first_and_respect_the_cap(conn):
    names = [f"Alder {w}" for w in ("Aa", "Bb", "Cc", "Dd", "Ee", "Ff", "Gg", "Hh", "Ii", "Jj")]
    keys = [company_key(n) for n in names]
    ts.add_targets(conn, [ts.TargetInput(n) for n in names], now=NOW)
    for i, key in enumerate(keys[:4]):
        ts.record_scan(
            conn, key, ok=True, status="ok", events=[], now=NOW - dt.timedelta(days=30 - i)
        )
    due = [r["company_key"] for r in ts.due_targets(conn, now=NOW)]
    assert len(due) == ts.MAX_COMPANIES_PER_RUN == 8
    # never-scanned rows go before any scanned row, then the oldest attempt first
    assert sorted(due[:6]) == sorted(keys[4:])
    assert due[6:] == keys[:2]


def test_failed_company_backs_off_one_then_three_then_seven_days(conn):
    ts.add_targets(conn, [ts.TargetInput("Acme Widgets")], now=NOW)
    key = "acme widgets"

    def due_after(days: float) -> bool:
        return bool(ts.due_targets(conn, now=NOW + dt.timedelta(days=days)))

    ts.record_scan(conn, key, ok=False, status="failed: x", events=[], now=NOW)
    assert not due_after(0.5) and due_after(1.01)
    ts.record_scan(conn, key, ok=False, status="failed: x", events=[], now=NOW)
    assert not due_after(2.9) and due_after(3.01)
    ts.record_scan(conn, key, ok=False, status="failed: x", events=[], now=NOW)
    assert not due_after(6.9) and due_after(7.01)
    ts.record_scan(conn, key, ok=False, status="failed: x", events=[], now=NOW)
    assert not due_after(6.9) and due_after(7.01)


def test_claim_is_exclusive_until_the_lease_expires(conn):
    ts.add_targets(conn, [ts.TargetInput("Acme Widgets")], now=NOW)
    assert ts.claim_scan(conn, "acme widgets", now=NOW) is True
    assert ts.claim_scan(conn, "acme widgets", now=NOW + dt.timedelta(minutes=10)) is False
    assert ts.due_targets(conn, now=NOW + dt.timedelta(minutes=10)) == []
    assert ts.claim_scan(conn, "acme widgets", now=NOW + dt.timedelta(minutes=31)) is True


def test_record_scan_releases_the_claim(conn):
    ts.add_targets(conn, [ts.TargetInput("Acme Widgets")], now=NOW)
    ts.claim_scan(conn, "acme widgets", now=NOW)
    ts.record_scan(conn, "acme widgets", ok=True, status="ok", events=[], now=NOW)
    assert ts.get_target(conn, "acme widgets")["scan_claimed_at"] == ""


def test_unnotified_events_and_mark_notified(conn):
    ts.add_targets(conn, [ts.TargetInput("Acme Widgets")], now=NOW)
    ts.record_scan(conn, "acme widgets", ok=True, status="ok", events=[_event()], now=NOW)
    rows = ts.unnotified_events(conn)
    assert len(rows) == 1 and rows[0]["company"] == "Acme Widgets"
    ts.mark_notified(conn, [rows[0]["id"]], now=NOW)
    assert ts.unnotified_events(conn) == []

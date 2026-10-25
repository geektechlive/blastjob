"""The targets screen (#85): list, bulk add, tier, remove, dismiss and the per-company page."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from blastjob.core import company as co
from blastjob.core import target_store as ts
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

NOW = dt.datetime(2026, 10, 24, 12, 0, 0)
SPAN = "named Dana Roe as its new chief executive officer"


@pytest.fixture
def env(tmp_path):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    conn = dbc.init_db(cfg)
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))
    yield client, conn
    conn.close()


def _tok() -> dict[str, str]:
    return {CSRF_HEADER: LAUNCH_TOKEN}


def _event(url="https://news.example.com/a", span=SPAN, published="2026-10-20", **kw):
    base = {
        "event_type": "new_ceo",
        "source_url": url,
        "page_title": "Acme names a CEO",
        "source_span": span,
        "published_on": published,
        "source_basis": "news_report",
        "identity_basis": "located_mention",
    }
    return ts.NewEvent(**{**base, **kw})


def _target(conn, name="Acme Widgets", events=(), ok=True, status="ok"):
    ts.add_targets(conn, [ts.TargetInput(name)], now=NOW)
    key = co.company_key(name)
    ts.record_scan(conn, key, ok=ok, status=status, events=list(events), now=NOW)
    return key


def test_empty_page_renders_with_the_add_form(env):
    client, _ = env
    r = client.get("/targets")
    assert r.status_code == 200
    assert "Add target companies" in r.text and 'name="_token"' in r.text


def test_nav_links_to_the_targets_page(env):
    client, _ = env
    assert 'href="/targets"' in client.get("/network").text


def test_add_rejects_a_missing_token_and_a_foreign_origin(env):
    client, conn = env
    assert client.post("/targets/add", data={"companies": "Acme"}).status_code == 403
    bad = client.post(
        "/targets/add",
        data={"companies": "Acme"},
        headers={**_tok(), "origin": "https://evil.example.com"},
    )
    assert bad.status_code == 403
    assert ts.list_targets(conn) == []


def test_every_post_route_checks_the_token(env):
    client, conn = env
    key = _target(conn, events=[_event()])
    event_id = ts.events_for(conn, key)[0]["id"]
    for path in (f"/targets/{key}/remove", f"/targets/{key}/tier", f"/events/{event_id}/dismiss"):
        assert client.post(path, data={"tier": "dream"}).status_code == 403
    assert ts.get_target(conn, key) is not None


def test_bulk_add_dedupes_and_reports_bad_lines(env):
    client, conn = env
    text = "Acme Widgets | acme.example.com\nacme widgets inc\nContoso | https://bad.example/x\n"
    r = client.post("/targets/add", data={"companies": text, "tier": "core"}, headers=_tok())
    assert r.status_code == 200
    assert "Added 1" in r.text and "already on the list" in r.text
    assert "Contoso" in r.text and "bare hostname" in r.text
    assert [t["company"] for t in ts.list_targets(conn)] == ["Acme Widgets"]
    assert ts.list_targets(conn)[0]["domain"] == "acme.example.com"


def test_bulk_add_takes_the_chosen_tier_and_refuses_an_unknown_one(env):
    client, conn = env
    client.post("/targets/add", data={"companies": "Northwind", "tier": "dream"}, headers=_tok())
    assert ts.list_targets(conn)[0]["tier"] == "dream"
    r = client.post("/targets/add", data={"companies": "Fabrikam", "tier": "vip"}, headers=_tok())
    assert r.status_code == 400


def test_list_shows_events_best_first_with_basis_source_and_span(env):
    client, conn = env
    _target(conn, "Contoso Foods", [_event(published="2026-10-01")])
    fabrikam = _event(
        url="https://fabrikam.example.org/n",
        published="2026-10-22",
        source_basis="employer_site",
        identity_basis="domain",
    )
    _target(conn, "Fabrikam", [fabrikam])
    _target(conn, "Zed Nothing")
    text = client.get("/targets").text
    assert text.index("Fabrikam") < text.index("Contoso Foods") < text.index("Zed Nothing")
    assert "published 2026-10-22" in text and "employer&#39;s own site" in text
    assert "news report" in text and "fabrikam.example.org" in text
    assert SPAN in text and "New CEO" in text
    assert "confirmed" not in text.lower()


def test_scan_states_are_never_confused_with_no_news(env):
    client, conn = env
    ts.add_targets(conn, [ts.TargetInput("Never Scanned")], now=NOW)
    _target(conn, "Clean Co", ok=True)
    _target(conn, "Broken Co", ok=False, status="failed: search error (RuntimeError)")
    text = client.get("/targets").text
    assert "Not scanned yet" in text
    assert "No news found" in text
    assert "Scan failed" in text and "search error (RuntimeError)" in text


def test_warm_path_counts_come_from_the_network_join(env):
    client, conn = env
    _target(conn, "Acme Widgets", [_event()])
    for name, dm in (("Pat Lane", 1), ("Sam Fox", 0)):
        conn.execute(
            "INSERT INTO network_contacts (full_name, company, company_key, enrich_decision_maker) "
            "VALUES (?,?,?,?)",
            (name, "Acme Widgets", "acme widgets", dm),
        )
    conn.commit()
    text = client.get("/targets").text
    assert "2 contacts" in text and "1 decision maker" in text


def test_a_non_http_event_link_is_not_rendered_as_a_link(env):
    client, conn = env
    _target(conn, "Acme Widgets", [_event(url="javascript:alert(1)")])
    text = client.get("/targets").text
    assert "javascript:" not in text


def test_span_text_is_escaped(env):
    client, conn = env
    _target(conn, "Acme Widgets", [_event(span="<script>alert(1)</script> named Dana Roe as CEO")])
    text = client.get("/targets").text
    assert "<script>alert(1)" not in text


def test_dismiss_removes_the_event_from_view(env):
    client, conn = env
    key = _target(conn, events=[_event()])
    event_id = ts.events_for(conn, key)[0]["id"]
    r = client.post(f"/events/{event_id}/dismiss", headers=_tok())
    assert r.status_code == 200
    assert SPAN not in r.text
    assert ts.events_for(conn, key) == []
    assert client.post("/events/9999/dismiss", headers=_tok()).status_code == 404


def test_dismiss_only_redirects_to_our_own_pages(env):
    client, conn = env
    key = _target(conn, events=[_event()])
    event_id = ts.events_for(conn, key)[0]["id"]
    r = client.post(
        f"/events/{event_id}/dismiss",
        data={"back": "https://evil.example.com/"},
        headers=_tok(),
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/targets"


def test_tier_change_and_remove(env):
    client, conn = env
    key = _target(conn)
    assert (
        client.post(f"/targets/{key}/tier", data={"tier": "bogus"}, headers=_tok()).status_code
        == 400
    )
    client.post(f"/targets/{key}/tier", data={"tier": "dream"}, headers=_tok())
    assert ts.get_target(conn, key)["tier"] == "dream"
    client.post(f"/targets/{key}/remove", headers=_tok())
    assert ts.get_target(conn, key) is None
    assert client.post("/targets/nobody/remove", headers=_tok()).status_code == 404


def test_removed_target_hides_events_and_readding_restores_them(env):
    client, conn = env
    key = _target(conn, events=[_event()])
    client.post(f"/targets/{key}/remove", headers=_tok())
    assert SPAN not in client.get("/targets").text
    client.post("/targets/add", data={"companies": "Acme Widgets"}, headers=_tok())
    assert SPAN in client.get("/targets").text


def test_target_page_works_without_a_company_profile_and_lists_contacts(env):
    client, conn = env
    key = _target(conn, "Acme Widgets", [_event()])
    conn.execute(
        "INSERT INTO network_contacts (full_name, company, company_key, position, url, "
        "enrich_decision_maker) VALUES ('Pat Lane','Acme Widgets',?,'VP Operations',"
        "'https://www.example.com/in/pat-lane',1)",
        (key,),
    )
    conn.commit()
    assert co.get_profile_by_key(conn, key) is None
    r = client.get(f"/targets/{key}")
    assert r.status_code == 200
    assert "Acme Widgets" in r.text and SPAN in r.text
    assert "Pat Lane" in r.text and "VP Operations" in r.text and "decision maker" in r.text
    assert "https://www.example.com/in/pat-lane" in r.text


def test_target_page_is_404_for_a_company_that_is_not_a_target(env):
    client, _ = env
    assert client.get("/targets/nobody").status_code == 404


def test_company_page_links_to_the_target_page_when_it_is_a_target(env):
    client, conn = env
    key = _target(conn, "Acme Widgets")
    co.upsert_profile(conn, co.CompanyProfile(company="Acme Widgets"))
    assert f"/targets/{key.replace(' ', '%20')}" in client.get(f"/companies/{key}").text
    other = co.CompanyProfile(company="Northwind Traders")
    co.upsert_profile(conn, other)
    assert "/targets/" not in client.get("/companies/northwind%20traders").text


def test_dismiss_back_rejects_a_trailing_newline(env):
    client, conn = env
    key = _target(conn, events=[_event()])
    event_id = ts.events_for(conn, key)[0]["id"]
    r = client.post(
        f"/events/{event_id}/dismiss",
        data={"back": "/targets\n"},
        headers=_tok(),
        follow_redirects=False,
    )
    assert r.headers["location"] == "/targets"
    assert "\n" not in r.headers["location"]

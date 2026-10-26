"""Recruiter desk status, last touch and the job-less note routes (#98, ADR 0062)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from blastjob.db import client as dbc
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN
from tests.asks_fixture import add_network, add_recruiter, make_cfg

H = {CSRF_HEADER: LAUNCH_TOKEN}


@pytest.fixture
def world():
    cfg = make_cfg()
    conn = dbc.init_db(cfg)
    rid = add_recruiter(conn, "Ria Fox", messages=2, last_message_at="2026-03-01T09:00:00")
    plain = add_network(conn, "Ann Lee")
    conn.close()
    return cfg, rid, plain


def _client(cfg) -> TestClient:
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))


def _rows(cfg, sql: str):
    conn = dbc.init_db(cfg)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_desk_shows_status_and_the_last_touch_source(world):
    cfg, rid, _ = world
    r = _client(cfg).get("/network/recruiters")
    assert r.status_code == 200
    assert "spoke" in r.text and "2026-03-01" in r.text and "LinkedIn message" in r.text


def test_desk_shows_an_ask_as_the_last_touch_source(world):
    cfg, rid, _ = world
    c = _client(cfg)
    c.post(
        f"/network/contacts/{rid}/asks",
        data={"channel": "email", "asked_on": "2026-10-20"},
        headers=H,
    )
    text = c.get("/network/recruiters").text
    assert "2026-10-20" in text and ">ask<" in text


def test_status_post_without_csrf_is_403(world):
    cfg, rid, _ = world
    r = _client(cfg).post(f"/network/contacts/{rid}/recruiter-status", data={"status": "active"})
    assert r.status_code == 403
    assert _rows(cfg, "SELECT recruiter_status FROM network_contacts WHERE id=1")[0][0] is None


def test_status_post_for_a_non_recruiter_is_404(world):
    cfg, _, plain = world
    r = _client(cfg).post(
        f"/network/contacts/{plain}/recruiter-status", data={"status": "active"}, headers=H
    )
    assert r.status_code == 404
    assert (
        _client(cfg)
        .post("/network/contacts/999/recruiter-status", data={"status": "active"}, headers=H)
        .status_code
        == 404
    )


def test_status_post_sets_and_auto_clears(world):
    cfg, rid, _ = world
    c = _client(cfg)
    r = c.post(f"/network/contacts/{rid}/recruiter-status", data={"status": "active"}, headers=H)
    assert r.status_code == 200 and f'id="recruiter-{rid}"' in r.text
    assert _rows(cfg, f"SELECT recruiter_status FROM network_contacts WHERE id={rid}")[0][0] == (
        "active"
    )
    c.post(f"/network/contacts/{rid}/recruiter-status", data={"status": "auto"}, headers=H)
    assert _rows(cfg, f"SELECT recruiter_status FROM network_contacts WHERE id={rid}")[0][0] is None


def test_do_not_contact_needs_a_reason_over_http(world):
    cfg, rid, _ = world
    c = _client(cfg)
    bad = c.post(
        f"/network/contacts/{rid}/recruiter-status", data={"status": "do_not_contact"}, headers=H
    )
    assert bad.status_code == 400
    long = c.post(
        f"/network/contacts/{rid}/recruiter-status",
        data={"status": "do_not_contact", "reason": "x" * 121},
        headers=H,
    )
    assert long.status_code == 400
    bogus = c.post(f"/network/contacts/{rid}/recruiter-status", data={"status": "zzz"}, headers=H)
    assert bogus.status_code == 400


def test_the_reason_is_escaped(world):
    cfg, rid, _ = world
    c = _client(cfg)
    r = c.post(
        f"/network/contacts/{rid}/recruiter-status",
        data={"status": "do_not_contact", "reason": "<script>alert(1)</script>"},
        headers=H,
    )
    assert r.status_code == 200
    assert "<script>alert(1)" not in r.text and "&lt;script&gt;" in r.text
    assert "<script>alert(1)" not in c.get("/network/recruiters").text


def test_note_post_logs_a_jobless_recruiter_ask(world):
    cfg, rid, _ = world
    r = _client(cfg).post(
        f"/network/contacts/{rid}/asks",
        data={
            "channel": "phone",
            "asked_on": "2026-10-20",
            "note": "<b>called</b>",
            "request_token": "c3" * 16,
        },
        headers=H,
    )
    assert r.status_code == 200 and f'id="recruiter-{rid}"' in r.text
    assert "<b>called" not in r.text and "&lt;b&gt;called" in r.text
    [row] = _rows(cfg, "SELECT job_id, kind, channel, network_contact_id FROM outreach_asks")
    assert row["job_id"] is None and row["kind"] == "recruiter" and row["channel"] == "phone"
    assert row["network_contact_id"] == rid


def test_note_post_guards(world):
    cfg, rid, plain = world
    c = _client(cfg)
    form = {"channel": "email", "asked_on": "2026-10-20"}
    assert c.post(f"/network/contacts/{rid}/asks", data=form).status_code == 403
    assert c.post(f"/network/contacts/{plain}/asks", data=form, headers=H).status_code == 404
    future = {**form, "asked_on": "2999-01-01"}
    assert c.post(f"/network/contacts/{rid}/asks", data=future, headers=H).status_code == 400
    assert _rows(cfg, "SELECT COUNT(*) FROM outreach_asks")[0][0] == 0


def test_double_submit_of_a_note_is_one_row(world):
    cfg, rid, _ = world
    c = _client(cfg)
    data = {"channel": "email", "asked_on": "2026-10-20", "request_token": "d4" * 16}
    assert c.post(f"/network/contacts/{rid}/asks", data=data, headers=H).status_code == 200
    assert c.post(f"/network/contacts/{rid}/asks", data=data, headers=H).status_code == 200
    assert _rows(cfg, "SELECT COUNT(*) FROM outreach_asks")[0][0] == 1


def test_updating_a_jobless_recruiter_ask_returns_the_desk_row(world):
    cfg, rid, _ = world
    c = _client(cfg)
    c.post(
        f"/network/contacts/{rid}/asks",
        data={"channel": "email", "asked_on": "2026-10-20"},
        headers=H,
    )
    ask = _rows(cfg, "SELECT id FROM outreach_asks")[0][0]
    body = {"status": "replied", "follow_up_on": "", "note": "yes"}
    r = c.post(f"/asks/{ask}", data=body, headers=H)
    assert r.status_code == 200 and f'id="recruiter-{rid}"' in r.text and "replied" in r.text


def test_updating_a_jobless_advice_ask_to_a_non_recruiter_still_says_saved(world):
    cfg, _, plain = world
    conn = dbc.init_db(cfg)
    from blastjob.core import outreach_asks as oa

    ask = oa.log_ask(
        conn,
        job_id=None,
        network_contact_id=plain,
        contact_id=None,
        kind="advice",
        channel="email",
        asked_on="2026-10-20",
    )
    conn.close()
    r = _client(cfg).post(
        f"/asks/{ask}", data={"status": "replied", "follow_up_on": "", "note": ""}, headers=H
    )
    assert r.status_code == 200 and r.text == "Saved."

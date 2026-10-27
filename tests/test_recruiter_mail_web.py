"""The Recruiter mail page and its decisions (#83): escaping, badges, CSRF, compare-and-set,
one timeline event per link, and a decision that rolls back with its event."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from blastjob.core import events
from blastjob.core import mail_triage as mt
from blastjob.db import client as dbc
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN
from tests.asks_fixture import add_job, add_recruiter
from tests.label_fixture import insert_triage, make_cfg

H = {CSRF_HEADER: LAUNCH_TOKEN}
EVIL = '<a href="http://evil.example.net/x">click</a><script>alert(1)</script>'


@pytest.fixture
def env():
    cfg = make_cfg()
    conn = dbc.init_db(cfg)
    with TestClient(create_app(load_config=lambda: cfg.model_copy(deep=True))) as client:
        yield client, conn, cfg
    conn.close()


def _decide(client, tid, version, action, target="", **extra):
    data = {"version": str(version), "action": action, "target_id": str(target), **extra}
    return client.post(f"/recruiter-mail/{tid}/decide", data=data, headers=H)


def _row(conn, tid):
    return conn.execute("SELECT * FROM mail_triage WHERE id=?", (tid,)).fetchone()


def _label_events(conn, job_id):
    return conn.execute(
        "SELECT * FROM application_events WHERE job_id=? AND source='mail_label'", (job_id,)
    ).fetchall()


# --- the page ------------------------------------------------------------------------------


def test_page_escapes_everything_from_the_mail_and_renders_no_link_from_it(env):
    client, conn, _ = env
    insert_triage(conn, sender_name=EVIL, subject=EVIL, snippet=EVIL)
    html = client.get("/recruiter-mail").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert 'href="http://evil' not in html and '<a href="http' not in html


def test_page_shows_new_before_decided_and_the_sender_marks(env):
    client, conn, _ = env
    jid = add_job(conn, "j1")
    insert_triage(conn, 1, subject="Decided one", sender_address="a@firm.example.com")
    new_id = insert_triage(conn, 2, subject="Fresh one", sender_address="b@firm.example.com")
    first = _row(conn, 1)
    mt.decide(conn, int(first["id"]), version=0, action="dismiss")
    html = client.get("/recruiter-mail").text
    assert html.index("Fresh one") < html.index("Decided one")
    assert new_id and jid


def test_known_badge_needs_a_passed_dmarc_and_matches_exactly_ignoring_blank(env):
    client, conn, _ = env
    rid = add_recruiter(conn, "Ria Fox")
    conn.execute("UPDATE network_contacts SET email='Ria@Firm.Example.com' WHERE id=?", (rid,))
    add_recruiter(conn, "Blank Email")
    conn.commit()
    insert_triage(conn, 1, sender_address="ria@firm.example.com", subject="from known")
    insert_triage(conn, 2, sender_address="ria@firm.example.com.evil.example.net", subject="x")
    insert_triage(conn, 3, sender_address="", subject="no address")
    html = client.get("/recruiter-mail").text
    assert html.count("Known sender") == 1
    assert html.count("New sender") == 2
    assert "Address matches a contact" not in html


def test_a_matching_address_without_a_passed_dmarc_is_only_a_neutral_match(env):
    client, conn, _ = env
    rid = add_recruiter(conn, "Ria Fox")
    conn.execute("UPDATE network_contacts SET email='ria@firm.example.com' WHERE id=?", (rid,))
    conn.commit()
    insert_triage(conn, 1, sender_address="ria@firm.example.com", auth_dmarc="none")
    insert_triage(conn, 2, sender_address="ria@firm.example.com", auth_dmarc="fail")
    html = client.get("/recruiter-mail").text
    assert html.count("Address matches a contact") == 2
    assert "Known sender" not in html


def test_automated_sender_is_marked_but_still_shown(env):
    client, conn, _ = env
    insert_triage(conn, 1, sender_address="no-reply@firm.example.com", subject="Auto one")
    html = client.get("/recruiter-mail").text
    assert "Auto one" in html and "Automated" in html


@pytest.mark.parametrize(
    "auth, wording",
    [
        (("pass", "pass", "pass"), "DMARC passed"),
        (("pass", "pass", "fail"), "Sender checks failed"),
        (("fail", "pass", "none"), "Sender checks failed"),
        (("unknown", "unknown", "unknown"), "No DMARC result"),
        (("pass", "pass", "none"), "No DMARC result"),
    ],
)
def test_badge_wording(env, auth, wording):
    client, conn, _ = env
    spf, dkim, dmarc = auth
    insert_triage(conn, 1, auth_spf=spf, auth_dkim=dkim, auth_dmarc=dmarc)
    html = client.get("/recruiter-mail").text
    assert wording in html
    assert "sending domain" in html


def test_page_with_the_label_off_says_so(env):
    client, _, cfg = env
    cfg.discovery.recruiter_label = ""
    assert "Settings" in client.get("/recruiter-mail").text


def test_job_choices_are_kept_jobs_best_fit_first(env):
    client, conn, _ = env
    add_job(conn, "lo", company="Lowco", score=60)
    add_job(conn, "hi", company="Hico", score=95)
    insert_triage(conn, 1)
    html = client.get("/recruiter-mail").text
    assert html.index("Hico") < html.index("Lowco")


def test_contact_choices_put_an_email_match_first(env):
    client, conn, _ = env
    add_recruiter(conn, "Zed Other", messages=50, last_message_at="2026-10-01")
    match = add_recruiter(conn, "Ria Fox", messages=1)
    conn.execute("UPDATE network_contacts SET email='ria@firm.example.com' WHERE id=?", (match,))
    conn.commit()
    insert_triage(conn, 1, sender_address="ria@firm.example.com")
    html = client.get("/recruiter-mail").text
    assert html.index("Ria Fox") < html.index("Zed Other")


# --- decisions ---------------------------------------------------------------------------------


def test_decision_without_csrf_is_forbidden(env):
    client, conn, _ = env
    tid = insert_triage(conn, 1)
    r = client.post(f"/recruiter-mail/{tid}/decide", data={"version": "0", "action": "dismiss"})
    assert r.status_code == 403 and _row(conn, tid)["status"] == "new"


def test_dismiss_marks_the_row_and_bumps_the_version(env):
    client, conn, _ = env
    tid = insert_triage(conn, 1)
    assert _decide(client, tid, 0, "dismiss").status_code == 200
    row = _row(conn, tid)
    assert row["status"] == "dismissed" and row["version"] == 1 and row["decided_at"]


def test_link_to_a_job_records_one_event_even_when_repeated(env):
    client, conn, _ = env
    jid = add_job(conn, "j1")
    tid = insert_triage(conn, 1)
    assert _decide(client, tid, 0, "job", jid).status_code == 200
    # Same job again as an explicit change: still one event.
    assert _decide(client, tid, 1, "job", jid, change="1").status_code == 200
    (ev,) = _label_events(conn, jid)
    assert ev["kind"] == "recruiter_reply" and ev["ref"] == "label:7:1"
    assert ev["at"].startswith("2026-10-26T10:00:00")
    assert _row(conn, tid)["job_id"] == jid


def test_changing_the_linked_job_moves_the_event(env):
    client, conn, _ = env
    a, b = add_job(conn, "a"), add_job(conn, "b")
    tid = insert_triage(conn, 1)
    _decide(client, tid, 0, "job", a)
    assert _decide(client, tid, 1, "job", b, change="1").status_code == 200
    assert _label_events(conn, a) == [] and len(_label_events(conn, b)) == 1


def test_link_to_a_contact_clears_the_job_and_removes_its_event(env):
    client, conn, _ = env
    jid = add_job(conn, "j1")
    rid = add_recruiter(conn, "Ria Fox")
    tid = insert_triage(conn, 1)
    _decide(client, tid, 0, "job", jid)
    assert _decide(client, tid, 1, "contact", rid, change="1").status_code == 200
    row = _row(conn, tid)
    assert row["job_id"] is None and row["network_contact_id"] == rid
    assert row["status"] == "linked" and _label_events(conn, jid) == []


def test_dismissing_a_linked_row_removes_its_event(env):
    client, conn, _ = env
    jid = add_job(conn, "j1")
    tid = insert_triage(conn, 1)
    _decide(client, tid, 0, "job", jid)
    assert _decide(client, tid, 1, "dismiss", change="1").status_code == 200
    assert _label_events(conn, jid) == []


def test_another_sources_event_on_the_job_is_untouched(env):
    client, conn, _ = env
    jid = add_job(conn, "j1")
    events.add_event(conn, jid, "recruiter_reply", "2026-10-20", ref="<x@y>", source="replies")
    tid = insert_triage(conn, 1)
    _decide(client, tid, 0, "job", jid)
    _decide(client, tid, 1, "dismiss", change="1")
    assert len(events.events_for(conn, jid)) == 1


def test_stale_version_is_409_and_changes_nothing(env):
    client, conn, _ = env
    a, b = add_job(conn, "a"), add_job(conn, "b")
    tid = insert_triage(conn, 1)
    assert _decide(client, tid, 0, "job", a).status_code == 200
    r = _decide(client, tid, 0, "job", b, change="1")
    assert r.status_code == 409
    assert _row(conn, tid)["job_id"] == a and _label_events(conn, b) == []


def test_two_changes_from_the_same_page_the_second_gets_409(env):
    client, conn, _ = env
    a, b, c = add_job(conn, "a"), add_job(conn, "b"), add_job(conn, "c")
    tid = insert_triage(conn, 1)
    _decide(client, tid, 0, "job", a)
    assert _decide(client, tid, 1, "job", b, change="1").status_code == 200
    assert _decide(client, tid, 1, "job", c, change="1").status_code == 409
    assert _row(conn, tid)["job_id"] == b


def test_deciding_a_decided_row_needs_the_explicit_change(env):
    client, conn, _ = env
    a = add_job(conn, "a")
    tid = insert_triage(conn, 1)
    _decide(client, tid, 0, "dismiss")
    assert _decide(client, tid, 1, "job", a).status_code == 409
    assert _row(conn, tid)["status"] == "dismissed"


@pytest.mark.parametrize(
    "action, target", [("job", "999"), ("job", ""), ("job", "x"), ("contact", "999"), ("nope", "")]
)
def test_bad_targets_are_refused(env, action, target):
    client, conn, _ = env
    tid = insert_triage(conn, 1)
    r = _decide(client, tid, 0, action, target)
    assert r.status_code in (400, 404) and _row(conn, tid)["status"] == "new"


def test_a_job_that_is_not_kept_is_refused(env):
    client, conn, _ = env
    jid = add_job(conn, "j1")
    conn.execute("UPDATE jobs SET review_state='archived' WHERE id=?", (jid,))
    conn.commit()
    tid = insert_triage(conn, 1)
    assert _decide(client, tid, 0, "job", jid).status_code == 400


def test_a_non_recruiter_contact_is_refused(env):
    client, conn, _ = env
    cur = conn.execute(
        "INSERT INTO network_contacts (full_name, company, company_key, position)"
        " VALUES ('Pat Plain', 'Acme', 'acme', 'Cook')"
    )
    conn.commit()
    tid = insert_triage(conn, 1)
    assert _decide(client, tid, 0, "contact", cur.lastrowid).status_code in (400, 404)


def test_unknown_triage_id_is_404(env):
    client, _, _ = env
    assert _decide(client, 999, 0, "dismiss").status_code == 404


def test_a_failed_event_insert_rolls_the_decision_back(env, monkeypatch):
    client, conn, _ = env
    jid = add_job(conn, "j1")
    tid = insert_triage(conn, 1)

    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(events, "insert_event", boom)
    with pytest.raises(RuntimeError):
        _decide(client, tid, 0, "job", jid)
    row = _row(conn, tid)
    assert row["status"] == "new" and row["version"] == 0 and row["job_id"] is None
    assert _label_events(conn, jid) == []


# --- the schema's own rails ----------------------------------------------------------------------


def test_a_row_cannot_hold_both_a_job_and_a_contact(env):
    _, conn, _ = env
    jid = add_job(conn, "j1")
    rid = add_recruiter(conn, "Ria Fox")
    with pytest.raises(sqlite3.IntegrityError):
        insert_triage(conn, 1, status="linked", job_id=jid, network_contact_id=rid)


def test_linked_requires_exactly_one_target_and_a_target_requires_linked(env):
    _, conn, _ = env
    jid = add_job(conn, "j1")
    with pytest.raises(sqlite3.IntegrityError):
        insert_triage(conn, 1, status="linked")
    with pytest.raises(sqlite3.IntegrityError):
        insert_triage(conn, 2, status="new", job_id=jid)


def test_identity_is_mailbox_uidvalidity_uid(env):
    _, conn, _ = env
    insert_triage(conn, 1)
    with pytest.raises(sqlite3.IntegrityError):
        insert_triage(conn, 1, message_id="<other@firm.example.com>")
    insert_triage(conn, 2, message_id="<t1@firm.example.com>")  # same Message-ID is allowed


@pytest.mark.parametrize("target", ["9" * 25, "1" * 400, "\u0661\u0662"])
def test_an_oversized_or_odd_target_id_is_a_400_not_a_500(env, target):
    client, conn, _ = env
    tid = insert_triage(conn, 1)
    r = _decide(client, tid, 0, "job", target)
    assert r.status_code == 400 and _row(conn, tid)["status"] == "new"


def test_an_estimated_receive_time_is_said_so(env):
    client, conn, _ = env
    insert_triage(conn, 1, received_estimated=1)
    assert "time not given by the server" in client.get("/recruiter-mail").text

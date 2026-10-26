"""Security-review fixes for recruiter submissions (#100): the acknowledgement is tied to the
submissions the page showed, our 409s carry a swap marker, and small input hardening."""

from __future__ import annotations

import re

from blastjob.core import submissions as subs
from blastjob.db import client as dbc
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN
from tests import test_submissions as _base
from tests.test_submissions import _client, _form, _job, _state, _sub, _world

# Fixtures re-exported so pytest finds them in this module.
cfg = _base.cfg
conn = _base.conn
_fixture_agencies = _base._fixture_agencies

H = {CSRF_HEADER: LAUNCH_TOKEN}
_ACK = re.compile(r'"submission_ack": "([0-9a-f]+)"')


def test_ack_is_tied_to_the_submissions_the_page_showed(cfg):
    jid = _world(cfg)
    c = _client(cfg)
    old = _ACK.search(c.get(f"/jobs/{jid}").text)
    assert old, "the page must carry an acknowledgement token"
    # A second submission is logged after the page rendered.
    r = c.post(f"/jobs/{jid}/submissions", data=_form(recruiter_name="Bo Two"), headers=H)
    assert r.status_code == 200
    stale = c.post(
        f"/jobs/{jid}/status",
        data={"status": "applied", "submission_ack": old.group(1)},
        headers=H,
    )
    assert stale.status_code == 409 and "Bo Two" in stale.text
    db = dbc.init_db(cfg)
    assert not _state(db, jid)["application_state"]
    db.close()
    fresh = _ACK.search(c.get(f"/jobs/{jid}").text)
    assert fresh and fresh.group(1) != old.group(1)
    ok = c.post(
        f"/jobs/{jid}/status",
        data={"status": "applied", "submission_ack": fresh.group(1)},
        headers=H,
    )
    assert ok.status_code == 200


def test_a_bare_one_is_no_longer_an_acknowledgement(cfg):
    jid = _world(cfg)
    r = _client(cfg).post(
        f"/jobs/{jid}/status", data={"status": "applied", "submission_ack": "1"}, headers=H
    )
    assert r.status_code == 409


def test_our_409s_carry_the_swap_marker_header(cfg):
    jid = _world(cfg)
    c = _client(cfg)
    a = c.post(f"/jobs/{jid}/status", data={"status": "applied"}, headers=H)
    b = c.post(f"/jobs/{jid}/stage/apply", data={"from_list": "1"}, headers=H)
    db = dbc.init_db(cfg)
    other = _job(db, application_state="applied", applied_at="2026-09-13")
    db.close()
    d = c.post(f"/jobs/{other}/submissions", data=_form(), headers=H)
    for resp in (a, b, d):
        assert resp.status_code == 409 and resp.headers.get("X-Submission-Warning") == "1"


def test_stage_409_body_escapes_the_recruiter_name(cfg):
    db = dbc.init_db(cfg)
    jid = _job(db)
    _sub(db, jid, name="<b>Zed</b>", firm="<i>F</i>")
    db.close()
    r = _client(cfg).post(f"/jobs/{jid}/stage/apply", data={"from_list": "1"}, headers=H)
    assert r.status_code == 409 and "<b>Zed" not in r.text and "&lt;b&gt;Zed" in r.text
    assert "<i>F" not in r.text


def test_non_ascii_digit_recruiter_id_is_400_not_500(cfg):
    db = dbc.init_db(cfg)
    jid = _job(db)
    db.close()
    r = _client(cfg).post(f"/jobs/{jid}/submissions", data=_form(network_contact_id="²"), headers=H)
    assert r.status_code == 400


def test_prior_check_and_insert_share_one_write_transaction(cfg, monkeypatch):
    db = dbc.init_db(cfg)
    jid = _job(db)
    db.close()
    seen: list[bool] = []
    real = subs.add_submission

    def spy(c, **kw):
        seen.append(c.in_transaction)
        return real(c, **kw)

    monkeypatch.setattr(subs, "add_submission", spy)
    r = _client(cfg).post(f"/jobs/{jid}/submissions", data=_form(), headers=H)
    assert r.status_code == 200 and seen == [True]

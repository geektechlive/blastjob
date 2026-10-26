"""Recruiter submissions (#100, ADR 0063): a recruiter put the owner forward for a role.

Pins: the same-role relation used here ignores application state (an unapplied repost still
counts), closes across clusters, and leaves `possible_same_role` alone; the first direct
application is refused on the server without the `submission_ack` token; logging after a
direct application needs "Log it anyway"; the recipient modes are exclusive. Offline; every
name is invented.
"""

from __future__ import annotations

import datetime as dt
import re
import tempfile

import pytest
from fastapi.testclient import TestClient

import tests.agency_fixture as agency_fixture
import tests.geo_fixture as geo_fixture
from blastjob.core import same_role as sr
from blastjob.core import submissions as subs
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN
from tests.asks_fixture import add_network, add_recruiter

H = {CSRF_HEADER: LAUNCH_TOKEN}
RADIUS = BlastJobConfig().discovery.commute_radius_miles
GEO = geo_fixture.region_cfg()
TITLE = "Vice President of Technology"
_seq = iter(range(1, 1_000_000))
TODAY = dt.date.today().isoformat()


@pytest.fixture(autouse=True)
def _fixture_agencies():
    agency_fixture.install_agencies()


@pytest.fixture
def cfg() -> BlastJobConfig:
    c = BlastJobConfig()
    geo_fixture.apply_region(c)
    agency_fixture.apply_agencies(c)
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _client(cfg) -> TestClient:
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda _c: None))


def _job(conn, *, company="Northwind Services", city="Fairhaven", title=TITLE, **cols) -> int:
    n = next(_seq)
    jid = dbc.upsert_job(
        conn,
        source=cols.pop("source", "adzuna"),
        external_id=f"s{n}",
        company=company,
        title=title,
        geo_city=city,
        geo_location_raw=f"{city}, OH",
    )
    cols.setdefault("first_seen", "2026-09-04T21:29:56")
    cols.setdefault("review_state", "kept")
    cols.setdefault("alignment_score", 85)
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), jid))
    conn.commit()
    return jid


def _cluster(conn, *ids: int) -> None:
    cid = conn.execute(
        "INSERT INTO job_clusters (fingerprint, created_at) VALUES ('t', '2026-09-01')"
    ).lastrowid
    for jid in ids:
        conn.execute("UPDATE jobs SET cluster_id=? WHERE id=?", (cid, jid))
    conn.commit()


def _sub(conn, job_id: int, name="Ravi Shah", firm="Northwind Search", on="2026-10-01") -> int:
    return subs.add_submission(conn, job_id=job_id, recruiter_name=name, firm=firm, submitted_on=on)


def _state(conn, job_id: int):
    return conn.execute(
        "SELECT application_state, applied_at FROM jobs WHERE id=?", (job_id,)
    ).fetchone()


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM recruiter_submissions").fetchone()[0]


# --- the role relation ----------------------------------------------------------------


def test_submission_on_the_job_itself_is_found(conn):
    a = _job(conn)
    _sub(conn, a)
    assert [s["job_id"] for s in subs.submissions_for_role(conn, a, RADIUS, GEO)] == [a]


def test_submission_on_a_cluster_sibling_is_found(conn):
    a, b = _job(conn, city="Fairhaven"), _job(conn, company="Swiftlane", city="Oakbrook")
    _cluster(conn, a, b)
    _sub(conn, b)
    assert [s["job_id"] for s in subs.submissions_for_role(conn, a, RADIUS, GEO)] == [b]


def test_submission_on_an_unapplied_same_employer_repost_in_another_cluster(conn):
    old = _job(conn, city="Fairhaven", first_seen="2026-06-01T00:00:00")
    repost = _job(conn, city="Oakbrook", first_seen="2026-10-01T00:00:00")
    _cluster(conn, old)
    _cluster(conn, repost)
    _sub(conn, old)
    assert sr.possible_same_role(conn, repost, RADIUS, GEO) == []  # nothing applied: no PREVIOUS
    found = subs.submissions_for_role(conn, repost, RADIUS, GEO)
    assert [s["job_id"] for s in found] == [old]


def test_agency_and_employer_postings_share_submissions(conn):
    employer = _job(conn)
    agency = _job(conn, company="Talonridge Global", city="West Fairhaven")
    _sub(conn, agency)
    assert len(subs.submissions_for_role(conn, employer, RADIUS, GEO)) == 1


def test_cross_cluster_suggestion_closure(conn):
    # E1 is far from E2, so they are not a pair; agency A is near E1 and clustered with E2.
    e1 = _job(conn, city="Fairhaven")
    e2 = _job(conn, city="Rivermont")
    a = _job(conn, company="Talonridge Global", city="Fairhaven")
    _cluster(conn, a, e2)
    _sub(conn, e2)
    assert e2 in subs.role_job_ids(conn, e1, RADIUS, GEO)
    assert [s["job_id"] for s in subs.submissions_for_role(conn, e1, RADIUS, GEO)] == [e2]


def test_unrelated_job_gets_nothing(conn):
    a = _job(conn)
    other = _job(conn, company="Brightwater Co", title="Chief Financial Officer")
    _sub(conn, a)
    assert subs.submissions_for_role(conn, other, RADIUS, GEO) == []


def test_a_closed_jobs_submission_still_counts(conn):
    a = _job(conn, application_state="posting_closed")
    b = _job(conn, city="Oakbrook")
    _cluster(conn, a, b)
    _sub(conn, a)
    assert len(subs.submissions_for_role(conn, b, RADIUS, GEO)) == 1


def test_submissions_are_newest_first(conn):
    a = _job(conn)
    first = _sub(conn, a, name="Ann One", on="2026-09-01")
    second = _sub(conn, a, name="Bo Two", on="2026-10-01")
    third = _sub(conn, a, name="Cy Three", on="2026-10-01")
    got = [s["id"] for s in subs.submissions_for_role(conn, a, RADIUS, GEO)]
    assert got == [third, second, first]


def test_possible_same_role_is_unchanged_by_the_split(conn):
    # An unapplied same-employer pair is still no PREVIOUS suggestion; an applied one still is.
    old = _job(conn, city="Fairhaven", application_state="applied", applied_at="2026-06-02")
    repost = _job(conn, city="Oakbrook", first_seen="2026-10-01T00:00:00")
    kinds = [c.kind for c in sr.possible_same_role(conn, repost, RADIUS, GEO)]
    assert kinds == [sr.PREVIOUS] and old


# --- validation (core) ----------------------------------------------------------------


def test_validation_future_date_overlong_fields_and_no_recipient(conn):
    a = _job(conn)
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    for kw in (
        {"recruiter_name": "Ann", "submitted_on": tomorrow},
        {"recruiter_name": "Ann", "submitted_on": "not-a-date"},
        {"recruiter_name": "x" * 81, "submitted_on": TODAY},
        {"recruiter_name": "Ann", "firm": "f" * 81, "submitted_on": TODAY},
        {"recruiter_name": "Ann", "note": "n" * 301, "submitted_on": TODAY},
        {"recruiter_name": "  ", "submitted_on": TODAY},
    ):
        with pytest.raises(subs.SubmissionError):
            subs.add_submission(conn, job_id=a, **kw)
    assert _count(conn) == 0


def test_non_recruiter_contact_is_refused(conn):
    a = _job(conn)
    person = add_network(conn, "Dana Fox")
    with pytest.raises(subs.SubmissionError):
        subs.add_submission(conn, job_id=a, network_contact_id=person, submitted_on=TODAY)


def test_contact_and_typed_name_together_are_refused_in_core(conn):
    a = _job(conn)
    rec = add_recruiter(conn, "Ravi Shah")
    with pytest.raises(subs.SubmissionError):
        subs.add_submission(
            conn, job_id=a, network_contact_id=rec, recruiter_name="Other", submitted_on=TODAY
        )


# --- first-apply warning text ---------------------------------------------------------


def test_first_apply_confirm_names_the_recruiter_and_only_before_applying(conn):
    a = _job(conn)
    _sub(conn, a, name="Ravi Shah", firm="Northwind Search", on="2026-10-01")
    text = subs.first_apply_confirm(conn, a, RADIUS, GEO)
    assert "Ravi Shah (Northwind Search) submitted you to this role on 2026-10-01" in text
    assert "Apply anyway?" in text
    conn.execute("UPDATE jobs SET application_state='applied', applied_at='2026-10-05'")
    conn.commit()
    assert subs.first_apply_confirm(conn, a, RADIUS, GEO) == ""


# --- both apply routes refuse without the acknowledgement ------------------------------


def _tok(cfg, jid: int) -> str:
    db = dbc.init_db(cfg)
    try:
        return subs.first_apply_warning(db, jid, RADIUS, GEO)[1]
    finally:
        db.close()


def _world(cfg):
    conn = dbc.init_db(cfg)
    jid = _job(conn)
    _sub(conn, jid)
    conn.close()
    return jid


def test_status_route_refuses_without_ack_and_accepts_with_it(cfg):
    jid = _world(cfg)
    c = _client(cfg)
    r = c.post(f"/jobs/{jid}/status", data={"status": "applied"}, headers=H)
    assert r.status_code == 409 and "Ravi Shah" in r.text
    conn = dbc.init_db(cfg)
    assert not _state(conn, jid)["application_state"]
    r = c.post(
        f"/jobs/{jid}/status",
        data={"status": "applied", "submission_ack": _tok(cfg, jid)},
        headers=H,
    )
    assert r.status_code == 200
    assert _state(conn, jid)["application_state"] == "applied"
    conn.close()


def test_stage_route_refuses_a_stale_page_and_accepts_the_ack(cfg):
    jid = _world(cfg)
    c = _client(cfg)
    r = c.post(f"/jobs/{jid}/stage/apply", data={"from_list": "1"}, headers=H)
    assert r.status_code == 409 and r.headers.get("HX-Refresh") == "true"
    conn = dbc.init_db(cfg)
    assert not _state(conn, jid)["application_state"]
    r = c.post(
        f"/jobs/{jid}/stage/apply",
        data={"from_list": "1", "submission_ack": _tok(cfg, jid)},
        headers=H,
    )
    assert r.status_code == 200
    assert _state(conn, jid)["application_state"] == "applied"
    conn.close()


def test_no_warning_or_block_on_screen_after_applying(cfg):
    jid = _world(cfg)
    c = _client(cfg)
    c.post(
        f"/jobs/{jid}/status",
        data={"status": "applied", "submission_ack": _tok(cfg, jid)},
        headers=H,
    )
    r = c.post(f"/jobs/{jid}/status", data={"status": "screen"}, headers=H)
    assert r.status_code == 200
    r = c.post(f"/jobs/{jid}/stage/advance", data={"from_list": "1"}, headers=H)
    assert r.status_code == 200
    conn = dbc.init_db(cfg)
    assert _state(conn, jid)["application_state"] == "interview"
    conn.close()
    assert "submitted you to this role" not in c.get(f"/jobs/{jid}").text
    assert "submitted you to this role" not in c.get("/applied").text


def test_unapplied_page_buttons_carry_the_warning_and_ack(cfg):
    jid = _world(cfg)
    page = _client(cfg).get(f"/jobs/{jid}").text
    assert "Ravi Shah" in page and "submission_ack" in page


def test_ack_is_not_needed_without_a_submission(cfg):
    conn = dbc.init_db(cfg)
    jid = _job(conn)
    conn.close()
    r = _client(cfg).post(f"/jobs/{jid}/status", data={"status": "applied"}, headers=H)
    assert r.status_code == 200


# --- logging a submission -------------------------------------------------------------


def _form(**over):
    return {"recruiter_name": "Ravi Shah", "firm": "Northwind Search", **over}


def test_logging_against_an_applied_same_role_job_needs_log_anyway(cfg):
    conn = dbc.init_db(cfg)
    jid = _job(conn, application_state="applied", applied_at="2026-09-13")
    conn.close()
    c = _client(cfg)
    r = c.post(f"/jobs/{jid}/submissions", data=_form(), headers=H)
    assert r.status_code == 409 and "You applied directly to this role on" in r.text
    conn = dbc.init_db(cfg)
    assert _count(conn) == 0
    r = c.post(f"/jobs/{jid}/submissions", data=_form(log_anyway="1"), headers=H)
    assert r.status_code == 200 and "Ravi Shah" in r.text
    assert _count(conn) == 1
    conn.close()


def test_a_rejected_direct_application_blocks_logging_until_log_anyway(cfg):
    conn = dbc.init_db(cfg)
    old = _job(conn, application_state="rejected", applied_at="2026-06-02")
    repost = _job(conn, city="Oakbrook", first_seen="2026-10-01T00:00:00")
    _cluster(conn, old, repost)
    conn.close()
    c = _client(cfg)
    r = c.post(f"/jobs/{repost}/submissions", data=_form(), headers=H)
    assert r.status_code == 409 and "now rejected" in r.text
    r = c.post(f"/jobs/{repost}/submissions", data=_form(log_anyway="1"), headers=H)
    assert r.status_code == 200


def test_logging_with_no_prior_application_just_saves(cfg):
    conn = dbc.init_db(cfg)
    jid = _job(conn)
    conn.close()
    r = _client(cfg).post(f"/jobs/{jid}/submissions", data=_form(), headers=H)
    assert r.status_code == 200 and "Ravi Shah" in r.text


def test_logged_text_is_escaped_and_listed_newest_first(cfg):
    conn = dbc.init_db(cfg)
    jid = _job(conn)
    conn.close()
    c = _client(cfg)
    c.post(
        f"/jobs/{jid}/submissions",
        data=_form(recruiter_name="Old One", submitted_on="2026-09-01"),
        headers=H,
    )
    r = c.post(
        f"/jobs/{jid}/submissions",
        data=_form(
            recruiter_name="<b>New</b>",
            firm="<i>F</i>",
            note="<script>x</script>",
            submitted_on="2026-10-02",
        ),
        headers=H,
    )
    assert "<script>x" not in r.text and "&lt;script&gt;" in r.text
    assert "&lt;b&gt;New" in r.text and "&lt;i&gt;F" in r.text
    assert r.text.index("New") < r.text.index("Old One")


def test_recruiter_select_mode_takes_name_from_the_contact(cfg):
    conn = dbc.init_db(cfg)
    jid = _job(conn)
    rec = add_recruiter(conn, "Ravi Shah", company="Northwind Search")
    conn.close()
    r = _client(cfg).post(
        f"/jobs/{jid}/submissions", data={"network_contact_id": str(rec)}, headers=H
    )
    assert r.status_code == 200 and "Ravi Shah" in r.text and "Northwind Search" in r.text


def test_contact_and_typed_name_together_is_400(cfg):
    conn = dbc.init_db(cfg)
    jid = _job(conn)
    rec = add_recruiter(conn, "Ravi Shah")
    conn.close()
    c = _client(cfg)
    for extra in ({"recruiter_name": "Other"}, {"firm": "Other Firm"}):
        r = c.post(
            f"/jobs/{jid}/submissions", data={"network_contact_id": str(rec), **extra}, headers=H
        )
        assert r.status_code == 400


def test_no_recipient_and_non_recruiter_and_future_date_are_400(cfg):
    conn = dbc.init_db(cfg)
    jid = _job(conn)
    person = add_network(conn, "Dana Fox")
    conn.close()
    c = _client(cfg)
    future = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    assert c.post(f"/jobs/{jid}/submissions", data={}, headers=H).status_code == 400
    assert (
        c.post(
            f"/jobs/{jid}/submissions", data={"network_contact_id": str(person)}, headers=H
        ).status_code
        == 400
    )
    assert (
        c.post(f"/jobs/{jid}/submissions", data=_form(submitted_on=future), headers=H).status_code
        == 400
    )


# --- security and ids -----------------------------------------------------------------


def test_routes_refuse_without_csrf_and_foreign_origin_and_bad_host(cfg):
    conn = dbc.init_db(cfg)
    jid = _job(conn)
    sid = _sub(conn, jid)
    conn.close()
    c = _client(cfg)
    assert c.post(f"/jobs/{jid}/submissions", data=_form()).status_code == 403
    assert c.post(f"/submissions/{sid}/delete").status_code == 403
    bad = {**H, "Origin": "https://evil.example.org"}
    assert c.post(f"/jobs/{jid}/submissions", data=_form(), headers=bad).status_code == 403
    assert c.post(f"/submissions/{sid}/delete", headers=bad).status_code == 403
    host = {**H, "host": "evil.example.com"}
    assert c.post(f"/jobs/{jid}/submissions", data=_form(), headers=host).status_code == 421
    conn = dbc.init_db(cfg)
    assert _count(conn) == 1
    conn.close()


def test_unknown_job_and_submission_ids_are_404(cfg):
    c = _client(cfg)
    assert c.post("/jobs/99999/submissions", data=_form(), headers=H).status_code == 404
    assert c.post("/submissions/99999/delete", headers=H).status_code == 404


def test_delete_removes_the_row(cfg):
    conn = dbc.init_db(cfg)
    jid = _job(conn)
    sid = _sub(conn, jid)
    conn.close()
    r = _client(cfg).post(f"/submissions/{sid}/delete", headers=H)
    assert r.status_code == 200 and "Ravi Shah" not in r.text
    conn = dbc.init_db(cfg)
    assert _count(conn) == 0
    conn.close()


def test_job_page_has_the_section_and_the_recruiter_select(cfg):
    conn = dbc.init_db(cfg)
    jid = _job(conn)
    add_recruiter(conn, "Ravi Shah")
    conn.close()
    page = _client(cfg).get(f"/jobs/{jid}").text
    assert 'id="submissions"' in page and "Recruiter submissions" in page
    assert re.search(r'name="network_contact_id"', page)


def test_shortlist_row_carries_the_warning_and_the_ack(cfg):
    _world(cfg)
    page = _client(cfg).get("/shortlist").text
    assert "submitted you to this role" in page and "submission_ack" in page


def test_a_refused_move_writes_nothing_and_leaves_no_open_transaction(cfg):
    jid = _world(cfg)
    conn = dbc.init_db(cfg)
    guard = subs.ack_guard(jid, RADIUS, GEO, "")
    from blastjob.web import service

    with pytest.raises(subs.AckRequired):
        service.set_application_state(conn, jid, "applied", ["", "drafted"], guard=guard)
    assert not conn.in_transaction
    assert not _state(conn, jid)["application_state"] and not _state(conn, jid)["applied_at"]
    conn.close()


def _drafted_world(cfg):
    conn = dbc.init_db(cfg)
    jid = _job(conn, application_state="drafted")
    _sub(conn, jid)
    conn.close()
    return jid


def test_drafted_shortlist_row_can_be_marked_applied_with_the_ack(cfg):
    jid = _drafted_world(cfg)
    c = _client(cfg)
    page = c.get("/shortlist").text
    assert "submission_ack" in page and "submitted you to this role" in page
    r = c.post(
        f"/jobs/{jid}/stage/apply",
        data={"from_list": "1", "submission_ack": _tok(cfg, jid)},
        headers=H,
    )
    assert r.status_code == 200
    conn = dbc.init_db(cfg)
    assert _state(conn, jid)["application_state"] == "applied"
    conn.close()


def test_drafted_job_page_buttons_carry_the_ack(cfg):
    jid = _drafted_world(cfg)
    page = _client(cfg).get(f"/jobs/{jid}").text
    assert "submission_ack" in page and "submitted you to this role" in page


def test_both_confirms_are_joined_into_one_message(cfg):
    conn = dbc.init_db(cfg)
    agency = _job(conn, company="Talonridge Global", city="West Fairhaven")
    _job(conn, application_state="applied", applied_at="2026-09-13")
    _sub(conn, agency)
    conn.close()
    c = _client(cfg)
    for page in (c.get("/shortlist").text, c.get(f"/jobs/{agency}").text):
        m = re.search(r'hx-confirm="([^"]*submitted you to this role[^"]*)"', page)
        assert m and "You already applied to" in m.group(1)

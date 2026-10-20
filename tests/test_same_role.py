"""Same role, different route: an agency posting that may be an employer's own
opening. Live incident (2026-09-13): the user applied to Northwind Services "Vice
President of Technology" (650) while Talonridge Global's "VP of Technology" (651),
the same role, sat on his Shortlist with a tailored packet. Nothing warned him.

These pin: suggestions carry evidence and never link anything by themselves;
'same' joins the two into one cluster (shown once, in its furthest stage);
'different' splits and suppresses forever; the Mark applied / Advance buttons
confirm by name when he has already applied through the other route.
"""

from __future__ import annotations

import datetime
import re
import tempfile

import pytest
from fastapi.testclient import TestClient

import tests.agency_fixture as agency_fixture
import tests.geo_fixture as geo_fixture
from blastjob.core import role_clusters as rc
from blastjob.core import same_role as sr
from blastjob.core.titles import normalize_role_title
from blastjob.db import client as dbc
from blastjob.matching.prompts import build_user_content
from blastjob.matching.scorer import _SYSTEM_PROMPT
from blastjob.models.config import BlastJobConfig
from blastjob.web import same_role as srv
from blastjob.web import stage_lists
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN


@pytest.fixture(autouse=True)
def _fixture_agencies():
    agency_fixture.install_agencies()


TOKEN = {CSRF_HEADER: LAUNCH_TOKEN}
RADIUS = BlastJobConfig().discovery.commute_radius_miles
GEO = geo_fixture.region_cfg()
_ROW_ID = re.compile(r'id="job-(\d+)"')
_seq = iter(range(1, 1_000_000))


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


def _job(conn, *, company: str, title: str, city: str, **cols) -> int:
    n = next(_seq)
    jid = dbc.upsert_job(
        conn,
        source=cols.pop("source", "adzuna"),
        external_id=f"x{n}",
        company=company,
        title=title,
        geo_city=city,
        geo_location_raw=cols.pop("raw", f"{city}, OH" if city else ""),
    )
    cols.setdefault("first_seen", "2026-09-04T21:29:56")
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), jid))
    conn.commit()
    return jid


def _northwind_pair(conn, *, employer_state: str = "applied", agency_state: str = "") -> tuple:
    employer = _job(
        conn,
        company="Northwind Services",
        title="Vice President of Technology",
        city="Fairhaven",
        review_state="kept",
        application_state=employer_state,
        applied_at="2026-09-13" if employer_state == "applied" else "",
        alignment_score=85,
    )
    agency = _job(
        conn,
        company="Talonridge Global",
        title="VP of Technology",
        city="West Fairhaven",
        review_state="kept",
        application_state=agency_state,
        alignment_score=84,
        first_seen="2026-09-02T07:00:00",
    )
    return agency, employer


def _cluster_of(conn, job_id: int):
    return conn.execute("SELECT cluster_id FROM jobs WHERE id=?", (job_id,)).fetchone()[0]


def _linked(conn, a: int, b: int) -> bool:
    ca = _cluster_of(conn, a)
    return ca is not None and ca == _cluster_of(conn, b)


# --- titles ------------------------------------------------------------------


def test_title_normalizer_folds_vp_and_drops_connectives():
    assert normalize_role_title("VP of Technology") == normalize_role_title(
        "Vice President of Technology"
    )
    assert normalize_role_title("Sr. Director, IT & Security (Hybrid)") == normalize_role_title(
        "Senior Director Information Technology and Security - Remote"
    )
    assert normalize_role_title("VP Technology") != normalize_role_title("VP Engineering")


# --- suggestions ---------------------------------------------------------------


def test_agency_job_gets_the_employer_as_one_candidate_with_evidence(conn):
    agency, employer = _northwind_pair(conn)

    cands = sr.possible_same_role(conn, agency, RADIUS, GEO)

    assert [c.job_id for c in cands] == [employer]
    c = cands[0]
    assert c.company == "Northwind Services" and c.kind == sr.EMPLOYER
    assert c.already_applied
    assert "same metro: West Fairhaven / Fairhaven" in c.evidence
    assert any(e.startswith("same title") for e in c.evidence)
    assert not _linked(conn, agency, employer)  # nothing linked


def test_employer_job_gets_the_agency_in_reverse(conn):
    agency, employer = _northwind_pair(conn)

    cands = sr.possible_same_role(conn, employer, RADIUS, GEO)

    assert [(c.job_id, c.kind) for c in cands] == [(agency, sr.AGENCY)]


def test_archived_employer_copy_still_counts_as_evidence(conn):
    agency = _job(conn, company="Talonridge Global", title="VP of Technology", city="Fairhaven")
    archived = _job(
        conn,
        company="Northwind Services Inc",
        title="Vice President of Technology",
        city="Fairhaven",
        review_state="archived",
    )

    assert [c.job_id for c in sr.possible_same_role(conn, agency, RADIUS, GEO)] == [archived]


def test_different_title_is_not_a_candidate(conn):
    agency = _job(conn, company="Talonridge Global", title="VP of Engineering", city="Fairhaven")
    _job(conn, company="Northwind Services", title="VP of Technology", city="Fairhaven")

    assert sr.possible_same_role(conn, agency, RADIUS, GEO) == []


def test_different_metro_is_not_a_candidate(conn):
    agency = _job(conn, company="Talonridge Global", title="VP of Technology", city="Fairhaven")
    _job(
        conn,
        company="Northwind Services",
        title="VP of Technology",
        city="Stockton",
        raw="Stockton, CA",
    )

    assert sr.possible_same_role(conn, agency, RADIUS, GEO) == []


def test_country_only_aggregator_matches_nothing(conn):
    agency = _job(conn, company="Jobwharf", title="VP of Technology", city="", raw="United States")
    _job(conn, company="Northwind Services", title="VP of Technology", city="Fairhaven")

    assert sr.possible_same_role(conn, agency, RADIUS, GEO) == []


def test_postings_months_apart_are_not_candidates(conn):
    agency = _job(conn, company="Talonridge Global", title="VP of Technology", city="Fairhaven")
    _job(
        conn,
        company="Northwind Services",
        title="VP of Technology",
        city="Fairhaven",
        first_seen="2026-05-01T00:00:00",
    )

    assert sr.possible_same_role(conn, agency, RADIUS, GEO) == []


def test_two_employers_are_both_shown_and_nothing_is_linked(conn, cfg):
    agency = _job(conn, company="Talonridge Global", title="VP of Technology", city="Fairhaven")
    a = _job(conn, company="Northwind Services", title="VP of Technology", city="Fairhaven")
    b = _job(conn, company="Copperleaf Corporation", title="VP Technology", city="Fairhaven")

    cands = sr.possible_same_role(conn, agency, RADIUS, GEO)
    page = _client(cfg).get(f"/jobs/{agency}").text

    assert {c.job_id for c in cands} == {a, b}
    assert set(sr.group_by_company(cands)) == {"northwind services", "copperleaf"}
    assert "Northwind Services · VP of Technology" in page
    assert "Copperleaf Corporation · VP Technology" in page
    assert "More than one employer matches" in page
    assert not any(_linked(conn, x, y) for x, y in ((agency, a), (agency, b), (a, b)))
    assert conn.execute("SELECT COUNT(*) FROM same_role_decisions").fetchone()[0] == 0


def test_list_candidates_are_computed_in_a_bounded_number_of_queries(conn):
    agency, employer = _northwind_pair(conn)
    for i in range(30):
        _job(conn, company="Talonridge Global", title=f"Director {i}", city="Fairhaven")
    ids = [r[0] for r in conn.execute("SELECT id FROM jobs")]
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        batch = sr.possible_same_role_batch(conn, ids, RADIUS, GEO)
    finally:
        conn.set_trace_callback(None)

    assert [c.job_id for c in batch[agency]] == [employer]
    assert len(statements) <= 2


# --- detail page ---------------------------------------------------------------


def test_detail_warns_red_naming_the_applied_employer(conn, cfg):
    agency, employer = _northwind_pair(conn)

    page = _client(cfg).get(f"/jobs/{agency}").text

    assert "Possibly the same role as" in page
    assert f'href="/jobs/{employer}"' in page
    assert "Northwind Services · Vice President of Technology" in page
    assert "(Applied, Sep 13)" in page
    assert "same-role-applied" in page
    assert "already applied there" in page
    assert "can cost you the placement" in page
    assert f"/jobs/{agency}/same-role/{employer}/same" in page


def test_detail_on_employer_says_an_agency_may_be_advertising_it(conn, cfg):
    agency, employer = _northwind_pair(conn, employer_state="")

    page = _client(cfg).get(f"/jobs/{employer}").text

    assert "An agency may be advertising this role too" in page
    assert "Talonridge Global · VP of Technology" in page
    assert "same-role-applied" not in page


def test_aggregator_with_no_candidate_gets_the_employer_hidden_note_and_chip(conn, cfg):
    jid = _job(
        conn,
        company="Swiftlane",
        title="Head of IT",
        city="",
        raw="United States",
        review_state="surfaced",
        alignment_score=90,
        source_state="active",
    )
    client = _client(cfg)

    page = client.get(f"/jobs/{jid}").text
    listing = client.get("/").text

    assert "Employer not named in this posting" in page
    assert "employer hidden" in listing


def test_confirm_same_joins_one_cluster_shown_once_in_applied(conn, cfg):
    agency, employer = _northwind_pair(conn)
    client = _client(cfg)

    resp = client.post(f"/jobs/{agency}/same-role/{employer}/same", headers=TOKEN)

    assert resp.status_code == 200
    assert _linked(conn, agency, employer)
    assert rc.decisions(conn) == {rc.pair_of(agency, employer): rc.SAME}
    assert "Also listed as" in resp.text
    shortlist = [int(i) for i in _ROW_ID.findall(client.get("/shortlist").text)]
    applied = [int(i) for i in _ROW_ID.findall(client.get("/applied").text)]
    assert agency not in shortlist
    assert applied.count(employer) == 1 and agency not in applied
    assert [j["id"] for j in stage_lists.applied_jobs(conn)] == [employer]


def test_undo_splits_and_records_different_and_suppresses_everything(conn, cfg):
    agency, employer = _northwind_pair(conn)
    client = _client(cfg)
    client.post(f"/jobs/{agency}/same-role/{employer}/same", headers=TOKEN)

    resp = client.post(f"/jobs/{agency}/same-role/{employer}/different", headers=TOKEN)

    assert resp.status_code == 200
    assert not _linked(conn, agency, employer)
    assert rc.decisions(conn) == {rc.pair_of(agency, employer): rc.DIFFERENT}
    assert sr.possible_same_role(conn, agency, RADIUS, GEO) == []
    page = client.get(f"/jobs/{agency}").text
    assert "Possibly the same role" not in page
    shortlist = client.get("/shortlist").text
    assert "already applied: Northwind Services" not in shortlist
    assert "possible duplicate of" not in shortlist


def test_not_the_same_on_a_suggestion_suppresses_it(conn, cfg):
    agency, employer = _northwind_pair(conn)

    _client(cfg).post(f"/jobs/{employer}/same-role/{agency}/different", headers=TOKEN)

    assert sr.possible_same_role(conn, agency, RADIUS, GEO) == []
    assert sr.possible_same_role(conn, employer, RADIUS, GEO) == []


def test_decision_route_requires_csrf(conn, cfg):
    agency, employer = _northwind_pair(conn)

    resp = _client(cfg).post(f"/jobs/{agency}/same-role/{employer}/same")

    assert resp.status_code == 403
    assert not _linked(conn, agency, employer)
    assert rc.decisions(conn) == {}


def test_decision_route_rejects_unknown_verdicts(conn, cfg):
    agency, employer = _northwind_pair(conn)

    resp = _client(cfg).post(f"/jobs/{agency}/same-role/{employer}/merge", headers=TOKEN)

    assert resp.status_code == 400


# --- lists and the apply guard -------------------------------------------------


def test_shortlist_row_shows_red_chip_and_mark_applied_confirms_by_name(conn, cfg):
    agency, _employer = _northwind_pair(conn)

    page = _client(cfg).get("/shortlist").text
    row = page[page.index(f'id="job-{agency}"') :]
    row = row[: row.index("</article>")]

    assert "already applied: Northwind Services" in row
    assert "same-role-chip-red" in row
    assert (
        'hx-confirm="You already applied to Northwind Services · Vice President of Technology, '
        'which may be this same role. Apply here too?"' in row
    )


def test_undecided_candidate_not_yet_applied_is_an_amber_duplicate_chip(conn, cfg):
    agency, _employer = _northwind_pair(conn, employer_state="")

    page = _client(cfg).get("/shortlist").text
    row = page[page.index(f'id="job-{agency}"') :]
    row = row[: row.index("</article>")]

    assert "possible duplicate of Northwind Services" in row
    assert 'hx-confirm="You already applied' not in row


def test_detail_applied_button_confirms_by_name(conn, cfg):
    agency, _employer = _northwind_pair(conn)

    page = _client(cfg).get(f"/jobs/{agency}").text

    assert "You already applied to Northwind Services · Vice President of Technology" in page
    status = page[page.index('id="app-status"') :]
    assert 'hx-confirm="You already applied to Northwind Services' in status


def test_list_page_calls_the_batch_lookup_once(conn, cfg, monkeypatch):
    for i in range(5):
        _job(
            conn,
            company="Talonridge Global",
            title=f"VP {i}",
            city="Fairhaven",
            review_state="kept",
            alignment_score=80,
        )
    calls: list[int] = []
    real = srv.sr.possible_same_role_batch

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(srv.sr, "possible_same_role_batch", counting)

    page = _client(cfg).get("/shortlist").text

    assert len(_ROW_ID.findall(page)) == 5
    assert len(calls) == 1


# --- scorer --------------------------------------------------------------------


def test_scorer_prompt_identifies_other_postings_without_asserting_sameness():
    prompt = _SYSTEM_PROMPT + build_user_content("history", [], 1000)

    assert "identify it only by its Company and Title exactly as listed in this batch" in prompt
    assert "Never state or imply that two postings at different companies are the same role" in (
        prompt
    )
    assert "never guess which employer a staffing agency or job aggregator" in prompt
    assert "similar title to" in prompt
    assert "unnamed" not in prompt


# --- previous applications: a repost of a role he applied to ---------------------


def _closed_repost(conn, *, state: str = "rejected", **repost_cols) -> tuple[int, int]:
    """Northwind Services applied Sep 13 (now `state`), re-served by Adzuna on Oct 8."""
    applied = _job(
        conn,
        company="Northwind Services",
        title="VP of Technology",
        city="Oakbrook",
        review_state="kept",
        application_state=state,
        applied_at="2026-09-13",
        alignment_score=85,
    )
    cols = {
        "city": "Fairhaven",
        "review_state": "surfaced",
        "source_state": "active",
        "alignment_score": 86,
        "first_seen": "2026-10-08T07:00:00",
        **repost_cols,
    }
    repost = _job(conn, company="Northwind Services Inc", title="VP of Technology", **cols)
    return applied, repost


def test_previous_application_suggested_for_closed_repost(conn):
    applied, repost = _closed_repost(conn)

    (cand,) = sr.possible_same_role(conn, repost, RADIUS, GEO)

    assert cand.job_id == applied
    assert cand.kind == sr.PREVIOUS
    assert cand.already_applied
    rows = conn.execute(f"SELECT {rc.FACT_COLS} FROM jobs ORDER BY id").fetchall()
    assert not rc.auto_same_role(rows[0], rows[1], RADIUS, GEO)


def test_previous_application_needs_no_time_window_and_accepts_a_loose_side(conn):
    applied, repost = _closed_repost(
        conn, city="", raw="United States", first_seen="2027-03-01T07:00:00"
    )

    assert [c.job_id for c in sr.possible_same_role(conn, repost, RADIUS, GEO)] == [applied]


def test_previous_suggestion_suppressed_by_different(conn, cfg):
    applied, repost = _closed_repost(conn)
    client = _client(cfg)

    client.post(f"/jobs/{repost}/same-role/{applied}/different", headers=TOKEN)

    assert sr.possible_same_role(conn, repost, RADIUS, GEO) == []
    assert "You applied to this role" not in client.get(f"/jobs/{repost}").text


def test_previous_suggestion_skips_a_job_already_in_the_same_cluster(conn):
    applied, repost = _closed_repost(conn)
    assert rc.merge_jobs(conn, applied, repost) == ""
    conn.commit()

    assert sr.possible_same_role(conn, repost, RADIUS, GEO) == []


def test_previous_suggestion_needs_the_same_employer(conn):
    _closed_repost(conn)
    other = _job(
        conn,
        company="Acme Corp",
        title="VP of Technology",
        city="Fairhaven",
        review_state="surfaced",
        source_state="active",
    )

    assert sr.possible_same_role(conn, other, RADIUS, GEO) == []


def test_detail_says_when_and_how_he_applied_before(conn, cfg):
    applied, repost = _closed_repost(conn)

    page = _client(cfg).get(f"/jobs/{repost}").text

    assert "You applied to this role on Sep 13 (rejected)" in page
    assert f'href="/jobs/{applied}"' in page
    assert f"/jobs/{repost}/same-role/{applied}/same" in page
    assert f"/jobs/{repost}/same-role/{applied}/different" in page
    assert "can cost you the placement" not in page
    assert "An agency may be advertising" not in page


def test_surfaced_row_chips_a_repost_of_a_previous_application(conn, cfg):
    _applied, repost = _closed_repost(conn)

    page = _client(cfg).get("/").text
    row = page[page.index(f'id="job-{repost}"') :]
    row = row[: row.index("</article>")]

    assert "applied before: Sep 13" in row
    assert "same-role-chip-red" not in row  # that application is closed


def test_shortlisted_repost_confirms_mark_applied_by_name(conn, cfg):
    _applied, repost = _closed_repost(conn, review_state="kept", application_state="")

    page = _client(cfg).get("/shortlist").text
    row = page[page.index(f'id="job-{repost}"') :]
    row = row[: row.index("</article>")]

    assert "applied before: Sep 13" in row
    assert 'hx-confirm="You already applied to Northwind Services · VP of Technology' in row


def test_repost_of_a_live_application_on_a_loose_location_chips_red(conn, cfg):
    _applied, repost = _closed_repost(conn, state="interview", city="", raw="United States")

    page = _client(cfg).get("/").text
    row = page[page.index(f'id="job-{repost}"') :]
    row = row[: row.index("</article>")]

    assert "applied before: Sep 13" in row
    assert "same-role-chip-red" in row


# --- a merge makes one application -------------------------------------------


def _application(conn, source: str, state: str, applied_at: str = "2026-09-13", **cols) -> int:
    return _job(
        conn,
        company="Northwind Services",
        title="Vice President of Technology",
        city="Fairhaven",
        source=source,
        review_state="kept",
        application_state=state,
        applied_at=applied_at,
        **cols,
    )


def _state(conn, job_id: int) -> tuple:
    r = conn.execute(
        "SELECT review_state, application_state, applied_at, archive_reason, archive_note "
        "FROM jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    return tuple(r)


def _notes(conn, job_id: int) -> str:
    return conn.execute("SELECT app_notes FROM jobs WHERE id=?", (job_id,)).fetchone()[0] or ""


def test_merging_two_applications_keeps_the_furthest_and_archives_the_other(conn, cfg):
    survivor = _application(conn, "workday:northwind", "screen", alignment_score=80)
    dup = _application(conn, "adzuna", "applied", alignment_score=90)

    resp = _client(cfg).post(f"/jobs/{dup}/same-role/{survivor}/same", headers=TOKEN)

    assert resp.status_code == 200 and "Not linked" not in resp.text
    assert _linked(conn, dup, survivor)
    assert _state(conn, survivor)[:2] == ("kept", "screen")
    assert _state(conn, dup) == ("archived", "", "", "other", f"Duplicate of #{survivor}")
    today = datetime.date.today().isoformat()
    assert (
        f"Merged #{dup} (adzuna, state applied, applied_at 2026-09-13) as a duplicate on {today}"
        in _notes(conn, survivor)
    )


def test_withdrawing_the_survivor_never_brings_the_duplicate_back(conn, cfg):
    survivor = _application(conn, "adzuna", "interview")
    dup = _application(conn, "linkedin", "applied")
    client = _client(cfg)
    client.post(f"/jobs/{survivor}/same-role/{dup}/same", headers=TOKEN)

    resp = client.post(f"/jobs/{survivor}/stage/withdraw", headers=TOKEN)

    assert resp.status_code == 200
    assert stage_lists.applied_jobs(conn) == []
    assert [j["id"] for j in stage_lists.closed_jobs(conn)] == [survivor]
    applied = [int(i) for i in _ROW_ID.findall(client.get("/applied").text)]
    assert dup not in applied


def test_merge_with_one_application_changes_no_state(conn):
    agency, employer = _northwind_pair(conn)

    assert srv.decide(conn, agency, employer, rc.SAME, RADIUS, GEO) == ""

    assert _state(conn, employer)[:2] == ("kept", "applied")
    assert _state(conn, agency)[:2] == ("kept", "")
    assert _notes(conn, employer) == ""


def test_drafted_rows_are_not_applications_and_are_never_archived(conn):
    a = _application(conn, "adzuna", "drafted", applied_at="")
    b = _application(conn, "linkedin", "drafted", applied_at="")

    assert srv.decide(conn, a, b, rc.SAME, RADIUS, GEO) == ""

    assert _state(conn, a)[0] == "kept" and _state(conn, b)[0] == "kept"


def _three_applications(conn) -> tuple[int, int, int]:
    a = _application(conn, "adzuna", "offer")
    b = _application(conn, "linkedin", "applied", applied_at="2026-09-10")
    c = _application(conn, "indeed", "screen", applied_at="2026-09-11")
    assert rc.merge_jobs(conn, a, b) == ""
    conn.commit()
    return a, b, c


def test_merging_into_a_cluster_with_two_applications_archives_both_duplicates(conn):
    a, b, c = _three_applications(conn)

    assert srv.decide(conn, a, c, rc.SAME, RADIUS, GEO) == ""

    assert _state(conn, a)[:2] == ("kept", "offer")
    assert _state(conn, b)[:3] == ("archived", "", "")
    assert _state(conn, c)[:3] == ("archived", "", "")
    notes = _notes(conn, a)
    assert f"Merged #{b} (linkedin, state applied, applied_at 2026-09-10)" in notes
    assert f"Merged #{c} (indeed, state screen, applied_at 2026-09-11)" in notes
    assert not conn.in_transaction


def _events(conn, job_id):
    return [
        (r["kind"], r["source"], r["note"])
        for r in conn.execute(
            "SELECT kind, source, note FROM application_events WHERE job_id=? ORDER BY id",
            (job_id,),
        )
    ]


def test_a_merge_writes_a_note_event_on_the_survivor_and_each_duplicate(conn):
    a, b, c = _three_applications(conn)
    b_events_before = _events(conn, b)

    assert srv.decide(conn, a, c, rc.SAME, RADIUS, GEO) == ""

    survivor_events = [e for e in _events(conn, a) if e[1] == "same_role"]
    assert len(survivor_events) == 2
    assert all(kind == "note" for kind, _, _ in survivor_events)
    joined = " ".join(note for _, _, note in survivor_events)
    assert f"#{b}" in joined and f"#{c}" in joined
    for dup in (b, c):
        dup_events = [e for e in _events(conn, dup) if e[1] == "same_role"]
        assert len(dup_events) == 1
        assert dup_events[0][0] == "note" and f"#{a}" in dup_events[0][2]
    # The duplicate's own earlier events stay where they were (not re-pointed).
    assert _events(conn, b)[: len(b_events_before)] == b_events_before


def test_a_failure_on_the_second_duplicate_rolls_the_whole_merge_back(conn, monkeypatch):
    a, b, c = _three_applications(conn)
    clusters_before = {j: _cluster_of(conn, j) for j in (a, b, c)}
    real = srv.retire_duplicate
    calls: list[int] = []

    def flaky(c_, row, survivor):
        calls.append(int(row["id"]))
        return real(c_, row, survivor) if len(calls) == 1 else False

    monkeypatch.setattr(srv, "retire_duplicate", flaky)

    error = srv.decide(conn, a, c, rc.SAME, RADIUS, GEO)

    assert error.startswith("Not linked:") and len(calls) == 2
    assert {j: _cluster_of(conn, j) for j in (a, b, c)} == clusters_before
    assert rc.decisions(conn) == {}
    assert [_state(conn, j)[:2] for j in (a, b, c)] == [
        ("kept", "offer"),
        ("kept", "applied"),
        ("kept", "screen"),
    ]
    assert _notes(conn, a) == ""
    assert conn.execute("SELECT COUNT(*) FROM application_events").fetchone()[0] == 0
    assert not conn.in_transaction


def test_retire_duplicate_refuses_a_row_whose_application_moved(conn):
    survivor = _application(conn, "adzuna", "interview")
    dup = _application(conn, "linkedin", "applied")
    stale = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (dup,)).fetchone())
    conn.execute("UPDATE jobs SET applied_at='2026-09-20' WHERE id=?", (dup,))
    conn.commit()

    assert srv.retire_duplicate(conn, stale, survivor) is False
    assert _state(conn, dup)[:2] == ("kept", "applied")


def test_posting_verdict_oob_status_keeps_the_apply_confirm(conn, cfg):
    agency, _employer = _northwind_pair(conn)

    r = _client(cfg).post(f"/jobs/{agency}/posting/open", headers=TOKEN)

    assert r.status_code == 200
    status = r.text[r.text.index('id="app-status"') :]
    assert 'hx-confirm="You already applied to Northwind Services' in status


def test_posting_verdict_oob_status_has_no_confirm_without_a_match(conn, cfg):
    jid = _job(
        conn,
        company="Solo Works",
        title="Chief Widget Officer",
        city="Fairhaven",
        review_state="kept",
        alignment_score=80,
    )

    r = _client(cfg).post(f"/jobs/{jid}/posting/open", headers=TOKEN)

    assert r.status_code == 200
    assert 'id="app-status"' in r.text
    assert "hx-confirm" not in r.text

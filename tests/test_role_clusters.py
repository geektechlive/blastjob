"""A repost of a role the user is actively applying to joins that application.

Live case (2026-10-08): Northwind Services "VP of Technology", job 650 (Adzuna,
applied Sep 13), re-served by Adzuna 34 days later under a new ad id. Same
source + outside AUTO_WINDOW_DAYS meant no merge, so it showed on Surfaced as a
new role and was recommended for a second application.
"""

from __future__ import annotations

import itertools
import re
import tempfile

import pytest
from fastapi.testclient import TestClient

import tests.agency_fixture as agency_fixture
import tests.geo_fixture as geo_fixture
from blastjob.core import recluster
from blastjob.core import role_clusters as rc
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app


@pytest.fixture(autouse=True)
def _fixture_agencies():
    agency_fixture.install_agencies()


RADIUS = BlastJobConfig().discovery.commute_radius_miles
GEO = geo_fixture.region_cfg()
_ROW_ID = re.compile(r'id="job-(\d+)"')
_seq = itertools.count(1)


def _fact(**over) -> dict:
    """One FACT_COLS-shaped row; the applied Northwind Services posting by default."""
    row = {
        "id": next(_seq),
        "source": "adzuna",
        "external_id": f"a{next(_seq)}",
        "company": "Northwind Services",
        "title": "VP of Technology",
        "geo_city": "Oakbrook",
        "geo_region": "OH",
        "geo_location_raw": "Oakbrook, OH",
        "geo_remote": 0,
        "first_seen": "2026-09-04T07:00:00",
        "cluster_id": None,
        "review_state": "kept",
        "application_state": "applied",
        "applied_at": "2026-09-13",
        "source_state": "active",
    }
    return {**row, **over}


def _repost(**over) -> dict:
    base = {
        "company": "Northwind Services Inc",
        "geo_city": "Fairhaven",
        "geo_location_raw": "Fairhaven, OH",
        "first_seen": "2026-10-08T07:00:00",
        "review_state": "surfaced",
        "application_state": "",
        "applied_at": "",
    }
    return _fact(**{**base, **over})


# --- the rule ----------------------------------------------------------------


def test_repost_joins_active_application_same_source_outside_window():
    applied, repost = _fact(), _repost()

    assert rc.days_apart(applied["first_seen"], repost["first_seen"]) > rc.AUTO_WINDOW_DAYS
    assert rc.auto_same_role(applied, repost, RADIUS, GEO)
    assert rc.auto_same_role(repost, applied, RADIUS, GEO)


@pytest.mark.parametrize("state", ["screen", "interview", "offer"])
def test_repost_joins_every_in_process_state(state):
    assert rc.auto_same_role(_fact(application_state=state), _repost(), RADIUS, GEO)


def test_repost_does_not_join_closed_application():
    applied = _fact(application_state="rejected")

    assert not rc.auto_same_role(applied, _repost(), RADIUS, GEO)


def test_repost_rule_needs_the_other_side_to_have_no_application_history():
    # Two applications are two decisions the user made: never merged silently.
    other_applied = _repost(review_state="kept", application_state="applied")
    other_dated = _repost(review_state="kept", application_state="", applied_at="2026-10-01")

    assert not rc.auto_same_role(_fact(), other_applied, RADIUS, GEO)
    assert not rc.auto_same_role(_fact(), other_dated, RADIUS, GEO)


def test_repost_rule_accepts_a_kept_but_untouched_repost():
    kept_fresh = _repost(review_state="kept", application_state="", applied_at="")

    assert rc.auto_same_role(_fact(), kept_fresh, RADIUS, GEO)


def test_repost_rule_needs_strict_metro():
    remote = _repost(geo_city="", geo_location_raw="Remote", geo_remote=1)
    unplaceable = _repost(geo_city="", geo_region="", geo_location_raw="United States")
    far = _repost(geo_city="Rivermont", geo_location_raw="Rivermont, OH")

    assert not rc.auto_same_role(_fact(), remote, RADIUS, GEO)
    assert not rc.auto_same_role(_fact(), unplaceable, RADIUS, GEO)
    assert not rc.auto_same_role(_fact(), far, RADIUS, GEO)


def test_repost_rule_never_joins_two_reqs_on_one_board():
    a = _fact(source="workday:x")
    b = _repost(source="workday:x")

    assert not rc.auto_same_role(a, b, RADIUS, GEO)


def test_repost_rule_skips_authoritative_side():
    # Falls back to the existing rule: different sources need the same metro.
    applied = _fact()
    board = _repost(
        source="greenhouse:northwindservices",
        geo_city="Rivermont",
        geo_location_raw="Rivermont, OH",
    )

    assert not rc.auto_same_role(applied, board, RADIUS, GEO)


def test_repost_rule_respects_agency():
    applied = _fact(company="Talonridge Global")
    repost = _repost(company="Talonridge Global")

    assert not rc.auto_same_role(applied, repost, RADIUS, GEO)


# --- through the database ----------------------------------------------------


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


def _row(conn, fact: dict) -> int:
    jid = dbc.upsert_job(
        conn,
        source=fact["source"],
        external_id=fact["external_id"],
        company=fact["company"],
        title=fact["title"],
        geo_city=fact["geo_city"],
        geo_region=fact["geo_region"],
        geo_location_raw=fact["geo_location_raw"],
    )
    conn.execute(
        "UPDATE jobs SET first_seen=?, review_state=?, application_state=?, applied_at=?, "
        "source_state='active', alignment_score=85 WHERE id=?",
        (
            fact["first_seen"],
            fact["review_state"],
            fact["application_state"],
            fact["applied_at"],
            jid,
        ),
    )
    conn.commit()
    return jid


def _northwind(conn) -> tuple[int, int]:
    return _row(conn, _fact()), _row(conn, _repost())


def _together(conn, a: int, b: int) -> bool:
    rows = conn.execute("SELECT cluster_id FROM jobs WHERE id IN (?, ?)", (a, b)).fetchall()
    return rows[0][0] is not None and rows[0][0] == rows[1][0]


def test_recluster_plan_merges_repost_into_application(conn):
    applied, repost = _northwind(conn)

    plan = recluster.plan_recluster(conn, RADIUS, GEO)
    changed = recluster.apply_recluster(conn, plan)

    assert [m.job_ids for m in plan] == [(applied, repost)]
    assert changed == 1
    assert _together(conn, applied, repost)


def test_one_repost_never_bridges_two_applications(conn):
    """The pairwise rule refuses two applications; the backfill's transitive
    union must not join them through a repost that matches both."""
    first = _row(conn, _fact(external_id="app1"))
    second = _row(conn, _fact(external_id="app2", application_state="interview"))
    repost = _row(conn, _repost(external_id="fresh"))

    changed = recluster.apply_recluster(conn, recluster.plan_recluster(conn, RADIUS, GEO))

    assert changed >= 1
    assert not _together(conn, first, second)
    assert _together(conn, first, repost) or _together(conn, second, repost)


def test_user_can_still_join_two_applications_by_hand(conn):
    first = _row(conn, _fact(external_id="app1"))
    second = _row(conn, _fact(external_id="app2"))

    assert rc.merge_jobs(conn, first, second) == ""
    assert _together(conn, first, second)


def test_different_decision_blocks_repost_merge(conn):
    applied, repost = _northwind(conn)
    rc.record_decision(conn, applied, repost, rc.DIFFERENT)
    conn.commit()

    assert rc.merge_jobs(conn, applied, repost) != ""
    assert recluster.plan_recluster(conn, RADIUS, GEO) == []
    assert not _together(conn, applied, repost)


def test_surfaced_page_drops_the_repost_once_merged_into_the_application(conn, cfg):
    applied, repost = _northwind(conn)
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda _c: None))
    before = [int(i) for i in _ROW_ID.findall(client.get("/").text)]

    recluster.apply_recluster(conn, recluster.plan_recluster(conn, RADIUS, GEO))

    after = [int(i) for i in _ROW_ID.findall(client.get("/").text)]
    applied_page = [int(i) for i in _ROW_ID.findall(client.get("/applied").text)]
    assert repost in before
    assert repost not in after
    assert applied_page.count(applied) == 1 and repost not in applied_page

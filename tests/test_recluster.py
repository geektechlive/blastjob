"""One job wearing different hats: the same-role auto rule, the backfill, and
the fingerprint path.

Shapes come from a read-only scan of live data (2026-09-13): one req
syndicated by Adzuna to several cities (Fernwood Digital x4, Globex Clearing x3), the same
employer under two company strings (Fabrikam Services / Fabrikam Services Inc),
a LinkedIn posting listed once for a city and once for "United States"
(Hartwell Dynamics). And genuinely different reqs that must stay apart: Lakeshore's
Mortgage Loan Originator in five branches, Oakmont's regional director in MA
and CA, Brightleaf's market director in NE and OH.
"""

from __future__ import annotations

import re
import tempfile

import pytest
from fastapi.testclient import TestClient

import tests.geo_fixture as geo_fixture
from blastjob import daily
from blastjob.core import recluster
from blastjob.core import role_clusters as rc
from blastjob.db import client as dbc
from blastjob.discovery.base import RawPosting
from blastjob.discovery.pipeline import run_discovery
from blastjob.models.config import BlastJobConfig, DiscoveryConfig
from blastjob.web import stage_lists
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

TOKEN = {CSRF_HEADER: LAUNCH_TOKEN}
RADIUS = BlastJobConfig().discovery.commute_radius_miles
GEO = geo_fixture.region_cfg()
_ROW_ID = re.compile(r'id="job-(\d+)"')
_seq = iter(range(1, 1_000_000))
DAY = "2026-09-04T21:29:56"


@pytest.fixture
def cfg() -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    geo_fixture.apply_region(c)
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _row(conn, source: str, company: str, title: str, city: str, raw: str = "", **cols) -> int:
    """A stored row in a cluster of its own, as the old fingerprint left it."""
    n = next(_seq)
    cluster = conn.execute(
        "INSERT INTO job_clusters (fingerprint, created_at) VALUES (?, datetime('now'))",
        (f"old-{n}",),
    ).lastrowid
    jid = dbc.upsert_job(
        conn,
        source=source,
        external_id=cols.pop("external_id", f"e{n}"),
        company=company,
        title=title,
        geo_city=city,
        geo_location_raw=raw or city,
        cluster_id=cluster,
    )
    cols.setdefault("first_seen", DAY)
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), jid))
    conn.commit()
    return jid


def _clusters(conn, ids) -> set:
    marks = ",".join("?" for _ in ids)
    return {r[0] for r in conn.execute(f"SELECT cluster_id FROM jobs WHERE id IN ({marks})", ids)}


def _backfill(conn) -> int:
    return recluster.apply_recluster(conn, recluster.plan_recluster(conn, RADIUS, GEO))


class _Feed:
    snapshot = False

    def __init__(self, source: str, posts: list[RawPosting]):
        self.source = source
        self._posts = posts

    async def fetch(self) -> list[RawPosting]:
        return self._posts


def _live_groups(conn) -> dict[str, list[int]]:
    fernwood_title = "Director of Digital Business Operations & Innovation"
    fernwood = [
        _row(conn, "adzuna", "Fernwood Digital", fernwood_title,
             city, f"{city}, {county} County", review_state="archived")
        for city, county in (
            ("Fairview", "Fairview"), ("Dellwood", "Dellwood"), ("Thornbury", "Thornbury"),
            ("West Fairhaven", "Fairhaven"),
        )
    ]  # fmt: skip
    globex_title = "Globex Clearing Tokenization - The Future of Digital Assets"
    globex = [
        _row(conn, "adzuna", "Globex Clearing", globex_title, city)
        for city in ("Fairhaven", "Thornbury", "Dellwood")
    ]
    winter = [
        _row(conn, "adzuna", "Fabrikam Services", "Vice President of Operations", "Ashford",
             review_state="kept", application_state="applied", applied_at="2026-09-13"),
        _row(conn, "adzuna", "Fabrikam Services Inc", "Vice President of Operations", "Fairhaven",
             review_state="archived"),
    ]  # fmt: skip
    lakeview = [
        _row(conn, "adzuna", "Lakeview Regional Health Authority",
             "Director, Security – Engineering, Operations and Incident Response", city,
             review_state=state, alignment_score=80, source_state="active")
        for city, state in (("Fairview", "queued"), ("Brookhaven", "archived"))
    ]  # fmt: skip
    hartwell = [
        _row(conn, "linkedin", "Hartwell Dynamics", "Head of Operations (Remote)",
             "Fairhaven", "Fairhaven, OH", geo_remote=1, review_state="kept",
             first_seen="2026-09-12T07:41:49"),
        _row(conn, "linkedin", "Hartwell Dynamics", "Head of Operations (Remote)",
             "United States ()", "United States (Remote)", geo_remote=1,
             review_state="archived", first_seen="2026-09-12T07:41:49"),
    ]  # fmt: skip
    lakeshore = [
        _row(conn, "dayforce:lakeshorecu", "Lakeshore Credit Union", "Mortgage Loan Originator",
             city, f"{city}, OH, USA", first_seen="2026-09-06T07:30:25")
        for city in ("Lakewood", "Bayport", "Maplecrest", "Fairview", "West Oakbrook")
    ]  # fmt: skip
    oakmont_title = "Director Regional Sales - Industrial Distribution"
    oakmont_jobs = [
        _row(conn, "workday:oakmont", "Oakmont", oakmont_title, city, raw)
        for city, raw in (
            ("Marlborough", "Marlborough, MA - USA"),
            ("Stockton", "Stockton, CA - USA"),
        )
    ]
    brightleaf = [
        _row(conn, "workday:brightleaf", "Brightleaf", "Market Director", city, city)
        for city in ("NE - Lincoln", "OH - Cincinnati")
    ]
    return {
        "fernwood": fernwood,
        "globex": globex,
        "winter": winter,
        "lakeview": lakeview,
        "hartwell": hartwell,
        "lakeshore": lakeshore,
        "oakmont_group": oakmont_jobs,
        "brightleaf": brightleaf,
    }


# --- the backfill over live-shaped groups ---------------------------------------


def test_backfill_lands_every_live_group_correctly(conn):
    groups = _live_groups(conn)

    _backfill(conn)

    for name in ("fernwood", "globex", "winter", "lakeview", "hartwell"):
        assert len(_clusters(conn, groups[name])) == 1, name
    assert len(_clusters(conn, groups["lakeshore"])) == 5
    assert len(_clusters(conn, groups["oakmont_group"])) == 2
    assert len(_clusters(conn, groups["brightleaf"])) == 2


def test_report_changes_nothing_and_names_the_stage_change(conn, cfg):
    groups = _live_groups(conn)
    before = conn.execute("SELECT id, cluster_id FROM jobs ORDER BY id").fetchall()
    clusters_before = conn.execute("SELECT COUNT(*) FROM job_clusters").fetchone()[0]

    report = daily.recluster_cli(cfg, conn, apply=False)

    assert [tuple(r) for r in conn.execute("SELECT id, cluster_id FROM jobs ORDER BY id")] == [
        tuple(r) for r in before
    ]
    assert conn.execute("SELECT COUNT(*) FROM job_clusters").fetchone()[0] == clusters_before
    assert conn.execute("SELECT COUNT(*) FROM same_role_decisions").fetchone()[0] == 0
    uw_line = next(line for line in report.splitlines() if "Lakeview Regional" in line)
    assert "visible before: Queue -> after: hidden" in uw_line and "STAGE CHANGES" in uw_line
    assert f"jobs {groups['winter'][0]}, {groups['winter'][1]}" in report
    assert "applied" not in report.splitlines()[-1]


def test_recluster_flag_applies_and_reports(conn, cfg):
    groups = _live_groups(conn)

    report = daily.recluster_cli(cfg, conn, apply=True)

    assert len(_clusters(conn, groups["fernwood"])) == 1
    assert report.splitlines()[-1] == "applied 5"


def test_archived_member_hides_the_merged_role_from_the_queue(conn):
    groups = _live_groups(conn)
    queued = groups["lakeview"][0]
    assert queued in [j["id"] for j in stage_lists.queued_jobs(conn)]

    _backfill(conn)

    assert queued not in [j["id"] for j in stage_lists.queued_jobs(conn)]


def test_stage_pages_stay_exclusive_after_merge(conn, cfg):
    groups = _live_groups(conn)
    _backfill(conn)
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda _c: None))

    seen: dict[int, str] = {}
    for route in ("/", "/queue", "/shortlist", "/applied", "/closed"):
        for jid in map(int, _ROW_ID.findall(client.get(route).text)):
            assert jid not in seen, (jid, seen.get(jid), route)
            seen[jid] = route
    assert seen.get(groups["winter"][0]) == "/applied"
    assert groups["winter"][1] not in seen


def test_backfill_only_merges_never_splits(conn):
    a = _row(conn, "adzuna", "Acme", "VP Ops", "Fairhaven")
    b = _row(conn, "greenhouse:acme", "Acme", "Chief of Staff", "Chicago", "Chicago, IL")
    conn.execute(
        "UPDATE jobs SET cluster_id=(SELECT cluster_id FROM jobs WHERE id=?) WHERE id=?", (a, b)
    )
    conn.commit()

    _backfill(conn)

    assert len(_clusters(conn, [a, b])) == 1


def test_agency_company_rows_never_auto_merge(conn):
    a = _row(conn, "linkedin", "Riverside Staffing", "VP Operations", "", "United States")
    b = _row(conn, "linkedin", "Riverside Staffing", "VP Operations", "", "United States")

    _backfill(conn)

    assert len(_clusters(conn, [a, b])) == 2


def test_same_title_sources_differ_needs_same_metro_or_a_loose_side(conn):
    board = _row(conn, "workday:acme", "Acme", "VP Ops", "Fairhaven", "Fairhaven, OH")
    far = _row(conn, "adzuna", "Acme", "VP Ops", "Stockton", "Stockton, CA",
               first_seen="2026-06-01T00:00:00")  # fmt: skip
    near = _row(conn, "greenhouse:acme", "Acme", "Vice President, Ops", "Oakbrook", "Oakbrook, OH")

    _backfill(conn)

    assert len(_clusters(conn, [board, near])) == 1
    assert len(_clusters(conn, [board, far])) == 2


def test_same_metro_reads_the_configured_towns_not_a_bundled_table(conn):
    """Isolates the thing this module actually changed: `place_of`/`same_metro`
    place a posting using the TOWNS IN THE CONFIG PASSED IN, not any table baked
    into the code. Fairhaven and Oakbrook are different city strings that are
    only "the same metro" because geo_fixture.TOWNS says so."""
    a = _row(conn, "workday:acme", "Acme", "VP Ops", "Fairhaven", "Fairhaven, OH")
    b = _row(conn, "greenhouse:acme", "Acme", "VP Ops", "Oakbrook", "Oakbrook, OH")
    rows = {int(r["id"]): r for r in conn.execute(f"SELECT {rc.FACT_COLS} FROM jobs")}

    place_a, place_b = (
        rc.place_of(rows[a], DiscoveryConfig()),
        rc.place_of(rows[b], DiscoveryConfig()),
    )
    assert place_a.centroid is None and place_b.centroid is None
    assert not rc.same_metro(place_a, place_b, RADIUS, loose=False)

    place_a, place_b = rc.place_of(rows[a], GEO), rc.place_of(rows[b], GEO)
    assert place_a.centroid is not None and place_b.centroid is not None
    assert rc.same_metro(place_a, place_b, RADIUS, loose=False)


# --- splits are honoured everywhere --------------------------------------------


def test_split_pair_is_never_re_merged_by_the_backfill(conn):
    groups = _live_groups(conn)
    _backfill(conn)
    first, second, *_rest = groups["fernwood"]

    rc.split_pair(conn, first, second, RADIUS, GEO)
    _backfill(conn)

    assert rc.decisions(conn) == {rc.pair_of(first, second): rc.DIFFERENT}
    assert len(_clusters(conn, [first, second])) == 2
    assert len(_clusters(conn, groups["fernwood"][2:] + [first])) == 1


def test_split_moves_the_others_linked_side_along(conn):
    groups = _live_groups(conn)
    _backfill(conn)
    agency = _row(conn, "adzuna", "Keystone Recruiting", "VP of Operations", "West Fairhaven")
    applied, archived = groups["winter"]
    assert rc.merge_jobs(conn, agency, applied) == ""
    rc.record_decision(conn, agency, applied, rc.SAME)
    conn.commit()

    rc.split_pair(conn, agency, applied, RADIUS, GEO)

    assert len(_clusters(conn, [applied, archived])) == 1
    assert len(_clusters(conn, [agency, applied])) == 2


async def test_split_pair_is_never_re_merged_by_the_fingerprint_path(conn, cfg):
    posts = [
        RawPosting(source="greenhouse:acme", external_id="1", company="Acme", title="VP Ops",
                   city="Fairhaven", description="x"),
        RawPosting(source="adzuna", external_id="9", company="Acme", title="VP Ops",
                   city="Fairhaven", description="x"),
    ]  # fmt: skip
    feeds = [_Feed("greenhouse:acme", posts[:1]), _Feed("adzuna", posts[1:])]
    await run_discovery(cfg, conn, adapters=feeds)
    board, adz = (r[0] for r in conn.execute("SELECT id FROM jobs ORDER BY id"))
    assert len(_clusters(conn, [board, adz])) == 1

    rc.split_pair(conn, board, adz, RADIUS, GEO)
    await run_discovery(cfg, conn, adapters=feeds)

    assert len(_clusters(conn, [board, adz])) == 2


async def test_confirmed_same_survives_a_rediscovery_refresh(conn, cfg):
    posts = [
        RawPosting(source="adzuna", external_id="1", company="Fabrikam Services",
                   title="Vice President of Operations", city="Fairhaven", description="x"),
        RawPosting(source="adzuna", external_id="2", company="Keystone Recruiting",
                   title="VP of Operations", city="West Fairhaven", description="x"),
    ]  # fmt: skip
    await run_discovery(cfg, conn, adapters=[_Feed("adzuna", posts)])
    employer, agency = (r[0] for r in conn.execute("SELECT id FROM jobs ORDER BY id"))
    assert len(_clusters(conn, [employer, agency])) == 2  # agencies are never automatic
    assert rc.merge_jobs(conn, agency, employer) == ""
    rc.record_decision(conn, agency, employer, rc.SAME)
    conn.commit()

    await run_discovery(cfg, conn, adapters=[_Feed("adzuna", posts)])

    assert len(_clusters(conn, [employer, agency])) == 1


# --- the daily run applies the rule to new postings ------------------------------


async def test_discovery_clusters_a_syndicated_req_across_cities(conn, cfg):
    posts = [
        RawPosting(source="adzuna", external_id=str(i), company="Fernwood Digital",
                   title="Director of Digital Business Operations & Innovation", city=city,
                   location_raw=f"{city}, OH", description="x")
        for i, city in enumerate(("Fairview", "Dellwood", "Thornbury", "West Fairhaven"))
    ]  # fmt: skip

    res = await run_discovery(cfg, conn, adapters=[_Feed("adzuna", posts)])

    ids = [r[0] for r in conn.execute("SELECT id FROM jobs")]
    assert len(_clusters(conn, ids)) == 1
    assert res.reclustered == 1


async def test_discovery_keeps_two_reqs_on_one_board_apart_even_in_one_city(conn, cfg):
    posts = [
        RawPosting(source="dayforce:lakeshorecu", external_id=str(i),
                   company="Lakeshore Credit Union", title="Mortgage Loan Originator",
                   city="Fairview", location_raw="Fairview, OH, USA", description="x")
        for i in (18118, 18119)
    ]  # fmt: skip

    await run_discovery(cfg, conn, adapters=[_Feed("dayforce:lakeshorecu", posts)])

    ids = [r[0] for r in conn.execute("SELECT id FROM jobs")]
    assert len(_clusters(conn, ids)) == 2


# --- detail page: the different hats ---------------------------------------------


def test_detail_lists_other_hats_and_splits_any_cluster(conn, cfg):
    groups = _live_groups(conn)
    _backfill(conn)
    applied, archived = groups["winter"]
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda _c: None))

    page = client.get(f"/jobs/{applied}").text
    assert "Also listed as" in page
    assert f'href="/jobs/{archived}"' in page
    assert "Fabrikam Services Inc · Vice President of Operations" in page
    assert f"/jobs/{applied}/same-role/{archived}/different" in page

    resp = client.post(f"/jobs/{applied}/same-role/{archived}/different", headers=TOKEN)

    assert resp.status_code == 200
    assert len(_clusters(conn, [applied, archived])) == 2
    assert rc.decisions(conn) == {rc.pair_of(applied, archived): rc.DIFFERENT}
    assert "Also listed as" not in resp.text


# --- code review: loose merges never involve an authoritative board ------------


def test_board_posting_and_unplaceable_linkedin_posting_do_not_merge(conn):
    board = _row(conn, "workday:acme", "Acme", "VP of Operations", "Fairhaven", "Fairhaven, OH")
    linkedin = _row(conn, "linkedin", "Acme", "VP of Operations", "United States ()",
                    "United States")  # fmt: skip

    _backfill(conn)

    assert len(_clusters(conn, [board, linkedin])) == 2


def test_board_posting_and_one_sided_remote_posting_do_not_merge(conn):
    board = _row(conn, "greenhouse:acme", "Acme", "VP of Operations", "Fairhaven")
    remote = _row(conn, "adzuna", "Acme", "VP of Operations", "", "Remote", geo_remote=1)

    _backfill(conn)

    assert len(_clusters(conn, [board, remote])) == 2


def test_board_postings_both_remote_merge(conn):
    board = _row(conn, "greenhouse:acme", "Acme", "VP of Operations", "", "Remote", geo_remote=1)
    other = _row(conn, "linkedin", "Acme", "VP of Operations", "", "United States", geo_remote=1)

    _backfill(conn)

    assert len(_clusters(conn, [board, other])) == 1


def test_two_boards_need_strict_metro(conn):
    old = _row(conn, "greenhouse:acme", "Acme", "VP of Operations", "", "United States")
    new = _row(conn, "workday:acme", "Acme", "VP of Operations", "Fairhaven", "Fairhaven, OH")
    moved = _row(conn, "lever:acme", "Acme", "VP of Operations", "Oakbrook", "Oakbrook, OH")

    _backfill(conn)

    assert len(_clusters(conn, [old, new])) == 2
    assert len(_clusters(conn, [new, moved])) == 1


def test_country_only_cities_are_not_the_same_metro(conn):
    board = _row(conn, "workday:acme", "Acme", "VP of Operations", "United States", "United States")
    other = _row(conn, "linkedin", "Acme", "VP of Operations", "United States ()",
                 "United States")  # fmt: skip

    _backfill(conn)

    assert len(_clusters(conn, [board, other])) == 2

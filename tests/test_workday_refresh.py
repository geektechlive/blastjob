"""A cheap refresh must never DEGRADE what a full fetch already learned.

Workday skips the detail GET for any posting whose JD we already hold. The
posting it emits in that case knows only what the listing said — which is a
building name or a count like "5 Locations", and nothing at all about remote
work. Writing those over the detail-derived city/region/remote is how 120 live
rows ended up with an "N Locations" placeholder and geo_remote reset to 0.

Also covers the remote/hybrid intent parse in normalization: a location that
literally reads "Remote-Ohio" must set geo_remote=1.
"""

from __future__ import annotations

import tempfile

import httpx
import pytest

from blastjob.db import client as dbc
from blastjob.discovery import workday
from blastjob.discovery.base import RawPosting
from blastjob.discovery.normalize import resolve_remote, to_job_columns
from blastjob.discovery.pipeline import known_jd_ids, run_discovery
from blastjob.models.config import BlastJobConfig

# A listing whose location text is the useless multi-site placeholder Workday
# returns for a req posted to several buildings (live shape, a multi-site retailer).
LISTING = {
    "total": 1,
    "jobPostings": [
        {
            "title": "Director, Business Infrastructure",
            "externalPath": "/job/Ohio/Director_R26-1",
            "locationsText": "5 Locations",
            "postedOn": "Posted 4 Days Ago",
            "bulletFields": ["R26-1"],
        }
    ],
}

# The detail endpoint knows the real place AND that the role is remote.
DETAIL = {
    "jobPostingInfo": {
        "title": "Director, Business Infrastructure",
        "jobDescription": "<p>Lead enterprise infrastructure.</p>",
        "location": "Remote-Ohio",
        "startDate": "2026-08-31",
        "jobReqId": "R26-1",
        "country": {"descriptor": "United States of America"},
        "remoteType": "Remote",
        "externalUrl": "https://acme.wd1.myworkdayjobs.com/Careers/job/Ohio/Director_R26-1",
    }
}


def _transport(calls: list[str] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json=LISTING)
        if "/job/" in request.url.path:
            return httpx.Response(200, json=DETAIL)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


# ---------- the presence stub knows nothing about place or remote ----------


def test_presence_stub_omits_location_and_remote_entirely():
    ref = workday.parse_board_token("acme/wd1/Careers")
    item = workday.parse_listing(LISTING)[0]
    stub = workday.presence_stub("Acme", ref, item)
    assert stub.description == ""
    assert stub.extra["detail_skipped"] is True
    assert (stub.location_raw, stub.city, stub.region, stub.remote) == ("", "", "", None)

    cols = to_job_columns(stub)
    for absent in ("raw_jd", "geo_location_raw", "geo_city", "geo_region", "geo_remote"):
        assert absent not in cols, f"{absent} must be absent-as-unchanged on a presence stub"


def test_detail_failure_stub_still_carries_what_the_listing_said():
    """Distinct path: the detail fetch was ATTEMPTED and failed, so there is no
    stored detail to protect and the listing location is better than nothing."""
    ref = workday.parse_board_token("acme/wd1/Careers")
    item = dict(workday.parse_listing(LISTING)[0], locations_text="Springfield, Ohio")
    stub = workday.stub_posting("Acme", ref, item)
    assert stub.location_raw == "Springfield, Ohio"
    assert to_job_columns(stub)["geo_region"] == "OH"


def test_placeholder_location_is_never_written():
    """'5 Locations' is a count, not a place — writing it destroys a known city."""
    p = RawPosting(
        source="workday:acme",
        external_id="R26-1",
        company="Acme",
        title="Director, Ops",
        location_raw="5 Locations",
        city="5 Locations",
    )
    cols = to_job_columns(p)
    assert "geo_location_raw" not in cols
    assert "geo_city" not in cols
    assert "geo_region" not in cols


# ---------- the end-to-end refresh ----------


@pytest.mark.asyncio
async def test_refresh_via_presence_stub_retains_detail_derived_geo():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)

    full = workday.WorkdayAdapter(
        "Acme", "acme/wd1/Careers", queries=["director"], transport=_transport()
    )
    await run_discovery(cfg, conn, adapters=[full])
    before = conn.execute(
        "SELECT geo_location_raw, geo_city, geo_region, geo_remote, cluster_id "
        "FROM jobs WHERE source='workday:acme'"
    ).fetchone()
    assert before["geo_location_raw"] == "Remote-Ohio"
    assert before["geo_remote"] == 1

    calls: list[str] = []
    stubbed = workday.WorkdayAdapter(
        "Acme",
        "acme/wd1/Careers",
        queries=["director"],
        known_ids=known_jd_ids(conn, "workday:acme"),
        transport=_transport(calls),
    )
    res = await run_discovery(cfg, conn, adapters=[stubbed])
    assert not any(c.startswith("GET") for c in calls)  # the refresh stayed cheap
    assert res.stale_marked == 0

    after = conn.execute(
        "SELECT geo_location_raw, geo_city, geo_region, geo_remote, cluster_id, source_state "
        "FROM jobs WHERE source='workday:acme'"
    ).fetchone()
    assert after["geo_location_raw"] == before["geo_location_raw"]
    assert after["geo_city"] == before["geo_city"]
    assert after["geo_region"] == before["geo_region"]
    assert after["geo_remote"] == 1
    assert after["source_state"] == "active"
    # and the cheap refresh must not reshuffle the cluster it can't fingerprint
    assert after["cluster_id"] == before["cluster_id"]
    assert conn.execute("SELECT COUNT(*) n FROM job_clusters").fetchone()["n"] == 1
    conn.close()


# ---------- remote / hybrid intent ----------


@pytest.mark.parametrize(
    "location_raw,expected",
    [
        ("Remote-Ohio", True),
        ("Remote - US", True),
        ("REMOTE, OH", True),
        ("Work From Home - Ohio", True),
        ("Hybrid Remote - Springfield, OH", False),
        ("Springfield, Ohio, United States", None),
        ("", None),
    ],
)
def test_remote_intent_parsed_from_location_text(location_raw, expected):
    p = RawPosting(source="s", external_id="1", location_raw=location_raw)
    assert resolve_remote(p) is expected


def test_remote_ohio_sets_geo_remote_column():
    p = RawPosting(
        source="workday:froedtert",
        external_id="R1",
        company="Froedtert",
        title="Director, Contact Center",
        location_raw="Remote-Ohio",
    )
    assert to_job_columns(p)["geo_remote"] == 1


def test_structured_remote_flag_wins_over_text_and_workplace_type():
    """Ashby publishes isRemote; never second-guess it from a location string."""
    explicit_false = RawPosting(
        source="ashby:x", external_id="1", remote=False, location_raw="Remote - US"
    )
    assert resolve_remote(explicit_false) is False
    assert to_job_columns(explicit_false)["geo_remote"] == 0

    hybrid = RawPosting(source="workday:x", external_id="2", workplace_type="Hybrid")
    assert resolve_remote(hybrid) is False


def test_unknown_remote_is_not_written():
    p = RawPosting(source="s", external_id="1", location_raw="Springfield, OH")
    assert "geo_remote" not in to_job_columns(p)


# ---------- the rows already degraded must repair themselves ----------


@pytest.mark.asyncio
async def test_stored_placeholder_location_forces_one_more_detail_fetch():
    """Without this, D1's fix freezes the damage instead of repairing it: a row
    that already holds "5 Locations" also holds a JD, so it takes the presence
    stub path forever and the presence stub now writes no location at all.
    Excluding it from `known_ids` buys exactly one detail GET, after which the
    row has a real location and drops back out of the expensive path."""
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)
    dbc.upsert_job(
        conn,
        source="workday:acme",
        external_id="R26-1",
        company="Acme",
        title="Director, Ops",
        raw_jd="a stored jd",
        geo_location_raw="5 Locations",
        geo_city="5 Locations",
    )
    assert known_jd_ids(conn, "workday:acme") == set()  # placeholder -> not "known"

    calls: list[str] = []
    a = workday.WorkdayAdapter(
        "Acme",
        "acme/wd1/Careers",
        queries=["director"],
        known_ids=known_jd_ids(conn, "workday:acme"),
        transport=_transport(calls),
    )
    await run_discovery(cfg, conn, adapters=[a])
    row = conn.execute(
        "SELECT geo_location_raw, geo_remote FROM jobs WHERE external_id='R26-1'"
    ).fetchone()
    assert sum(c.startswith("GET") for c in calls) == 1
    assert row["geo_location_raw"] == "Remote-Ohio"
    assert row["geo_remote"] == 1
    # repaired -> back on the cheap path next run
    assert known_jd_ids(conn, "workday:acme") == {"R26-1"}
    conn.close()


def test_known_jd_ids_only_exempts_the_exact_placeholder_shape():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)
    for ext_id, loc in [
        ("keep-1", "Springfield, Ohio"),
        ("keep-2", "Remote-Ohio"),
        ("keep-3", "Building 5 Locations Campus"),
        ("refetch-1", "5 Locations"),
        ("refetch-2", "2 locations"),
        ("refetch-3", "1 Location"),
    ]:
        dbc.upsert_job(
            conn,
            source="workday:acme",
            external_id=ext_id,
            company="Acme",
            title="Director, Ops",
            raw_jd="jd",
            geo_location_raw=loc,
        )
    assert known_jd_ids(conn, "workday:acme") == {"keep-1", "keep-2", "keep-3"}
    conn.close()

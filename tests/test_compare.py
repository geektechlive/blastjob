"""compare_rows() SQL/aggregation logic and the compare.html template render."""

from __future__ import annotations

import tempfile

import pytest

import blastjob.web.app as appmod
from blastjob.core import agencies
from blastjob.core import company as co
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import compare


@pytest.fixture(autouse=True)
def _configured_agency():
    agencies.set_extra_agencies(["Acme Talent Partners"])


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    connection = dbc.init_db(cfg)

    aggregator_id = dbc.upsert_job(
        connection,
        source="manual",
        external_id="1",
        company="Lensa",
        title="Senior Director, Operations",
    )
    nwi_id = dbc.upsert_job(
        connection,
        source="manual",
        external_id="2",
        company="Northwind Insurance Group",
        title="VP of Business Operations (Remote)",
        comp_min=190_000,
        comp_max=225_000,
        comp_provenance="posted",
        geo_remote=1,
        geo_location_raw="United States",
    )
    foundation_id = dbc.upsert_job(
        connection,
        source="manual",
        external_id="3",
        company="Northshore Digital Foundation",
        title="Chief Operations Officer",
        geo_location_raw="United States",
    )

    connection.execute(
        "UPDATE jobs SET review_state='surfaced', alignment_score=82 WHERE id=?", (aggregator_id,)
    )
    connection.execute(
        "UPDATE jobs SET review_state='surfaced', alignment_score=78 WHERE id=?", (nwi_id,)
    )
    connection.execute(
        "UPDATE jobs SET review_state='surfaced', alignment_score=80 WHERE id=?", (foundation_id,)
    )
    connection.commit()

    co.upsert_profile(
        connection,
        co.CompanyProfile(
            company="Northwind Insurance Group",
            industry="Insurance services",
            employees=7000,
            hq="Chicago, IL",
        ),
    )
    return connection


def test_compare_rows_orders_by_alignment_score_desc(conn):
    rows = compare.compare_rows(conn)
    assert [r["company"] for r in rows] == [
        "Lensa",
        "Northshore Digital Foundation",
        "Northwind Insurance Group",
    ]


def test_northwind_comp_and_profile_fields(conn):
    rows = compare.compare_rows(conn)
    northwind = next(r for r in rows if r["company"] == "Northwind Insurance Group")
    assert northwind["comp"] == "$190k–$225k posted"
    assert northwind["industry"] == "Insurance services"
    assert northwind["size"] == "7,000 employees"
    assert northwind["where"].startswith("Remote")


def test_aggregator_row_flagged_as_aggregator(conn):
    rows = compare.compare_rows(conn)
    aggregator_row = next(r for r in rows if r["company"] == "Lensa")
    assert any("aggregator" in f for f in aggregator_row["flags"])


def test_foundation_flags_missing_comp_and_vague_location(conn):
    rows = compare.compare_rows(conn)
    foundation = next(r for r in rows if r["company"] == "Northshore Digital Foundation")
    assert "comp not posted" in foundation["flags"]
    assert "location: country only" in foundation["flags"]


def test_source_filter_with_no_matches_is_empty(conn):
    assert compare.compare_rows(conn, source="nope") == []


def test_is_agency_matches_known_staffing_firms():
    assert compare.is_agency("Acme Talent Partners")
    assert compare.is_agency("Acme Talent Partners Inc")
    assert not compare.is_agency("Northwind Credit Union")


def test_red_flags_on_agency_row_includes_aggregator_flag(conn):
    rows = compare.compare_rows(conn)
    aggregator_row = next(r for r in rows if r["company"] == "Lensa")
    agency_id = dbc.upsert_job(
        conn,
        source="manual",
        external_id="4",
        company="Acme Talent Partners",
        title="Operations Director",
    )
    conn.execute(
        "UPDATE jobs SET review_state='surfaced', alignment_score=75 WHERE id=?", (agency_id,)
    )
    conn.commit()
    agency_row = conn.execute(
        "SELECT company, comp_provenance, comp_max, geo_location_raw, geo_remote "
        "FROM jobs WHERE id=?",
        (agency_id,),
    ).fetchone()
    flags = compare.red_flags(agency_row, None, 200_000)
    assert any("aggregator" in f for f in flags)
    # sanity: the fixture's aggregator row still flags too (unchanged behavior)
    assert any("aggregator" in f for f in aggregator_row["flags"])


def test_compare_template_renders_titles_and_flags(conn):
    rows = compare.compare_rows(conn)
    # Render through the APP's environment, not a fresh one: the production env
    # registers the `safe_url` filter, and a locally built env silently proves
    # nothing about what the cockpit actually serves.
    env = appmod.templates.env
    html = env.get_template("compare.html").render(
        rows=rows, state="surfaced", source="", token="", csrf_header="x", archive_reasons={}
    )
    assert "Senior Director, Operations" in html
    assert "VP of Business Operations (Remote)" in html
    assert "Chief Operations Officer" in html
    assert "aggregator / recruiting platform" in html

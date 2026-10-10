"""The whole-card LinkedIn repair (`blastjob-daily --repair-linkedin-titles[-report]`).

15 live LinkedIn rows stored the whole job card as the title with an empty
company. The repair splits them, but only on structure (a cluster sibling's or
another DB row's company); a vocabulary split is listed as "needs confirmation"
and applied only for ids the user passes with --accept. The report stages the
repairs in a savepoint, plans the recluster on the repaired data and rolls back,
so it predicts exactly what the apply run merges and leaves the DB untouched.
"""

from __future__ import annotations

import argparse
import tempfile

import pytest

import tests.geo_fixture as geo_fixture
from blastjob import daily_tools
from blastjob.core import role_clusters as rc
from blastjob.db import client as dbc
from blastjob.discovery import linkedin_repair as lr
from blastjob.models.config import BlastJobConfig

RADIUS = BlastJobConfig().discovery.commute_radius_miles
GEO = geo_fixture.region_cfg()
GRANITE = (
    "Vice President Information Technology Granite Ridge Hotels & Resorts · Fairhaven, OH (On-site)"
)
COPPERFIELD = (
    "Director of IT Infrastructure Copperfield · Fairhaven, OH $240K-$250K / year Easy Apply"
)
BRIGHTLAKE = "Head of Infrastructure (Remote) Brightlake Corporation · Fairhaven, OH"
AMBIGUOUS = "Director of IT Acme Technologies · Fairhaven, OH"


@pytest.fixture
def cfg() -> BlastJobConfig:
    c = BlastJobConfig()
    geo_fixture.apply_region(c)
    c.paths.data_dir = tempfile.mkdtemp()
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(conn, source: str, ext: str, title: str, company: str = "", **cols) -> int:
    jid = dbc.upsert_job(conn, source=source, external_id=ext, company=company, title=title)
    cols.setdefault("first_seen", "2026-10-01T08:00:00")
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), jid))
    conn.commit()
    return jid


@pytest.fixture
def rows(conn) -> dict[str, int]:
    granite = _job(conn, "linkedin", "1917", GRANITE)
    copperfield = _job(conn, "linkedin", "914", COPPERFIELD)
    sibling = _job(
        conn, "adzuna", "a1", "Director of IT Infrastructure", "Copperfield", geo_city="Fairhaven"
    )
    assert rc.merge_jobs(conn, copperfield, sibling) == ""
    conn.commit()
    brightlake = _job(conn, "linkedin", "1577", BRIGHTLAKE)
    brightlake_board = _job(
        conn,
        "adzuna",
        "a2",
        "Head of Infrastructure",
        "Brightlake Corporation",
        geo_city="Fairhaven",
        geo_region="OH",
        geo_location_raw="Fairhaven, OH",
    )
    ambiguous = _job(conn, "linkedin", "1578", AMBIGUOUS)
    return {
        "granite": granite,
        "copperfield": copperfield,
        "brightlake": brightlake,
        "brightlake_board": brightlake_board,
        "ambiguous": ambiguous,
    }


def _snapshot(conn) -> tuple[list[tuple], list[tuple]]:
    jobs = [tuple(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()]
    clusters = [tuple(r) for r in conn.execute("SELECT * FROM job_clusters ORDER BY id")]
    return jobs, clusters


def _row(conn, jid: int):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


def test_plan_marks_structural_splits_to_apply_and_vocab_splits_for_confirmation(conn, rows):
    plan = {r.job_id: r for r in lr.plan_repairs(conn)}

    assert (
        plan[rows["copperfield"]].basis == lr.BASIS_SIBLING and plan[rows["copperfield"]].accepted
    )
    assert plan[rows["brightlake"]].basis == lr.BASIS_DB and plan[rows["brightlake"]].accepted
    assert plan[rows["granite"]].basis == lr.BASIS_VOCAB
    assert not plan[rows["granite"]].accepted
    assert plan[rows["ambiguous"]].basis == lr.BASIS_NONE
    assert not plan[rows["ambiguous"]].accepted


def test_accept_applies_a_vocab_split_only_for_the_listed_ids(conn, rows):
    plan = {r.job_id: r for r in lr.plan_repairs(conn, accept={rows["granite"]})}

    assert plan[rows["granite"]].accepted
    assert plan[rows["granite"]].card.company == "Granite Ridge Hotels & Resorts"


def test_report_writes_nothing_and_predicts_the_apply_runs_merges(conn, rows):
    before = _snapshot(conn)

    report = lr.run_repair(conn, RADIUS, GEO, apply=False)

    assert _snapshot(conn) == before
    assert not conn.in_transaction
    applied = lr.run_repair(conn, RADIUS, GEO, apply=True)
    assert [m.job_ids for m in report.merges] == [m.job_ids for m in applied.merges]
    assert (rows["brightlake"], rows["brightlake_board"]) in [m.job_ids for m in applied.merges]
    assert applied.merged == 1
    brightlake = _row(conn, rows["brightlake"])
    assert brightlake["cluster_id"] is not None
    assert brightlake["cluster_id"] == _row(conn, rows["brightlake_board"])["cluster_id"]


def test_apply_rewrites_title_company_and_geo_through_the_discovery_helpers(conn, rows):
    lr.run_repair(conn, RADIUS, GEO, apply=True)

    brightlake = _row(conn, rows["brightlake"])
    assert (brightlake["title"], brightlake["company"]) == (
        "Head of Infrastructure",
        "Brightlake Corporation",
    )
    assert (brightlake["geo_location_raw"], brightlake["geo_city"], brightlake["geo_region"]) == (
        "Fairhaven, OH",
        "Fairhaven",
        "OH",
    )
    assert brightlake["geo_remote"] == 1
    copperfield = _row(conn, rows["copperfield"])
    assert (copperfield["title"], copperfield["company"]) == (
        "Director of IT Infrastructure",
        "Copperfield",
    )
    # Unconfirmed vocab split and no-split rows are left exactly as they were.
    assert _row(conn, rows["granite"])["title"] == GRANITE
    assert _row(conn, rows["granite"])["company"] == ""
    assert _row(conn, rows["ambiguous"])["title"] == AMBIGUOUS


def test_apply_with_accept_repairs_the_confirmed_vocab_row(conn, rows):
    lr.run_repair(conn, RADIUS, GEO, apply=True, accept={rows["granite"]})

    granite = _row(conn, rows["granite"])
    assert (granite["title"], granite["company"]) == (
        "Vice President Information Technology",
        "Granite Ridge Hotels & Resorts",
    )


def test_report_lists_one_line_per_row_and_the_merges(conn, rows):
    text = lr.format_repair(lr.run_repair(conn, RADIUS, GEO, apply=False))

    assert f"{rows['granite']} | {GRANITE} | Vice President Information Technology" in text
    assert "needs confirmation" in text
    assert "merge Brightlake Corporation" in text
    assert "applied" not in text.splitlines()[-1]


def test_cli_dispatch_report_and_apply(cfg, conn, rows):
    ap = argparse.ArgumentParser()
    daily_tools.add_arguments(ap)

    report = daily_tools.dispatch(
        ap.parse_args(["--repair-linkedin-titles-report", "--accept", str(rows["granite"])]),
        cfg,
        conn,
    )
    assert report is not None and "accepted" in report
    assert _row(conn, rows["granite"])["company"] == ""

    daily_tools.dispatch(
        ap.parse_args(["--repair-linkedin-titles", "--accept", str(rows["granite"])]), cfg, conn
    )
    assert _row(conn, rows["granite"])["company"] == "Granite Ridge Hotels & Resorts"
    assert daily_tools.dispatch(ap.parse_args([]), cfg, conn) is None


def test_accept_rejects_a_non_numeric_id():
    ap = argparse.ArgumentParser()
    daily_tools.add_arguments(ap)
    with pytest.raises(SystemExit):
        ap.parse_args(["--accept", "1917,abc"])

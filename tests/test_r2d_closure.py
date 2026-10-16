"""Round 2 D3/D7: dropping a bad link undoes the close it caused; a dry-run
(shadow) pass does not hide a newly closed kept job from the real run's digest."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta

import pytest

import tests.geo_fixture as geo_fixture
from blastjob.core import closure
from blastjob.core import role_clusters as rc
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from tests.test_d_closure_clustering import _deps, _job, _link_to, _row

NOW = datetime(2026, 10, 16, 8, 0, 0)
RADIUS = BlastJobConfig().discovery.commute_radius_miles
GEO = geo_fixture.region_cfg()


@pytest.fixture
def cfg():
    c = BlastJobConfig()
    geo_fixture.apply_region(c)
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    c.discovery.closure_auto = True
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _setup_closed_via_link(conn):
    board = _job(conn, source="greenhouse:acme", ext="g1", review="new", source_state="stale")
    agg = _job(conn, source="linkedin", ext="l1")
    cid = conn.execute(
        "INSERT INTO job_clusters (fingerprint, created_at) VALUES ('f','x')"
    ).lastrowid
    conn.execute("UPDATE jobs SET cluster_id=? WHERE id IN (?, ?)", (cid, board, agg))
    conn.commit()
    _link_to(conn, agg, board)
    return board, agg


async def test_dropping_a_rejected_link_reopens_the_job_it_closed(cfg, conn):
    board, agg = _setup_closed_via_link(conn)
    await closure.run_closure(cfg, conn, deps=_deps(), now=NOW)
    assert _row(conn, agg)["application_state"] == "posting_closed"

    a, b = rc.pair_of(agg, board)
    conn.execute(
        "INSERT INTO same_role_decisions (job_a, job_b, decision, decided_at) "
        "VALUES (?, ?, ?, 'x')",
        (a, b, rc.DIFFERENT),
    )
    conn.commit()
    await closure.run_closure(cfg, conn, deps=_deps(), now=NOW + timedelta(days=1))

    row = _row(conn, agg)
    assert row["application_state"] == ""  # restored, not stranded in posting_closed
    assert (row["posting_closed_at"] or "") == ""


async def test_split_pair_then_run_reopens_the_job_it_closed(cfg, conn):
    board, agg = _setup_closed_via_link(conn)
    await closure.run_closure(cfg, conn, deps=_deps(), now=NOW)
    assert _row(conn, agg)["application_state"] == "posting_closed"

    rc.split_pair(conn, agg, board, RADIUS, GEO)
    await closure.run_closure(cfg, conn, deps=_deps(), now=NOW + timedelta(days=1))

    assert _row(conn, agg)["application_state"] == ""


async def test_dropped_link_never_overrides_a_user_move(cfg, conn):
    board, agg = _setup_closed_via_link(conn)
    await closure.run_closure(cfg, conn, deps=_deps(), now=NOW)
    conn.execute("UPDATE jobs SET application_state='applied' WHERE id=?", (agg,))
    a, b = rc.pair_of(agg, board)
    conn.execute(
        "INSERT INTO same_role_decisions (job_a, job_b, decision, decided_at) "
        "VALUES (?, ?, ?, 'x')",
        (a, b, rc.DIFFERENT),
    )
    conn.commit()

    await closure.run_closure(cfg, conn, deps=_deps(), now=NOW + timedelta(days=1))

    assert _row(conn, agg)["application_state"] == "applied"


async def test_dropped_link_in_shadow_moves_nothing(cfg, conn):
    cfg.discovery.closure_auto = False
    board, agg = _setup_closed_via_link(conn)
    await closure.run_closure(cfg, conn, deps=_deps(), now=NOW)
    a, b = rc.pair_of(agg, board)
    conn.execute(
        "INSERT INTO same_role_decisions (job_a, job_b, decision, decided_at) "
        "VALUES (?, ?, ?, 'x')",
        (a, b, rc.DIFFERENT),
    )
    conn.commit()
    await closure.run_closure(cfg, conn, deps=_deps(), now=NOW + timedelta(days=1))
    row = _row(conn, agg)
    assert row["application_state"] == "" and (row["posting_closed_at"] or "") == ""


async def test_real_run_announces_a_close_a_shadow_run_already_stamped(cfg, conn):
    board = _job(conn, source="greenhouse:acme", ext="g1", review="kept", source_state="stale")

    shadow = await closure.run_closure(cfg, conn, deps=_deps(), shadow=True, now=NOW)
    assert [j["id"] for j in shadow.closed_kept] == [board]
    assert _row(conn, board)["application_state"] == ""

    real = await closure.run_closure(cfg, conn, deps=_deps(), now=NOW + timedelta(hours=1))
    assert [j["id"] for j in real.closed_kept] == [board]
    assert _row(conn, board)["application_state"] == "posting_closed"

    again = await closure.run_closure(cfg, conn, deps=_deps(), now=NOW + timedelta(days=1))
    assert again.closed_kept == []  # announced once, not every day

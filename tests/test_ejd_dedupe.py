"""Employer-JD lookups (issue #18): one search per identical role, and no searches
for hidden copies of a role already applied to. Neither may lose a valid lookup."""

from __future__ import annotations

import tempfile

import pytest

import tests.geo_fixture as geo_fixture
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from tests.test_ejd_u4_upgrade import JD, SNIPPET, Net, _cluster, _job, _row, _run, _status


@pytest.fixture
def cfg():
    c = BlastJobConfig()
    geo_fixture.apply_region(c)
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _region(conn, jid: int, region: str) -> None:
    conn.execute("UPDATE jobs SET geo_region=? WHERE id=?", (region, jid))
    conn.commit()


def _applied(conn, jid: int) -> None:
    conn.execute(
        "UPDATE jobs SET application_state='interview', applied_at='2026-09-13' WHERE id=?",
        (jid,),
    )
    conn.commit()


# --- one search per identical (company, title, region) -------------------------------------


async def test_identical_copies_share_one_search_and_each_gets_its_own_outcome(cfg, conn):
    ids = [_job(conn, ext=f"a{i}", review="surfaced", score=80) for i in range(3)]
    net = Net(hits=False)

    res = await _run(cfg, conn, network=True, deps=net.deps())

    assert net.probes == ["probe:Acme"]
    assert res.requests_used == 9  # one probe_company, not three
    for jid in ids:
        assert _status(res, jid) == "no_match"
        assert _row(conn, jid)["jd_lookup_status"] == "no_match"


async def test_a_shared_match_is_still_adopted_by_every_copy(cfg, conn):
    ids = [_job(conn, ext=f"a{i}", review="surfaced", score=80) for i in range(3)]
    net = Net()

    res = await _run(cfg, conn, network=True, deps=net.deps())

    assert net.probes == ["probe:Acme"] and net.fetches == 1
    assert sorted(res.upgraded) == sorted(ids)
    for jid in ids:
        assert _row(conn, jid)["raw_jd"].strip() == JD.strip()


async def test_a_copy_in_another_region_searches_on_its_own(cfg, conn):
    cid = _cluster(conn)
    a = _job(conn, ext="a1", review="surfaced", score=80, cluster_id=cid)
    b = _job(conn, ext="a2", review="surfaced", score=80, cluster_id=cid)
    _region(conn, b, "WI")
    net = Net(hits=False)

    res = await _run(cfg, conn, network=True, deps=net.deps())

    assert net.probes == ["probe:Acme", "probe:Acme"]
    assert _status(res, a) == "no_match" and _status(res, b) == "no_match"


async def test_a_failed_search_is_not_reused(cfg, conn):
    a = _job(conn, ext="a1", review="surfaced", score=90)
    b = _job(conn, ext="a2", review="surfaced", score=80)
    net = Net(fail=True)

    res = await _run(cfg, conn, network=True, deps=net.deps())

    assert net.probes == ["probe:Acme", "probe:Acme"]
    assert _status(res, a) == "transient" and _status(res, b) == "transient"


# --- hidden copies of an applied role -------------------------------------------------------


async def test_undecided_copies_of_an_applied_role_are_skipped_but_the_application_is_searched(
    cfg, conn
):
    cid = _cluster(conn)
    applied = _job(conn, source="linkedin", ext="l1", review="kept", cluster_id=cid)
    _applied(conn, applied)
    copy = _job(conn, ext="a1", review="queued", score=84, cluster_id=cid)
    _region(conn, copy, "WI")  # different inputs, so only the skip can stop its search
    net = Net(hits=False)

    res = await _run(cfg, conn, network=True, deps=net.deps())

    assert _status(res, copy) == "applied_role"
    assert _status(res, applied) == "no_match"
    assert net.probes == ["probe:Acme"]
    row = _row(conn, copy)
    assert (row["jd_lookup_status"] or "", row["jd_lookup_at"] or "") == ("", "")
    assert row["raw_jd"] == SNIPPET


async def test_a_drafted_only_role_is_not_skipped(cfg, conn):
    cid = _cluster(conn)
    drafted = _job(conn, source="linkedin", ext="l1", review="kept", cluster_id=cid)
    conn.execute("UPDATE jobs SET application_state='drafted' WHERE id=?", (drafted,))
    conn.commit()
    copy = _job(conn, ext="a1", review="queued", score=84, cluster_id=cid)

    res = await _run(cfg, conn, network=True, deps=Net(hits=False).deps())

    assert _status(res, copy) == "no_match"


async def test_a_copy_split_off_the_applied_role_is_searched_again(cfg, conn):
    cid = _cluster(conn)
    applied = _job(conn, source="linkedin", ext="l1", review="kept", cluster_id=cid)
    _applied(conn, applied)
    copy = _job(conn, ext="a1", review="queued", score=84, cluster_id=cid)
    first = await _run(cfg, conn, network=True, deps=Net(hits=False).deps())
    assert _status(first, copy) == "applied_role"

    conn.execute("UPDATE jobs SET cluster_id=NULL WHERE id=?", (copy,))
    conn.commit()
    again = await _run(cfg, conn, network=True, deps=Net(hits=False).deps())

    assert _status(again, copy) == "no_match"  # no backoff left over from the skip


async def test_an_explicit_request_for_a_copy_is_never_skipped(cfg, conn):
    cid = _cluster(conn)
    applied = _job(conn, source="linkedin", ext="l1", review="kept", cluster_id=cid)
    _applied(conn, applied)
    copy = _job(conn, ext="a1", review="queued", score=84, cluster_id=cid)

    res = await _run(
        cfg, conn, network=True, deps=Net(hits=False).deps(), job_ids=[copy],
        respect_backoff=False,
    )  # fmt: skip

    assert _status(res, copy) == "no_match"

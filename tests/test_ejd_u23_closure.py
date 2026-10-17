"""Employer-JD units U2/U3 (issue #17): the link SQL moved out of closure, what
closure records (`searched`, `reverted_jd`), and the revert of an adopted
description when its proof goes away ('not the same role', a dropped link).
"""

from __future__ import annotations

import tempfile
from datetime import datetime

import httpx
import pytest

import tests.geo_fixture as geo_fixture
from blastjob.core import closure
from blastjob.core import closure_links as cl
from blastjob.core import role_clusters as rc
from blastjob.core.closure_links import Link, LinkDeps
from blastjob.db import client as dbc
from blastjob.discovery.base import RawPosting
from blastjob.discovery.resolve import BoardHit
from blastjob.models.config import BlastJobConfig

NOW = datetime(2026, 10, 16, 8, 0, 0)
GEO = geo_fixture.region_cfg()
RADIUS = BlastJobConfig().discovery.commute_radius_miles
EMPLOYER_JD = "Run operations for the whole company. " * 30
SNIPPET = "Short aggregator snippet."


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


def _job(conn, *, source, ext, review="kept", source_state="active", cluster_id=None,
         jd=SNIPPET) -> int:  # fmt: skip
    jid = dbc.upsert_job(
        conn, source=source, external_id=ext, company="Acme", title="VP of Ops",
        geo_city="Oakbrook", geo_region="OH", geo_location_raw="Oakbrook, OH", raw_jd=jd,
    )  # fmt: skip
    conn.execute(
        "UPDATE jobs SET review_state=?, source_state=?, cluster_id=?, "
        "first_seen='2026-10-01T00:00:00', last_seen='2026-10-15T00:00:00' WHERE id=?",
        (review, source_state, cluster_id, jid),
    )
    conn.commit()
    return jid


def _row(conn, jid):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


def _link_to(conn, jid: int, other: int) -> None:
    o = _row(conn, other)
    conn.execute(
        "UPDATE jobs SET employer_link_source=?, employer_link_external_id=?, "
        "employer_link_url='' WHERE id=?",
        (o["source"], o["external_id"], jid),
    )
    conn.commit()


def _adopt(conn, jid: int, other: int) -> None:
    """Adopt the employer's text over `jid`, with `other` as the proof."""
    o = _row(conn, other)
    snap = dbc.jd_snapshot(conn, jid)
    assert snap is not None
    ok = dbc.set_employer_jd(
        conn,
        snap,
        text=EMPLOYER_JD,
        jd_source=dbc.EMPLOYER_JD_PREFIX + o["source"],
        jd_source_ref=o["external_id"],
        now="2026-10-16T08:00:00",
    )
    assert ok
    conn.commit()


def _no_net() -> LinkDeps:
    async def none_list(*_a):
        return []

    async def no_check(_link):
        raise AssertionError("no network probe expected")

    return LinkDeps(
        probe_company=lambda *_a: [],
        probe_workday=lambda *_a: [],
        probe_dayforce=lambda *_a: [],
        fetch_board=none_list,
        check_link=no_check,
    )


# --- the moved link SQL -------------------------------------------------------------


def test_save_link_writes_the_link_and_never_commits(conn):
    jid = _job(conn, source="linkedin", ext="l1")

    n = cl.save_link(conn, jid, Link("greenhouse:acme", "g1", "https://x/g1"))

    assert n == 1 and conn.in_transaction
    conn.rollback()
    assert (_row(conn, jid)["employer_link_source"] or "") == ""


def test_save_link_never_overwrites_the_users_manual_link(conn):
    jid = _job(conn, source="linkedin", ext="l1")
    conn.execute("UPDATE jobs SET employer_link_source='manual' WHERE id=?", (jid,))
    conn.commit()

    assert cl.save_link(conn, jid, Link("greenhouse:acme", "g1", "")) == 0
    assert _row(conn, jid)["employer_link_source"] == "manual"


def test_save_link_skips_a_row_with_a_manual_verdict(conn):
    jid = _job(conn, source="linkedin", ext="l1")
    conn.execute("UPDATE jobs SET posting_check_manual=1 WHERE id=?", (jid,))
    conn.commit()

    assert cl.save_link(conn, jid, Link("greenhouse:acme", "g1", "")) == 0


def test_rejected_link_is_true_only_for_a_different_decision(conn):
    board = _job(conn, source="greenhouse:acme", ext="g1", review="new")
    agg = _job(conn, source="linkedin", ext="l1")
    link = Link("greenhouse:acme", "g1", "")
    assert cl.rejected_link(conn, agg, link) is False
    rc.record_decision(conn, agg, board, rc.SAME)
    assert cl.rejected_link(conn, agg, link) is False
    rc.record_decision(conn, agg, board, rc.DIFFERENT)
    assert cl.rejected_link(conn, agg, link) is True
    assert cl.rejected_link(conn, agg, Link("greenhouse:acme", "unheld", "")) is False


def test_link_still_proof_follows_the_sibling_rule(conn):
    cid = rc._new_cluster(conn, "t")
    board = _job(conn, source="greenhouse:acme", ext="g1", review="new", cluster_id=cid)
    agg = _job(conn, source="linkedin", ext="l1", cluster_id=cid)
    row = _row(conn, agg)

    assert cl.link_still_proof(conn, row, board, RADIUS, GEO) is True  # same metro
    rc.record_decision(conn, agg, board, rc.DIFFERENT)
    assert cl.link_still_proof(conn, row, board, RADIUS, GEO) is False


def test_link_still_proof_for_an_unclustered_undecided_link_is_true(conn):
    board = _job(conn, source="greenhouse:acme", ext="g1", review="new")
    agg = _job(conn, source="linkedin", ext="l1")
    assert cl.link_still_proof(conn, _row(conn, agg), board, RADIUS, GEO) is True


# --- ClosureResult.searched ------------------------------------------------------------


def _board_deps(ids: list[str]) -> LinkDeps:
    async def fetch_board(*_a):
        return [RawPosting(source="greenhouse:acme", external_id=i, title="VP of Ops") for i in ids]

    return LinkDeps(
        probe_company=lambda _n: [BoardHit("Acme", "greenhouse", "acme", 4)],
        probe_workday=lambda *_a: [],
        probe_dayforce=lambda *_a: [],
        fetch_board=fetch_board,
        check_link=_no_net().check_link,
    )


async def test_closure_reports_the_rows_it_searched(cfg, conn):
    searched = _job(conn, source="adzuna", ext="a1")
    agency = _job(conn, source="adzuna", ext="a2")
    conn.execute("UPDATE jobs SET company='Northside Staffing' WHERE id=?", (agency,))
    conn.commit()

    res = await closure.run_closure(cfg, conn, deps=_board_deps(["g1"]), now=NOW)

    assert res.searched == {searched}
    assert res.reverted_jd == []


async def test_a_row_skipped_by_the_search_cap_is_not_reported_searched(cfg, conn, monkeypatch):
    monkeypatch.setattr(closure, "MAX_LINK_SEARCHES_PER_RUN", 1)
    first = _job(conn, source="adzuna", ext="a1")
    second = _job(conn, source="adzuna", ext="a2")

    res = await closure.run_closure(cfg, conn, deps=_board_deps([]), now=NOW)

    assert len(res.searched) == 1 and res.searched <= {first, second}


# --- reverting an adopted description ----------------------------------------------------


async def test_closure_dropping_a_link_reverts_the_description_adopted_over_it(cfg, conn):
    cid = rc._new_cluster(conn, "t")
    board = _job(conn, source="greenhouse:acme", ext="g1", review="new", cluster_id=cid)
    agg = _job(conn, source="adzuna", ext="a1", cluster_id=cid)
    _link_to(conn, agg, board)
    _adopt(conn, agg, board)
    assert _row(conn, agg)["raw_jd"].startswith("Run operations")
    rc.record_decision(conn, agg, board, rc.DIFFERENT)  # proof gone, link still stored

    res = await closure.run_closure(cfg, conn, deps=_no_net(), now=NOW)

    row = _row(conn, agg)
    assert (row["employer_link_source"] or "") == ""
    assert row["raw_jd"] == SNIPPET and (row["jd_source"] or "") == ""
    assert res.reverted_jd == [agg]


async def test_dropping_a_link_leaves_a_description_adopted_over_another_link(cfg, conn):
    cid = rc._new_cluster(conn, "t")
    board = _job(conn, source="greenhouse:acme", ext="g1", review="new", cluster_id=cid)
    other = _job(conn, source="greenhouse:acme", ext="g2", review="new")
    agg = _job(conn, source="adzuna", ext="a1", cluster_id=cid)
    _link_to(conn, agg, board)
    _adopt(conn, agg, other)  # text came from g2, not the link being dropped
    rc.record_decision(conn, agg, board, rc.DIFFERENT)

    res = await closure.run_closure(cfg, conn, deps=_no_net(), now=NOW)

    assert _row(conn, agg)["raw_jd"].startswith("Run operations")
    assert res.reverted_jd == []


def test_clear_links_between_reverts_both_ways_and_returns_the_ids(conn):
    a = _job(conn, source="adzuna", ext="a1")
    b = _job(conn, source="adzuna", ext="a2")
    _adopt(conn, a, b)
    _adopt(conn, b, a)

    reverted = rc.clear_links_between(conn, a, b)

    assert sorted(reverted) == sorted([a, b])
    for jid in (a, b):
        assert _row(conn, jid)["raw_jd"] == SNIPPET
        assert (_row(conn, jid)["jd_source"] or "") == ""


def test_split_pair_surfaces_the_reverted_ids(conn):
    cid = rc._new_cluster(conn, "t")
    board = _job(conn, source="greenhouse:acme", ext="g1", review="new", cluster_id=cid)
    agg = _job(conn, source="adzuna", ext="a1", cluster_id=cid)
    _link_to(conn, agg, board)
    _adopt(conn, agg, board)

    reverted = rc.split_pair(conn, agg, board, RADIUS, GEO)

    assert reverted == [agg]
    assert _row(conn, agg)["raw_jd"] == SNIPPET
    assert (_row(conn, agg)["employer_link_source"] or "") == ""


def test_split_pair_keeps_a_pasted_description(conn):
    cid = rc._new_cluster(conn, "t")
    board = _job(conn, source="greenhouse:acme", ext="g1", review="new", cluster_id=cid)
    agg = _job(conn, source="adzuna", ext="a1", cluster_id=cid)
    dbc.set_pasted_jd(conn, agg, "Pasted by the owner. " * 40)
    conn.commit()

    assert rc.split_pair(conn, agg, board, RADIUS, GEO) == []
    assert _row(conn, agg)["raw_jd"].startswith("Pasted by the owner")


def test_a_failure_after_the_revert_rolls_the_whole_split_back(conn, monkeypatch):
    cid = rc._new_cluster(conn, "t")
    board = _job(conn, source="greenhouse:acme", ext="g1", review="new", cluster_id=cid)
    agg = _job(conn, source="adzuna", ext="a1", cluster_id=cid)
    _link_to(conn, agg, board)
    _adopt(conn, agg, board)
    before = _row(conn, agg)["raw_jd"]

    def boom(*_a, **_k):
        raise RuntimeError("injected")

    monkeypatch.setattr(rc, "_new_cluster", boom)
    with pytest.raises(RuntimeError):
        rc.split_pair(conn, agg, board, RADIUS, GEO)
    conn.rollback()  # what the caller's transaction does on an exception

    row = _row(conn, agg)
    assert row["raw_jd"] == before and row["jd_source"].startswith("employer:")
    assert row["employer_link_external_id"] == "g1"
    assert conn.execute("SELECT COUNT(*) FROM same_role_decisions").fetchone()[0] == 0


# --- the real Workday detail fetch ------------------------------------------------------------


async def test_fetch_workday_detail_reads_title_region_and_text():
    url = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Oakbrook-OH/VP-of-Ops_R1"
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "jobPostingInfo": {
                    "title": "VP of Ops",
                    "location": "Oakbrook, OH",
                    "jobDescription": "<p>Run ops.</p>",
                    "jobReqId": "R1",
                }
            },
        )

    got = await cl.fetch_workday_detail(
        "acme/wd5/careers", url, transport=httpx.MockTransport(handler)
    )

    assert got is not None
    assert (got.title, got.region, got.description) == ("VP of Ops", "OH", "Run ops.")
    assert seen == [
        "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/careers/job/Oakbrook-OH/VP-of-Ops_R1"
    ]


async def test_fetch_workday_detail_is_none_on_a_410():
    url = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Oakbrook-OH/VP-of-Ops_R1"
    transport = httpx.MockTransport(lambda r: httpx.Response(410))
    assert await cl.fetch_workday_detail("acme/wd5/careers", url, transport=transport) is None


async def test_fetch_workday_detail_raises_on_a_server_error():
    url = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Oakbrook-OH/VP-of-Ops_R1"
    transport = httpx.MockTransport(lambda r: httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        await cl.fetch_workday_detail("acme/wd5/careers", url, transport=transport)

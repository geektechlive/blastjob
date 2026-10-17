"""Employer-JD review fixes F4, F8, F9, S1, S4: shadow closure leaves adopted text
alone, stale-kept rotation, the daily note counts a row once, a lookalike ATS
tenant cannot supply text, and the Workday detail is size-capped.
"""

from __future__ import annotations

import datetime
import json

import httpx
import pytest

from blastjob import daily_employer_jd as dej
from blastjob.core import closure
from blastjob.core import closure_links as cl
from blastjob.core import employer_jd as ej
from blastjob.core import page_fetch as pf
from blastjob.core import page_links as pl
from blastjob.core import role_clusters as rc
from blastjob.core.closure_links import Link
from blastjob.matching import rescore
from tests import test_ejd_u23_closure as u23
from tests.test_ejd_u23_closure import NOW as CLOSURE_NOW
from tests.test_ejd_u23_closure import SNIPPET, _adopt, _link_to, _no_net, _row
from tests.test_ejd_u23_closure import _job as closure_job

cfg = u23.cfg  # the fixtures, shared with the U2/U3 tests
conn = u23.conn


# --- F4: shadow closure does not revert adopted text -----------------------------------------


def _split_adoption(conn):
    cid = rc._new_cluster(conn, "t")
    board = closure_job(conn, source="greenhouse:acme", ext="g1", review="new", cluster_id=cid)
    agg = closure_job(conn, source="adzuna", ext="a1", cluster_id=cid)
    _link_to(conn, agg, board)
    _adopt(conn, agg, board)
    rc.record_decision(conn, agg, board, rc.DIFFERENT)  # the proof is gone
    conn.commit()
    return agg


async def test_shadow_closure_reports_the_revert_it_would_make_and_makes_none(cfg, conn):
    agg = _split_adoption(conn)

    res = await closure.run_closure(cfg, conn, deps=_no_net(), shadow=True, now=CLOSURE_NOW)

    row = _row(conn, agg)
    assert row["raw_jd"].startswith("Run operations") and row["jd_source"]  # text stays
    assert res.would_revert_jd == [agg] and res.reverted_jd == []
    assert (row["employer_link_source"] or "") == ""  # the link drop is the existing behavior


async def test_a_real_closure_run_still_reverts(cfg, conn):
    agg = _split_adoption(conn)
    cfg.discovery.closure_auto = False  # not shadow: only the state moves are off

    res = await closure.run_closure(cfg, conn, deps=_no_net(), now=CLOSURE_NOW)

    assert _row(conn, agg)["raw_jd"] == SNIPPET
    assert res.reverted_jd == [agg] and res.would_revert_jd == []


# --- F8: stale kept rows rotate by when their text last changed ---------------------------------


def test_stale_kept_ids_take_the_oldest_text_first_not_the_lowest_id(conn):
    ids = [closure_job(conn, source="adzuna", ext=f"a{i}") for i in range(12)]
    for n, jid in enumerate(ids):  # the LOWEST ids carry the NEWEST text
        conn.execute(
            "UPDATE jobs SET score_stale=1, jd_fetched_at=? WHERE id=?",
            (f"2026-10-{20 - n:02d}T00:00:00", jid),
        )
    conn.commit()

    got = rescore.stale_kept_ids(conn, limit=10)

    assert got == list(reversed(ids))[:10]


def test_stale_kept_ids_put_a_blank_fetch_stamp_first(conn):
    a = closure_job(conn, source="adzuna", ext="a1")
    b = closure_job(conn, source="adzuna", ext="a2")
    conn.execute(
        "UPDATE jobs SET score_stale=1, jd_fetched_at='2026-10-01T00:00:00' WHERE id=?", (a,)
    )
    conn.execute("UPDATE jobs SET score_stale=1, jd_fetched_at='' WHERE id=?", (b,))
    conn.commit()

    assert rescore.stale_kept_ids(conn, limit=1) == [b]


# --- F9: the daily note counts a row once ---------------------------------------------------------


def _outcome(job_id: int, status: str, via: str) -> ej.RowOutcome:
    return ej.RowOutcome(job_id, "Acme", "VP of Ops", 80, "kept", status, via=via)


def test_the_note_counts_a_dry_run_row_once_across_the_free_and_network_steps():
    free = ej.UpgradeResult(outcomes=[_outcome(5, "would_upgrade", "copy")])
    net = ej.UpgradeResult(
        outcomes=[_outcome(5, "would_upgrade", "search"), _outcome(6, "would_upgrade", "search")]
    )

    note = dej._note(free, net, rescored=0, rescore_failed=0)

    assert "would store 2" in note and "would store 3" not in note


def test_the_note_lets_the_network_outcome_win_for_a_row_in_both_steps():
    free = ej.UpgradeResult(outcomes=[_outcome(5, "sibling_not_full", "copy")])
    net = ej.UpgradeResult(outcomes=[_outcome(5, "upgraded", "search")])

    note = dej._note(free, net, rescored=0, rescore_failed=0)

    assert "1 stored" in note and "sibling_not_full" not in note


def test_the_note_counts_a_revert_in_both_steps_once():
    free = ej.UpgradeResult(outcomes=[_outcome(5, "reverted", "revalidate")], reverted=[5])
    net = ej.UpgradeResult(outcomes=[_outcome(5, "reverted", "revalidate")], reverted=[5])

    assert dej._note(free, net, rescored=0, rescore_failed=0).count("1 reverted") == 1


# --- S1: text adoption binds the page to the company's own ATS tenant -----------------------------

PAGE_URL = "https://careers.acme.com/jobs/1"
TODAY = datetime.date(2026, 10, 16)
COMPANY, TITLE, REGION = "Acme", "VP of Operations", "OH"
TEXT = "Lead the operations organization for the whole company. " * 20


def _page_html(form: str) -> str:
    ld = {
        "@context": "https://schema.org", "@type": "JobPosting", "title": TITLE,
        "url": PAGE_URL, "validThrough": "2099-12-31", "description": TEXT,
        "hiringOrganization": {"@type": "Organization", "name": "Acme Inc"},
        "jobLocation": {"@type": "Place", "address": {"addressRegion": "OH"}},
    }  # fmt: skip
    return (
        f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head>'
        f'<body><form action="{form}"></form></body></html>'
    )


def _fetch(html: str):
    async def fetch(_url: str):
        return pf.Page(status=200, url=PAGE_URL, html=html, cross_host=False)

    return fetch


LINK = Link("page:careers.acme.com", PAGE_URL, PAGE_URL)


@pytest.mark.parametrize("tenant", ["acmeholdings", "acme-evil-staffing", "acmeevil"])
async def test_a_lookalike_tenant_cannot_supply_text_but_still_serves_closure(tenant):
    html = _page_html(f"https://{tenant}.applytojob.com/apply/1")
    fetch = _fetch(html)

    text = await pl.page_description(LINK, COMPANY, TITLE, REGION, fetch=fetch, today=TODAY)
    still_open = await pl.check_page(LINK, COMPANY, TITLE, REGION, fetch=fetch, today=TODAY)

    assert text == (None, "company_mismatch")
    assert still_open is True  # closure keeps its looser, unchanged rule


@pytest.mark.parametrize("tenant", ["acme", "acme1", "acme22"])
async def test_the_companys_own_tenant_still_supplies_text(tenant):
    html = _page_html(f"https://{tenant}.applytojob.com/apply/1")

    text, reason = await pl.page_description(
        LINK, COMPANY, TITLE, REGION, fetch=_fetch(html), today=TODAY
    )

    assert reason == "ok" and text is not None


# --- S4: the Workday detail body is capped --------------------------------------------------------


async def test_a_workday_detail_over_the_size_cap_raises():
    url = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Oakbrook-OH/VP-of-Ops_R1"
    body = json.dumps(
        {"jobPostingInfo": {"title": "VP", "jobDescription": "x" * (cl.MAX_DETAIL_BYTES + 10)}}
    )
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=body.encode()))

    with pytest.raises(ValueError, match="too large"):
        await cl.fetch_workday_detail("acme/wd5/careers", url, transport=transport)


async def test_a_workday_detail_just_under_the_cap_still_parses():
    url = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Oakbrook-OH/VP-of-Ops_R1"
    body = json.dumps({"jobPostingInfo": {"title": "VP", "jobDescription": "<p>ok</p>"}})
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=body.encode()))

    got = await cl.fetch_workday_detail("acme/wd5/careers", url, transport=transport)

    assert got is not None and got.description == "ok"

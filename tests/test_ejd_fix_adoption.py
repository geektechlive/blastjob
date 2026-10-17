"""Employer-JD review fixes F1, F2, F5, F6, F7, F10 and the end-to-end gap: what may
supply a description (title/company binding, proof re-run on revalidation), what a
refused lookup may leave behind, and the network re-verify of adoptions.

Everything is injected; nothing here touches the network.
"""

from __future__ import annotations

import datetime

import pytest

from blastjob.core import employer_jd as ej
from blastjob.core import page_fetch as pf
from blastjob.core import page_links as pl
from blastjob.core import role_clusters as rc
from blastjob.core.closure_links import Link, LinkDeps
from blastjob.discovery.base import RawPosting
from blastjob.discovery.resolve import BoardHit
from tests import test_ejd_u4_upgrade as u4
from tests.test_ejd_u4_upgrade import (
    BOARD,
    JD,
    JD2,
    NO_TITLE_JD,
    NOW,
    SNIPPET,
    Net,
    _cluster,
    _dump,
    _job,
    _pair,
    _row,
    _run,
    _status,
)

cfg = u4.cfg  # the fixtures, shared with the U4 tests
conn = u4.conn

# --- F1: the held posting must still BE this role ---------------------------------------


async def test_a_retitled_sibling_is_not_copied(cfg, conn):
    agg, board = _pair(conn)
    conn.execute("UPDATE jobs SET title='Chief Financial Officer' WHERE id=?", (board,))
    conn.commit()

    res = await _run(cfg, conn)

    assert _row(conn, agg)["raw_jd"] == SNIPPET and res.upgraded == []


async def test_a_retitled_sibling_the_user_confirmed_is_still_copied(cfg, conn):
    agg, board = _pair(conn)
    conn.execute("UPDATE jobs SET title='Chief Financial Officer' WHERE id=?", (board,))
    rc.record_decision(conn, agg, board, rc.SAME)
    conn.commit()

    res = await _run(cfg, conn)

    assert _status(res, agg) == "upgraded"


async def test_a_sibling_of_another_company_is_not_copied(cfg, conn):
    agg, board = _pair(conn)
    conn.execute("UPDATE jobs SET company='Globex' WHERE id=?", (board,))
    conn.commit()

    res = await _run(cfg, conn)

    assert _row(conn, agg)["raw_jd"] == SNIPPET and res.upgraded == []


async def test_a_stored_link_to_a_retitled_held_row_is_not_copied(cfg, conn):
    agg, board = _pair(conn)
    conn.execute(
        "UPDATE jobs SET employer_link_source=?, employer_link_external_id='g1' WHERE id=?",
        (BOARD, agg),
    )
    conn.execute("UPDATE jobs SET title='Chief Financial Officer' WHERE id=?", (board,))
    conn.commit()

    res = await _run(cfg, conn)

    assert _row(conn, agg)["raw_jd"] == SNIPPET and res.upgraded == []


@pytest.mark.parametrize("new_text", [JD, JD2], ids=["text-unchanged", "text-revised"])
async def test_a_held_row_retitled_after_adoption_is_reverted_not_revised(cfg, conn, new_text):
    agg, board = _pair(conn, review="surfaced", score=70)
    await _run(cfg, conn)
    assert _row(conn, agg)["jd_source"] == "employer:greenhouse:acme"
    conn.execute(
        "UPDATE jobs SET title='Chief Financial Officer', raw_jd=? WHERE id=?", (new_text, board)
    )
    conn.commit()

    res = await _run(cfg, conn)

    row = _row(conn, agg)
    assert row["raw_jd"] == SNIPPET and row["jd_source"] == ""
    assert res.reverted == [agg] and _status(res, agg) == "reverted"


async def test_a_revision_that_is_not_a_jd_for_this_role_is_reverted(cfg, conn):
    agg, board = _pair(conn, review="surfaced", score=70)
    await _run(cfg, conn)
    conn.execute("UPDATE jobs SET raw_jd=? WHERE id=?", (NO_TITLE_JD, board))
    conn.commit()

    res = await _run(cfg, conn)

    # The reverted row is a candidate again and is refused afresh in the same run.
    assert _row(conn, agg)["raw_jd"] == SNIPPET and res.reverted == [agg]
    assert [o.status for o in res.outcomes if o.job_id == agg] == ["reverted", "not_a_jd"]


# --- F5: revalidation re-runs the proof the adoption used ---------------------------------


async def test_a_row_moved_out_of_its_cluster_by_a_third_party_is_reverted(cfg, conn):
    agg, board = _pair(conn, review="surfaced", score=70)
    await _run(cfg, conn)
    assert _row(conn, agg)["jd_source"] == "employer:greenhouse:acme"
    # A second same-titled board row in the metro makes the company+title route
    # ambiguous, so once the sibling link is gone nothing re-proves g1.
    _job(conn, source=BOARD, ext="g2", review="new", jd=JD2)
    conn.execute("UPDATE jobs SET cluster_id=? WHERE id=?", (_cluster(conn), agg))
    conn.commit()

    res = await _run(cfg, conn)

    row = _row(conn, agg)
    assert row["raw_jd"] == SNIPPET and row["jd_source"] == ""
    assert res.reverted == [agg]


async def test_a_proof_for_a_different_posting_does_not_keep_the_adoption(cfg, conn):
    agg, board = _pair(conn, review="surfaced", score=70)
    await _run(cfg, conn)
    # The cluster now proves g2, not g1, which supplied the text.
    other = _job(conn, source=BOARD, ext="g2", review="new", jd=JD2,
                 cluster_id=_row(conn, agg)["cluster_id"])  # fmt: skip
    conn.execute("UPDATE jobs SET cluster_id=? WHERE id=?", (_cluster(conn), board))
    conn.commit()
    assert other != board

    res = await _run(cfg, conn)

    # Reverted, then re-adopted from the posting the cluster really proves.
    assert res.reverted == [agg] and _row(conn, agg)["jd_source_ref"] == "g2"


async def test_a_stored_link_to_the_ref_keeps_the_adoption_after_a_cluster_move(cfg, conn):
    agg, board = _pair(conn, review="surfaced", score=70)
    await _run(cfg, conn)
    _job(conn, source=BOARD, ext="g2", review="new", jd=JD2)
    conn.execute(
        "UPDATE jobs SET cluster_id=?, employer_link_source=?, employer_link_external_id='g1' "
        "WHERE id=?",
        (_cluster(conn), BOARD, agg),
    )
    conn.commit()

    res = await _run(cfg, conn)

    assert _row(conn, agg)["jd_source"] == "employer:greenhouse:acme" and res.reverted == []


# --- F2: a refused link save never gates the text ---------------------------------------------


async def test_a_row_with_a_manual_verdict_still_takes_the_text(cfg, conn):
    agg = _job(conn, review="kept")
    conn.execute("UPDATE jobs SET posting_check_manual=1 WHERE id=?", (agg,))
    conn.commit()

    res = await _run(cfg, conn, network=True, deps=Net().deps())

    row = _row(conn, agg)
    assert _status(res, agg) == "upgraded" and row["raw_jd"].strip() == JD.strip()
    assert (row["employer_link_source"] or "") == ""  # the link save is best-effort
    assert row["jd_lookup_status"] == "found"


async def test_a_match_the_cluster_cannot_prove_is_a_backed_off_refusal(cfg, conn):
    # g1 is this row's cluster sibling but in another metro: sibling proof fails,
    # so the adoption can never land. It must back off, not be searched daily.
    agg, _board = _pair(conn, board_city="Rivermont")
    net = Net()

    res = await _run(cfg, conn, network=True, deps=net.deps())
    again = await _run(
        cfg, conn, network=True, deps=net.deps(), now=NOW + datetime.timedelta(days=1)
    )

    row = _row(conn, agg)
    assert _status(res, agg) == "no_proof" and row["raw_jd"] == SNIPPET
    assert row["jd_lookup_status"] == "no_match"
    assert _status(again, agg) == "backoff" and len(net.probes) == 1


# --- F7: a drifted re-check leaves no link behind -----------------------------------------------


def _drifting_deps(second_title: str, second_text: str = JD) -> LinkDeps:
    """A board whose first listing matches the job and whose re-check does not."""
    calls = {"n": 0}

    async def fetch_board(ats, token, title):
        calls["n"] += 1
        first = calls["n"] == 1
        return [
            RawPosting(
                source=f"greenhouse:{token}",
                external_id="g1",
                title="VP of Ops" if first else second_title,
                url="https://x/g1",
                description="" if first else second_text,
                region="OH",
            )  # fmt: skip
        ]

    return LinkDeps(
        probe_company=lambda name: [BoardHit(name, "greenhouse", "acme", 4)],
        probe_workday=lambda *_a: [],
        probe_dayforce=lambda *_a: [],
        fetch_board=fetch_board,
    )


@pytest.mark.parametrize("reason", ["title_drift"])
async def test_a_title_drift_on_the_recheck_saves_no_link(cfg, conn, reason):
    agg = _job(conn, review="kept")

    res = await _run(cfg, conn, network=True, deps=_drifting_deps("Janitor"))

    row = _row(conn, agg)
    assert _status(res, agg) == reason and (row["employer_link_source"] or "") == ""
    assert row["jd_lookup_status"] == "no_match" and row["raw_jd"] == SNIPPET


async def test_a_blank_recheck_keeps_the_link_for_closure(cfg, conn):
    agg = _job(conn, review="kept")

    res = await _run(cfg, conn, network=True, deps=_drifting_deps("VP of Ops", second_text=""))

    assert _status(res, agg) == "no_text"
    assert _row(conn, agg)["employer_link_source"] == BOARD


# --- F10: a sibling that is not full is not the end of the road ----------------------------------


async def test_a_pasted_sibling_falls_through_to_the_network_when_it_is_on(cfg, conn):
    agg, board = _pair(conn)
    conn.execute("UPDATE jobs SET jd_pasted_at='2026-10-02' WHERE id=?", (board,))
    conn.commit()

    res = await _run(cfg, conn, network=True, deps=Net().deps())

    assert _status(res, agg) == "upgraded"
    assert _row(conn, agg)["raw_jd"].strip() == JD.strip()


async def test_a_pasted_sibling_with_the_network_off_stays_sibling_not_full(cfg, conn):
    agg, board = _pair(conn)
    conn.execute("UPDATE jobs SET jd_pasted_at='2026-10-02' WHERE id=?", (board,))
    conn.commit()

    res = await _run(cfg, conn, network=False)

    assert _status(res, agg) == "sibling_not_full"


# --- F6: network-proven adoptions are re-judged, capped and budgeted ------------------------------


def _adopted_unheld(conn, *, ext="gX", fetched="2026-09-01T08:00:00", company="Acme", n=1) -> int:
    """An aggregator row holding employer text adopted from a posting NOT in the DB."""
    agg = _job(conn, ext=f"a{n}", company=company, review="kept")
    conn.execute(
        "UPDATE jobs SET raw_jd=?, jd_source=?, jd_source_ref=?, jd_prior=?, jd_fetched_at=?, "
        "employer_link_source=?, employer_link_external_id=?, employer_link_url='https://x/' || ? "
        "WHERE id=?",
        (JD, "employer:" + BOARD, ext, SNIPPET, fetched, BOARD, ext, ext, agg),
    )
    conn.commit()
    return agg


async def test_a_network_adoption_whose_posting_drifted_is_reverted(cfg, conn):
    agg = _adopted_unheld(conn)

    res = await _run(cfg, conn, network=True, deps=Net(ext="gX", title="Janitor").deps())

    row = _row(conn, agg)
    assert row["raw_jd"] == SNIPPET and row["jd_source"] == ""
    assert res.reverted == [agg] and _status(res, agg) == "reverted"


async def test_a_network_adoption_that_still_verifies_is_kept_and_rotated(cfg, conn):
    agg = _adopted_unheld(conn)

    res = await _run(cfg, conn, network=True, deps=Net(ext="gX").deps())

    row = _row(conn, agg)
    assert row["jd_source"] == "employer:greenhouse:acme" and res.reverted == []
    assert row["jd_fetched_at"] == NOW.isoformat(timespec="seconds")  # moves to the back


async def test_a_gone_network_adoption_is_reverted(cfg, conn):
    agg = _adopted_unheld(conn)

    res = await _run(cfg, conn, network=True, deps=Net(ext="other").deps())  # gX not listed

    assert _row(conn, agg)["raw_jd"] == SNIPPET and res.reverted == [agg]


async def test_a_failed_reverify_leaves_the_adoption_alone(cfg, conn):
    agg = _adopted_unheld(conn)

    async def boom(*_a):
        raise RuntimeError("down")

    deps = LinkDeps(fetch_board=boom, probe_company=lambda *_a: [])
    res = await _run(cfg, conn, network=True, deps=deps)

    assert _row(conn, agg)["jd_source"] == "employer:greenhouse:acme" and res.reverted == []


async def test_reverify_is_capped_oldest_first(cfg, conn, monkeypatch):
    monkeypatch.setattr(ej, "REVERIFY_PER_RUN", 1)
    old = _adopted_unheld(conn, ext="gX", fetched="2026-08-01T00:00:00", n=1)
    new = _adopted_unheld(conn, ext="gX", fetched="2026-10-01T00:00:00", n=2)

    res = await _run(cfg, conn, network=True, deps=Net(ext="gX", title="Janitor").deps())

    assert res.reverted == [old] and _row(conn, new)["jd_source"] == "employer:greenhouse:acme"


async def test_reverify_stops_cleanly_when_the_budget_is_spent(cfg, conn):
    agg = _adopted_unheld(conn)

    res = await _run(cfg, conn, network=True, deps=Net(ext="gX").deps(), request_limit=0)

    assert res.requests_used == 0 and _row(conn, agg)["jd_source"] == "employer:greenhouse:acme"


async def test_reverify_with_write_false_reports_and_writes_nothing(cfg, conn):
    agg = _adopted_unheld(conn)
    before = _dump(conn)

    res = await _run(
        cfg, conn, network=True, write=False, deps=Net(ext="gX", title="Janitor").deps()
    )

    assert _dump(conn) == before and _status(res, agg) == "would_revert"


async def test_the_network_off_never_reverifies(cfg, conn):
    agg = _adopted_unheld(conn)
    net = Net(ext="gX", title="Janitor")

    await _run(cfg, conn, network=False, deps=net.deps())

    assert net.calls == [] and _row(conn, agg)["jd_source"] == "employer:greenhouse:acme"


# --- end to end with a Jina key: Workday, Dayforce and the careers-page fallback ------------------

PAGE_HOST = "careers.acme.com"
PAGE_URL = f"https://{PAGE_HOST}/apply/jobs/details/AbC123"
WORKDAY_URL = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Oakbrook-OH/VP-of-Ops_R1"


def _no(*_a):
    return []


def test_the_ejd_fix_page_helpers_are_importable():
    assert pf.Page and pl.page_description


async def test_a_workday_board_is_probed_matched_and_its_detail_adopted(cfg, conn):
    agg = _job(conn, review="kept")
    probes: list[str] = []

    async def fetch_board(ats, token, title):
        probes.append(f"list:{ats}")
        return [RawPosting(source="workday:acme", external_id="R1", title="VP of Ops",
                           url=WORKDAY_URL, region="OH")]  # fmt: skip

    async def detail(token, url):
        probes.append("detail")
        return RawPosting(source="workday:acme", external_id="R1", title="VP of Ops",
                          url=url, description=JD, region="OH")  # fmt: skip

    deps = LinkDeps(
        jina_api_key="k",
        probe_company=_no,
        probe_workday=lambda name, key: [BoardHit(name, "workday", "acme/wd5/careers", 3)],
        probe_dayforce=_no,
        fetch_board=fetch_board,
        fetch_workday_detail=detail,
    )

    res = await _run(cfg, conn, network=True, deps=deps)

    row = _row(conn, agg)
    assert _status(res, agg) == "upgraded" and row["raw_jd"].strip() == JD.strip()
    assert (row["jd_source"], row["jd_source_ref"]) == ("employer:workday:acme", "R1")
    assert row["employer_link_source"] == "workday:acme" and probes == ["list:workday", "detail"]


async def test_a_dayforce_board_is_probed_and_its_listing_text_adopted(cfg, conn):
    agg = _job(conn, review="kept")

    async def fetch_board(ats, token, title):
        return [RawPosting(source="dayforce:acme", external_id="77", title="VP of Ops",
                           url="https://jobs.dayforcehcm.com/en-US/acme/CANDIDATEPORTAL/jobs/77",
                           description=JD, region="OH")]  # fmt: skip

    deps = LinkDeps(
        jina_api_key="k",
        probe_company=_no,
        probe_workday=_no,
        probe_dayforce=lambda name, key: [BoardHit(name, "dayforce", "acme/CANDIDATEPORTAL", 3)],
        fetch_board=fetch_board,
    )

    res = await _run(cfg, conn, network=True, deps=deps, request_limit=500)

    row = _row(conn, agg)
    assert _status(res, agg) == "upgraded"
    assert (row["jd_source"], row["jd_source_ref"]) == ("employer:dayforce:acme", "77")


def _page_html() -> str:
    import json

    ld = {
        "@context": "https://schema.org", "@type": "JobPosting", "title": "VP of Ops",
        "url": PAGE_URL, "validThrough": "2099-12-31", "description": JD,
        "hiringOrganization": {"@type": "Organization", "name": "Acme Inc"},
        "jobLocation": {"@type": "Place", "address": {"addressRegion": "OH"}},
    }  # fmt: skip
    return (
        f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head>'
        '<body><form action="https://acme1.applytojob.com/apply/AbC123"></form></body></html>'
    )


async def test_the_careers_page_fallback_adopts_the_trusted_postings_text(cfg, conn):
    agg = _job(conn, review="kept")
    link = Link(f"page:{PAGE_HOST}", PAGE_URL, PAGE_URL)

    async def fetch(url: str):
        return pf.Page(status=200, url=PAGE_URL, html=_page_html(), cross_host=False)

    async def find_page(company, title, region, key):
        return link

    async def page_description(lk, company, title, region):
        return await pl.page_description(
            lk, company, title, region, fetch=fetch, today=datetime.date(2026, 10, 16)
        )

    deps = LinkDeps(
        jina_api_key="k",
        probe_company=_no,
        probe_workday=_no,
        probe_dayforce=_no,
        fetch_board=lambda *_a: _async_list(),
        find_page=find_page,
        page_description=page_description,
    )

    res = await _run(cfg, conn, network=True, deps=deps, request_limit=500)

    row = _row(conn, agg)
    assert _status(res, agg) == "upgraded" and row["raw_jd"].strip() == JD.strip()
    assert row["jd_source"] == f"employer:page:{PAGE_HOST}" and row["jd_source_ref"] == PAGE_URL
    assert row["employer_link_source"] == f"page:{PAGE_HOST}"


async def _async_list():
    return []


async def test_a_page_adoption_whose_posting_was_retitled_is_reverted(cfg, conn):
    agg = _job(conn, review="kept")
    conn.execute(
        "UPDATE jobs SET raw_jd=?, jd_source=?, jd_source_ref=?, jd_prior=?, jd_fetched_at=? "
        "WHERE id=?",
        (JD, f"employer:page:{PAGE_HOST}", PAGE_URL, SNIPPET, "2026-09-01T00:00:00", agg),
    )
    conn.commit()
    seen: list[Link] = []

    async def page_description(link, company, title, region):
        seen.append(link)
        return None, "title_drift"

    deps = LinkDeps(page_description=page_description, probe_company=_no)

    res = await _run(cfg, conn, network=True, deps=deps)

    assert seen == [Link(f"page:{PAGE_HOST}", PAGE_URL, PAGE_URL)]
    assert res.reverted == [agg] and _row(conn, agg)["raw_jd"] == SNIPPET

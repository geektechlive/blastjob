"""The user's manual employer posting link: the preferred apply link, never proof.

`POST /jobs/{id}/employer-link` stores a URL he pastes as
`employer_link_source='manual'`. It is validated syntactically only (http(s),
not an aggregator, not LinkedIn) and never fetched. Closure treats it as
display-only: no probe, no verdict change, and the pass never overwrites it.
"""

from __future__ import annotations

import tempfile
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from blastjob.core import closure
from blastjob.core.closure_links import Link, LinkDeps
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

TOKEN = {CSRF_HEADER: LAUNCH_TOKEN}
URL = "https://careers.acme.com/jobs/123"
NOW = datetime(2026, 10, 9, 8, 0, 0)


@pytest.fixture
def cfg() -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    c.discovery.closure_auto = True
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(conn, source: str = "linkedin", ext: str = "1", **cols) -> int:
    jid = dbc.upsert_job(
        conn,
        source=source,
        external_id=ext,
        company="Acme",
        title="VP of IT",
        url="https://www.linkedin.com/jobs/view/1/",
        geo_city="Fairhaven",
        geo_region="OH",
        geo_location_raw="Fairhaven, OH",
    )
    cols.setdefault("review_state", "kept")
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), jid))
    conn.commit()
    return jid


def _link(conn, jid: int) -> tuple:
    r = conn.execute(
        "SELECT employer_link_source, employer_link_external_id, employer_link_url "
        "FROM jobs WHERE id=?",
        (jid,),
    ).fetchone()
    return tuple(r)


def _client(cfg) -> TestClient:
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda _c: None))


# --- the route -------------------------------------------------------------------


def test_saving_a_manual_link_stores_it_and_refreshes_the_page(conn, cfg):
    jid = _job(conn)

    resp = _client(cfg).post(f"/jobs/{jid}/employer-link", data={"url": URL}, headers=TOKEN)

    assert resp.status_code == 200 and resp.headers.get("HX-Refresh") == "true"
    assert _link(conn, jid) == ("manual", URL, URL)


def test_the_detail_page_offers_the_form_and_uses_the_link(conn, cfg):
    jid = _job(
        conn, employer_link_source="manual", employer_link_external_id=URL, employer_link_url=URL
    )

    page = _client(cfg).get(f"/jobs/{jid}").text

    assert f'hx-post="/jobs/{jid}/employer-link"' in page
    assert f'value="{URL}"' in page
    assert "apply on employer site" in page


def test_the_route_requires_csrf(conn, cfg):
    jid = _job(conn)

    resp = _client(cfg).post(f"/jobs/{jid}/employer-link", data={"url": URL})

    assert resp.status_code == 403
    assert _link(conn, jid) == ("", "", "")


def test_the_route_refuses_a_foreign_origin(conn, cfg):
    jid = _job(conn)

    resp = _client(cfg).post(
        f"/jobs/{jid}/employer-link",
        data={"url": URL},
        headers={**TOKEN, "origin": "https://evil.example"},
    )

    assert resp.status_code == 403
    assert _link(conn, jid) == ("", "", "")


@pytest.mark.parametrize(
    "bad",
    [
        "javascript:alert(1)",
        "careers.acme.com/jobs/123",
        "ftp://careers.acme.com/x",
        "https://www.linkedin.com/jobs/view/1/",
        "https://linkedin.com/jobs/view/1/",
        "https://www.indeed.com/viewjob?jk=1",
        "https://www.glassdoor.com/job/1",
    ],
)
def test_the_route_rejects_unsafe_aggregator_and_linkedin_urls(conn, cfg, bad):
    jid = _job(conn)

    resp = _client(cfg).post(f"/jobs/{jid}/employer-link", data={"url": bad}, headers=TOKEN)

    assert resp.status_code == 200 and "HX-Refresh" not in resp.headers
    assert "employer-link-error" in resp.text
    assert _link(conn, jid) == ("", "", "")


def test_an_empty_submit_clears_a_manual_link(conn, cfg):
    jid = _job(
        conn, employer_link_source="manual", employer_link_external_id=URL, employer_link_url=URL
    )

    _client(cfg).post(f"/jobs/{jid}/employer-link", data={"url": " "}, headers=TOKEN)

    assert _link(conn, jid) == ("", "", "")


def test_an_empty_submit_never_clears_an_automatic_link(conn, cfg):
    auto = ("greenhouse:acme", "9", "https://boards.greenhouse.io/acme/jobs/9")
    jid = _job(
        conn,
        employer_link_source=auto[0],
        employer_link_external_id=auto[1],
        employer_link_url=auto[2],
    )

    _client(cfg).post(f"/jobs/{jid}/employer-link", data={"url": ""}, headers=TOKEN)

    assert _link(conn, jid) == auto


def test_an_unknown_job_is_a_404(cfg):
    resp = _client(cfg).post("/jobs/999/employer-link", data={"url": URL}, headers=TOKEN)
    assert resp.status_code == 404


# --- closure: display-only ---------------------------------------------------------


def _no_network(calls: list[str], *, page_answer: bool | None = None) -> LinkDeps:
    """Records every network call. Closure swallows a failing probe (a failed
    probe is no evidence), so raising here would prove nothing: the test
    asserts the record is empty instead."""

    async def check_link(link: Link) -> bool | None:
        calls.append(f"check_link {link.url}")
        return False

    async def check_page(link: Link, company: str, title: str, region: str) -> bool | None:
        calls.append(f"check_page {link.url}")
        return page_answer

    async def find_page(*a, **k):
        calls.append("find_page")
        return None

    def probe(*a, **k):
        calls.append("probe")
        return []

    async def fetch_board(*a, **k):
        calls.append("fetch_board")
        return []

    return LinkDeps(
        check_link=check_link,
        check_page=check_page,
        find_page=find_page,
        probe_company=probe,
        probe_workday=probe,
        probe_dayforce=probe,
        fetch_board=fetch_board,
    )


@pytest.mark.asyncio
async def test_a_manual_link_is_never_probed_and_changes_no_verdict(conn, cfg):
    jid = _job(
        conn,
        employer_link_source="manual",
        employer_link_external_id=URL,
        employer_link_url=URL,
        posting_check="verified_open",
    )

    calls: list[str] = []

    await closure.run_closure(cfg, conn, deps=_no_network(calls), now=NOW)

    assert calls == []
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["posting_check"] == "verified_open"
    assert row["application_state"] == ""
    assert _link(conn, jid) == ("manual", URL, URL)


@pytest.mark.asyncio
async def test_a_dead_manual_url_closes_nothing(conn, cfg):
    """The Codex scenario: a mistyped or dead URL 404s. Nothing reads it."""
    jid = _job(
        conn, employer_link_source="manual", employer_link_external_id=URL, employer_link_url=URL
    )

    calls: list[str] = []

    await closure.run_closure(cfg, conn, deps=_no_network(calls, page_answer=False), now=NOW)

    assert calls == []
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert (row["posting_check"] or "") == ""
    assert row["review_state"] == "kept" and row["application_state"] == ""


@pytest.mark.asyncio
async def test_closure_never_overwrites_a_manual_link_with_a_sibling(conn, cfg):
    board = _job(
        conn, source="greenhouse:acme", ext="9", review_state="surfaced", source_state="stale"
    )
    jid = _job(
        conn, employer_link_source="manual", employer_link_external_id=URL, employer_link_url=URL
    )
    cur = conn.execute("INSERT INTO job_clusters (fingerprint, created_at) VALUES ('f','')")
    conn.execute("UPDATE jobs SET cluster_id=? WHERE id IN (?,?)", (cur.lastrowid, board, jid))
    conn.commit()
    calls: list[str] = []

    await closure.run_closure(cfg, conn, deps=_no_network(calls), now=NOW)

    assert _link(conn, jid) == ("manual", URL, URL)
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["application_state"] == "" and (row["posting_check"] or "") == ""


@pytest.mark.asyncio
async def test_an_authoritative_job_keeps_its_own_verdict_despite_a_manual_link(conn, cfg):
    jid = _job(
        conn,
        source="workday:acme",
        ext="r1",
        source_state="stale",
        employer_link_source="manual",
        employer_link_external_id=URL,
        employer_link_url=URL,
    )
    calls: list[str] = []

    await closure.run_closure(cfg, conn, deps=_no_network(calls), now=NOW)

    assert calls == []
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["posting_check"] == "closed"
    assert _link(conn, jid) == ("manual", URL, URL)

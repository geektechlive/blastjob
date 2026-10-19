"""One role, one "apply here" link: the best URL across a cluster's members.

Order: the employer's own board posting, then a board link closure found, then
an employer careers page or the user's manual link, then a non-agency aggregator
(LinkedIn > Indeed > Adzuna > other), and an agency posting last. Every URL
goes through safe_external_url; one that fails is skipped, never rendered.
"""

from __future__ import annotations

import datetime as dt
import tempfile

import pytest
from fastapi.testclient import TestClient

import tests.agency_fixture as agency_fixture
import tests.geo_fixture as geo_fixture
from blastjob import daily
from blastjob.core import employer_domains
from blastjob.core import role_clusters as rc
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.apply_link import ApplyLink, apply_links_for, best_apply_link


@pytest.fixture(autouse=True)
def _fixture_agencies():
    agency_fixture.install_agencies()


def _m(source: str, url: str = "", company: str = "Acme", **kw) -> dict:
    return {
        "id": kw.pop("id", 1),
        "source": source,
        "company": company,
        "url": url,
        "employer_link_source": kw.pop("link_source", ""),
        "employer_link_url": kw.pop("link_url", ""),
        "source_state": kw.pop("source_state", "active"),
    }


def test_authoritative_own_posting_wins_with_its_board_named():
    members = [
        _m("linkedin", "https://www.linkedin.com/jobs/view/1/"),
        _m("workday:acme", "https://acme.wd5.myworkdayjobs.com/x/job/1"),
    ]
    assert best_apply_link(members) == ApplyLink(
        "https://acme.wd5.myworkdayjobs.com/x/job/1", "employer site (Workday)"
    )


def test_order_after_the_board_posting():
    # careers.acme.com is listed as Acme's own domain, so its page link is shown
    # (an unlisted off-domain page shows only its ATS root, #23).
    employer_domains.set_employer_domains({"Acme": ["acme.com"]})
    board_link = _m(
        "linkedin",
        "https://www.linkedin.com/jobs/view/1/",
        link_source="greenhouse:acme",
        link_url="https://boards.greenhouse.io/acme/jobs/9",
    )
    page_link = _m(
        "indeed",
        "https://www.indeed.com/viewjob?jk=1",
        link_source="page:careers.acme.com",
        link_url="https://careers.acme.com/j/9",
        id=2,
    )
    manual = _m(
        "adzuna",
        "https://www.adzuna.com/details/1",
        link_source="manual",
        link_url="https://careers.acme.com/j/10",
        id=3,
    )
    # page and manual share a tier; the lower id breaks the tie.
    linkedin = _m("linkedin", "https://www.linkedin.com/jobs/view/2/")
    indeed = _m("indeed", "https://www.indeed.com/viewjob?jk=2")
    adzuna = _m("adzuna", "https://www.adzuna.com/details/2")
    other = _m("searchfirm:kf", "https://kornferry.com/j/2")
    agency = _m("linkedin", "https://www.linkedin.com/jobs/view/3/", company="Talonridge Global")

    pool = [agency, other, adzuna, indeed, linkedin, manual, page_link, board_link]
    expected = [
        ("https://boards.greenhouse.io/acme/jobs/9", "employer site (Greenhouse)"),
        ("https://careers.acme.com/j/9", "employer site"),
        ("https://careers.acme.com/j/10", "employer site"),
        ("https://www.linkedin.com/jobs/view/2/", "LinkedIn"),
        ("https://www.indeed.com/viewjob?jk=2", "Indeed"),
        ("https://www.adzuna.com/details/2", "Adzuna"),
        ("https://kornferry.com/j/2", "searchfirm:kf"),
        ("https://www.linkedin.com/jobs/view/3/", "Talonridge Global (agency)"),
    ]
    for url, label in expected:
        best = best_apply_link(pool)
        assert best == ApplyLink(url, label)
        pool = [m for m in pool if url not in (m["url"], m["employer_link_url"])]
    assert best_apply_link([]) is None


def test_an_agency_is_last_even_on_linkedin():
    agency = _m("linkedin", "https://www.linkedin.com/jobs/view/3/", company="Corvin Hale")
    adzuna = _m("adzuna", "https://www.adzuna.com/details/2")
    assert best_apply_link([agency, adzuna]).url == "https://www.adzuna.com/details/2"


def test_unsafe_urls_are_skipped():
    members = [
        _m("workday:acme", "javascript:alert(1)"),
        _m("linkedin", "", link_source="manual", link_url="data:text/html,x"),
        _m("indeed", "https://www.indeed.com/viewjob?jk=3"),
    ]
    assert best_apply_link(members) == ApplyLink("https://www.indeed.com/viewjob?jk=3", "Indeed")
    assert best_apply_link([_m("linkedin", "javascript:x")]) is None


# --- rendered ------------------------------------------------------------------


@pytest.fixture
def cfg() -> BlastJobConfig:
    c = BlastJobConfig()
    geo_fixture.apply_region(c)
    agency_fixture.apply_agencies(c)
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(conn, source: str, ext: str, url: str, **cols) -> int:
    jid = dbc.upsert_job(conn, source=source, external_id=ext, company="Acme", title="CIO", url=url)
    if cols:
        sets = ", ".join(f"{k}=?" for k in cols)
        conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), jid))
    conn.commit()
    return jid


def _pair(conn) -> tuple[int, int]:
    li = _job(
        conn,
        "linkedin",
        "1",
        "https://www.linkedin.com/jobs/view/1/",
        review_state="surfaced",
        alignment_score=90,
    )
    board = _job(
        conn,
        "workday:acme",
        "2",
        "https://acme.wd5.myworkdayjobs.com/x/job/2",
        review_state="surfaced",
        alignment_score=70,
    )
    assert rc.merge_jobs(conn, li, board) == ""
    conn.commit()
    return li, board


def _client(cfg) -> TestClient:
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda _c: None))


def test_list_row_renders_the_clusters_best_link(conn, cfg):
    li, _board = _pair(conn)

    page = _client(cfg).get("/").text

    row = page[page.index(f'id="job-{li}"') :]
    row = row[: row.index("</article>")]
    assert (
        '<a class="apply-link" href="https://acme.wd5.myworkdayjobs.com/x/job/2" '
        'rel="noopener" target="_blank">apply on employer site (Workday)'
    ) in row


def test_unclustered_row_uses_its_own_url(conn, cfg):
    jid = _job(
        conn,
        "indeed",
        "9",
        "https://www.indeed.com/viewjob?jk=9",
        review_state="surfaced",
        alignment_score=90,
    )

    page = _client(cfg).get("/").text

    row = page[page.index(f'id="job-{jid}"') :]
    assert "apply on Indeed" in row[: row.index("</article>")]


def test_detail_page_shows_the_apply_link_above_open_posting(conn, cfg):
    li, _board = _pair(conn)

    page = _client(cfg).get(f"/jobs/{li}").text

    apply_at = page.index("apply on employer site (Workday)")
    assert apply_at < page.index("open posting")


def test_digest_links_the_clusters_best_url(conn):
    li, _board = _pair(conn)
    rows = daily.pending_jobs(conn)

    links = apply_links_for(conn, rows)
    text = daily.build_digest(rows, {}, [], apply_links=links)

    assert links[li].url == "https://acme.wd5.myworkdayjobs.com/x/job/2"
    assert "<https://acme.wd5.myworkdayjobs.com/x/job/2>" in text
    # No map: the row's own URL, as before.
    assert "<https://www.linkedin.com/jobs/view/1/>" in daily.build_digest(rows, {}, [])


@pytest.mark.asyncio
async def test_run_daily_sends_the_best_link(cfg, conn, monkeypatch):
    li, _board = _pair(conn)

    async def no_discovery(cfg, conn, adapters=None):
        from blastjob.discovery.pipeline import DiscoveryResult

        return DiscoveryResult()

    async def no_closure(cfg, conn, res, **_kw):
        return None

    async def no_replies(cfg, conn, res, dry_run=False):
        return None

    import blastjob.discovery.pipeline as dp

    monkeypatch.setattr(dp, "run_discovery", no_discovery)
    monkeypatch.setattr(daily, "_closure_pass", no_closure)
    monkeypatch.setattr(daily, "_replies_pass", no_replies)
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    await daily.run_daily(cfg, conn, send=send, score=False, today=dt.date(2026, 10, 6))

    assert sent and "<https://acme.wd5.myworkdayjobs.com/x/job/2>" in sent[0]

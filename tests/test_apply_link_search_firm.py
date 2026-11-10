"""A retained firm's page is the apply link while its search is open (ADR 0072, #112)."""

from __future__ import annotations

import pytest

import tests.agency_fixture as agency_fixture
from blastjob.web.apply_link import ApplyLink, best_apply_link

FIRM_URL = "https://www.acmesearch.example/opportunities/1/chief-information-officer"
EMPLOYER_URL = "https://fabrikam.wd5.myworkdayjobs.com/x/job/1"


@pytest.fixture(autouse=True)
def _fixture_agencies() -> None:
    agency_fixture.install_agencies()


def _firm(**kw) -> dict:
    return {
        "id": kw.pop("id", 2),
        "source": "searchfirm:acme",
        "company": "Acme Executive Search (retained search)",
        "url": FIRM_URL,
        "source_state": "active",
        "posting_check": kw.pop("posting_check", ""),
        "posting_closed_at": kw.pop("posting_closed_at", ""),
        **kw,
    }


def _employer(**kw) -> dict:
    return {
        "id": kw.pop("id", 1),
        "source": "workday:fabrikam",
        "company": "Fabrikam Credit Union",
        "url": EMPLOYER_URL,
        "source_state": "active",
        **kw,
    }


def test_the_firm_page_beats_the_employers_own_board_posting() -> None:
    link = best_apply_link([_employer(), _firm()])
    assert link == ApplyLink(FIRM_URL, "retained search (Acme Executive Search)")


def test_a_closed_search_never_gets_the_firm_tier() -> None:
    for closed in (_firm(posting_check="closed"), _firm(posting_closed_at="2026-11-09T10:00:00")):
        link = best_apply_link([_employer(), closed])
        assert link == ApplyLink(EMPLOYER_URL, "employer site (Workday)")


def test_a_contingency_agency_still_ranks_last() -> None:
    contingency = {
        "id": 3,
        "source": "adzuna",
        "company": agency_fixture.STAFFING_AGENCY,
        "url": "https://www.adzuna.example/job/3",
        "source_state": "active",
    }
    link = best_apply_link([contingency, _employer()])
    assert link == ApplyLink(EMPLOYER_URL, "employer site (Workday)")
    only = best_apply_link([contingency])
    assert only is not None and only.label.endswith("(agency)")


def test_a_filled_firm_page_loses_to_the_employer_posting_through_the_database(tmp_path) -> None:
    from blastjob.db import client as dbc
    from blastjob.models.config import BlastJobConfig
    from blastjob.web.apply_link import apply_links_for

    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    conn = dbc.init_db(cfg)
    cluster = conn.execute(
        "INSERT INTO job_clusters (fingerprint, created_at) VALUES ('fx', '2026-11-01')"
    ).lastrowid
    employer = dbc.upsert_job(
        conn,
        source="workday:fabrikam",
        external_id="e1",
        company="Fabrikam Credit Union",
        title="Chief Information Officer",
        url=EMPLOYER_URL,
    )
    firm = dbc.upsert_job(
        conn,
        source="searchfirm:acme",
        external_id="f1",
        company="Acme Executive Search (retained search)",
        title="Fabrikam Credit Union - Chief Information Officer",
        url=FIRM_URL,
    )
    conn.execute("UPDATE jobs SET cluster_id=?", (cluster,))
    conn.commit()

    rows = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    open_link = apply_links_for(conn, rows)[employer]
    conn.execute("UPDATE jobs SET posting_check='closed' WHERE id=?", (firm,))
    conn.commit()
    rows = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    closed_link = apply_links_for(conn, rows)[employer]

    assert open_link.url == FIRM_URL
    assert closed_link == ApplyLink(EMPLOYER_URL, "employer site (Workday)")

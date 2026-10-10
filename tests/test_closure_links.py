"""Employer-link matching and the real network layer, fully mocked.

A wrong link would falsely close a live job, so the matcher is deliberately
strict: an ambiguous title or an unrelated board name means "no link".
"""

from __future__ import annotations

import tempfile

import httpx
import pytest

from blastjob.core import closure, page_fetch, page_links
from blastjob.core import closure_links as cl
from blastjob.core.closure_links import default_deps as real_default_deps
from blastjob.db import client as dbc
from blastjob.discovery.base import RawPosting
from blastjob.discovery.resolve import BoardHit
from blastjob.models.config import BlastJobConfig


def _p(ext: str, title: str, region: str = "") -> RawPosting:
    return RawPosting(source="greenhouse:acme", external_id=ext, title=title, region=region)


def test_normalize_title_expands_abbreviations_and_drops_punctuation():
    assert cl.normalize_title("Sr. VP, Generic Program (Remote)") == (
        "senior vice president generic program"
    )
    assert cl.normalize_title("Director - Ops & Security") == "director ops and security"


def test_exact_normalized_title_is_a_match():
    got = cl.match_posting(
        "VP of Operations", [_p("1", "Vice President of Operations"), _p("2", "CFO")]
    )
    assert got is not None and got.external_id == "1"


def test_two_exact_matches_are_ambiguous():
    assert (
        cl.match_posting(
            "VP of Operations", [_p("1", "VP of Operations"), _p("2", "VP of Operations")]
        )
        is None
    )


def test_a_broader_title_is_not_a_match():
    assert (
        cl.match_posting(
            "Director of Operations", [_p("1", "Director of Operations Infrastructure")]
        )
        is None
    )


def test_near_identical_title_matches_when_it_is_the_only_close_candidate():
    got = cl.match_posting(
        "Vice President Generic Program",
        [_p("1", "Vice President, Generic Programs"), _p("2", "Controller")],
    )
    assert got is not None and got.external_id == "1"


def test_two_near_candidates_are_ambiguous():
    got = cl.match_posting(
        "Vice President Generic Program",
        [
            _p("1", "Vice President, Generic Programs"),
            _p("2", "Vice President Generic Program I"),
        ],
    )
    assert got is None


def test_region_mismatch_blocks_a_match():
    assert (
        cl.match_posting(
            "VP of Operations", [_p("1", "VP of Operations", region="TX")], region="OH"
        )
        is None
    )


@pytest.mark.parametrize(
    ("job", "posting", "ok"),
    [
        ("Wisconsin", "IL", False),
        ("Wisconsin", "WI", True),
        ("wisconsin", "Wisconsin", True),
        ("New  York", "NY", True),
        ("", "IL", True),
        ("Remote", "IL", True),
    ],
)
def test_region_folds_state_names_to_codes(job, posting, ok):
    assert cl._region_ok(job, posting) is ok


@pytest.mark.parametrize(
    ("ats", "token", "company", "ok"),
    [
        ("greenhouse", "fenwick", "Fenwick Corporation", True),
        ("workday", "carrows/wd504/carrowscareers", "Carrow's", True),
        ("dayforce", "lakeshorecu/CANDIDATEPORTAL", "Lakeshore Credit Union", False),
        ("greenhouse", "acmehealth", "Acme", False),
        ("lever", "", "Acme", False),
    ],
)
def test_board_must_belong_to_the_job_company(ats, token, company, ok):
    assert cl.board_matches_company(ats, token, company) is ok


def test_link_source_to_token_round_trip():
    wd = cl.Link("workday:acme", "R1", "https://acme.wd5.myworkdayjobs.com/en-US/Ext/job/x_R1")
    assert cl.board_token(wd) == ("workday", "acme/wd5/Ext")
    gh = cl.Link("greenhouse:fenwick", "9", "https://boards.greenhouse.io/fenwick/jobs/9")
    assert cl.board_token(gh) == ("greenhouse", "fenwick")
    df = cl.Link("dayforce:lcu", "5", "https://jobs.dayforcehcm.com/lcu/CANDIDATEPORTAL/jobs/5")
    assert cl.board_token(df) == ("dayforce", "lcu/CANDIDATEPORTAL")
    assert cl.board_token(cl.Link("linkedin", "1", "https://www.linkedin.com/jobs/view/1")) is None


# --- the real network layer: no request may ever reach linkedin.com ---------


class _Net:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def _response(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        return httpx.Response(
            200,
            json={"jobs": [], "jobPostings": [], "total": 0, "maxCount": 0, "csrfToken": "t"},
            request=request,
        )


@pytest.fixture
def net(monkeypatch):
    rec = _Net()

    async def async_send(self, request, **kw):
        return rec._response(request)

    def sync_send(self, request, **kw):
        return rec._response(request)

    monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
    monkeypatch.setattr(httpx.Client, "send", sync_send)
    return rec


async def test_linkedin_jobs_never_trigger_a_request_to_linkedin(net, monkeypatch):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)
    try:
        for i, company in enumerate(["Fabrikam Equipment", "Northwind Power", "Acme Staffing"]):
            jid = dbc.upsert_job(
                conn,
                source="linkedin",
                external_id=f"li{i}",
                company=company,
                title="Chief Operations Officer",
                url=f"https://www.linkedin.com/jobs/view/{i}",
            )
            conn.execute("UPDATE jobs SET review_state='kept' WHERE id=?", (jid,))
        # One already linked to a board the daily config does not poll.
        conn.execute(
            "UPDATE jobs SET employer_link_source='greenhouse:northwind', "
            "employer_link_external_id='77', employer_link_url='https://www.linkedin.com/x' "
            "WHERE external_id='li1'"
        )
        conn.commit()
        deps = cl.LinkDeps(jina_api_key="test-key")
        # The real page search too, over the same mocked transport.
        monkeypatch.setattr(page_links, "_jina_search", page_fetch.jina_search)
        monkeypatch.setattr(page_links, "_safe_get", page_fetch.safe_get)
        await closure.run_closure(cfg, conn, deps=deps)
    finally:
        conn.close()
    assert net.urls, "the mocked layer should have seen the resolver probes"
    assert not [u for u in net.urls if "linkedin.com" in httpx.URL(u).host]


# --- the page: fallback ----------------------------------------------------------


class _PageDeps:
    def __init__(self, boards: list[BoardHit], postings: list[RawPosting]) -> None:
        self.boards, self.postings, self.page_calls = boards, postings, []

    def probe(self, name: str) -> list[BoardHit]:
        return list(self.boards)

    def none(self, name: str, key: str) -> list[BoardHit]:
        return []

    async def fetch_board(self, ats: str, token: str, title: str) -> list[RawPosting]:
        return list(self.postings)

    async def find_page(self, company: str, title: str, region: str, key: str):
        self.page_calls.append((company, title, region, key))
        return cl.Link("page:careers.acme.com", "https://careers.acme.com/j/1", "u")

    def deps(self, jina: str = "key") -> cl.LinkDeps:
        return cl.LinkDeps(
            jina_api_key=jina,
            probe_company=self.probe,
            probe_workday=self.none,
            probe_dayforce=self.none,
            fetch_board=self.fetch_board,
            find_page=self.find_page,
        )


async def test_zero_board_hits_fall_through_to_the_page_search():
    fake = _PageDeps([], [])

    link = await cl.resolve_link("Acme", "VP of IT", "WI", fake.deps())

    assert link is not None and link.source == "page:careers.acme.com"
    assert fake.page_calls == [("Acme", "VP of IT", "WI", "key")]


async def test_board_ambiguity_does_not_fall_through_to_the_page_search():
    hit = BoardHit("Acme", "greenhouse", "acme", 2)
    fake = _PageDeps([hit], [_p("1", "VP of IT"), _p("2", "Vice President of IT")])

    assert await cl.resolve_link("Acme", "VP of IT", "WI", fake.deps()) is None
    assert fake.page_calls == []


async def test_a_board_match_wins_over_the_page_search():
    fake = _PageDeps([BoardHit("Acme", "greenhouse", "acme", 1)], [_p("1", "VP of IT")])

    link = await cl.resolve_link("Acme", "VP of IT", "WI", fake.deps())

    assert link is not None and link.source == "greenhouse:acme"
    assert fake.page_calls == []


async def test_no_jina_key_means_no_page_search():
    fake = _PageDeps([], [])

    assert await cl.resolve_link("Acme", "VP of IT", "WI", fake.deps(jina="")) is None
    assert fake.page_calls == []


async def test_default_check_link_has_no_opinion_on_a_page_link(net):
    link = cl.Link("page:careers.acme.com", "https://careers.acme.com/j/1", "u")

    assert await real_default_deps().check_link(link) is None
    assert net.urls == []

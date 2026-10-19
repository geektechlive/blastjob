"""Employer-JD units U2/U3 (issue #17): Match/resolve_match, the request budget and
verified_description (fresh trust revalidation before any description is adopted).

Everything is injected; nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import datetime
import json

import pytest

from blastjob.core import closure_links as cl
from blastjob.core import employer_domains
from blastjob.core import page_fetch as pf
from blastjob.core import page_links as pl
from blastjob.core.closure_links import Link, LinkDeps
from blastjob.discovery.base import RawPosting
from blastjob.discovery.resolve import BoardHit

COMPANY, TITLE, REGION = "Acme", "VP of Operations", "OH"
LONG_JD = "Lead the operations organization. " * 40


def _gh(ext: str, title: str, region: str = "OH", description: str = LONG_JD) -> RawPosting:
    return RawPosting(
        source="greenhouse:acme",
        external_id=ext,
        title=title,
        region=region,
        url=f"https://boards.greenhouse.io/acme/jobs/{ext}",
        description=description,
    )


def _deps(board: list[RawPosting], **over) -> LinkDeps:
    async def fetch_board(ats: str, token: str, title: str) -> list[RawPosting]:
        return board

    base = {
        "probe_company": lambda company: [BoardHit(company, "greenhouse", "acme", len(board))],
        "fetch_board": fetch_board,
    }
    return LinkDeps(**{**base, **over})


def _run(coro):
    return asyncio.run(coro)


# --- Match / resolve_match -----------------------------------------------------


def test_resolve_match_carries_the_matched_posting_and_resolve_link_wraps_it():
    board = [_gh("1", "VP of Operations"), _gh("2", "CFO")]
    deps = _deps(board)

    match = _run(cl.resolve_match(COMPANY, TITLE, REGION, deps))
    link = _run(cl.resolve_link(COMPANY, TITLE, REGION, deps))

    assert match is not None and match.posting is not None
    assert match.posting.description == LONG_JD
    assert match.link == link == Link("greenhouse:acme", "1", board[0].url)


def test_resolve_match_is_none_when_nothing_matches():
    assert _run(cl.resolve_match(COMPANY, TITLE, REGION, _deps([_gh("2", "CFO")]))) is None


def test_a_page_fallback_match_has_a_link_and_no_posting():
    page_link = Link("page:careers.acme.com", "https://careers.acme.com/j/1", "https://x")

    async def find_page(company, title, region, key):
        return page_link

    deps = _deps([], jina_api_key="k", find_page=find_page, probe_workday=lambda c, k: [],
                 probe_dayforce=lambda c, k: [])  # fmt: skip
    match = _run(cl.resolve_match(COMPANY, TITLE, REGION, deps))
    assert match == cl.Match(link=page_link, posting=None)


# --- RequestBudget / budgeted ---------------------------------------------------


def test_request_budget_counts_and_refuses_past_the_limit():
    b = cl.RequestBudget(2)
    assert (b.used, b.remaining, b.exhausted) == (0, 2, False)
    assert b.take() is True
    assert b.take() is True
    assert (b.used, b.remaining, b.exhausted) == (2, 0, True)
    assert b.take() is False
    assert b.used == 2  # a refused take spends nothing


def test_request_budget_take_n_is_all_or_nothing():
    b = cl.RequestBudget(3)
    assert b.take(2) is True
    assert b.take(2) is False
    assert b.remaining == 1


async def _async_none(*a, **k):
    return None


def test_budgeted_meters_every_outbound_callable_and_stops_when_empty():
    deps = LinkDeps(
        jina_api_key="k",
        probe_company=lambda c: [],
        probe_workday=lambda c, k: [],
        probe_dayforce=lambda c, k: [],
        fetch_board=_async_none,
        check_link=_async_none,
        find_page=_async_none,
        check_page=_async_none,
        fetch_workday_detail=_async_none,
        page_description=_async_none,
    )
    link = Link("greenhouse:acme", "1", "")
    calls = {
        "probe_company": lambda d: asyncio.to_thread(d.probe_company, "Acme"),
        "probe_workday": lambda d: asyncio.to_thread(d.probe_workday, "Acme", "k"),
        "probe_dayforce": lambda d: asyncio.to_thread(d.probe_dayforce, "Acme", "k"),
        "fetch_board": lambda d: d.fetch_board("greenhouse", "acme", ""),
        "check_link": lambda d: d.check_link(link),
        "find_page": lambda d: d.find_page("Acme", "T", "OH", "k"),
        "check_page": lambda d: d.check_page(link, "Acme", "T", "OH"),
        "fetch_workday_detail": lambda d: d.fetch_workday_detail("t/wd1/s", "https://x"),
        "page_description": lambda d: d.page_description(link, "Acme", "T", "OH"),
    }
    # Each call is charged its worst-case request units (the exact table lives in
    # test_ejd_fix_budget); here only that EVERY callable is metered at all.
    for name, call in calls.items():
        budget = cl.RequestBudget(30)
        wrapped = cl.budgeted(deps, budget)
        _run(call(wrapped))
        spent = budget.used
        assert spent >= 1, name
        budget.take(budget.remaining)  # empty it
        with pytest.raises(cl.BudgetExhausted):
            _run(call(wrapped))
        assert budget.used == budget.limit, name


def test_budgeted_keeps_the_key_and_unmetered_fields():
    deps = LinkDeps(jina_api_key="secret")
    assert cl.budgeted(deps, cl.RequestBudget(1)).jina_api_key == "secret"


def test_resolve_match_stops_with_budget_exhausted_mid_resolution():
    deps = cl.budgeted(_deps([_gh("1", "VP of Operations")]), cl.RequestBudget(1))
    with pytest.raises(cl.BudgetExhausted):
        _run(cl.resolve_match(COMPANY, TITLE, REGION, deps))  # probe, then the board fetch


# --- verified_description: boards ------------------------------------------------


def _verify(link: Link, deps: LinkDeps, company: str = COMPANY, title: str = TITLE):
    return _run(cl.verified_description(link, company, title, REGION, deps))


GH_LINK = Link("greenhouse:acme", "1", "https://boards.greenhouse.io/acme/jobs/1")


def test_a_board_posting_that_still_matches_gives_its_text():
    text, reason = _verify(GH_LINK, _deps([_gh("1", "Vice President of Operations")]))
    assert (text, reason) == (LONG_JD, "ok")


def test_a_reused_posting_id_for_another_role_is_title_drift():
    text, reason = _verify(GH_LINK, _deps([_gh("1", "Chief Financial Officer")]))
    assert (text, reason) == (None, "title_drift")


def test_a_second_equally_good_title_makes_the_match_ambiguous():
    board = [_gh("1", "VP of Operations"), _gh("9", "VP of Operations")]
    assert _verify(GH_LINK, _deps(board)) == (None, "title_drift")


def test_a_better_matching_other_id_is_title_drift_not_adoption():
    board = [_gh("1", "VP of Operations Infrastructure"), _gh("2", "VP of Operations")]
    assert _verify(GH_LINK, _deps(board)) == (None, "title_drift")


def test_a_posting_gone_from_the_board_is_gone():
    assert _verify(GH_LINK, _deps([_gh("2", "VP of Operations")])) == (None, "gone")


def test_a_board_that_is_not_the_companys_is_company_mismatch():
    link = Link("greenhouse:acmelabs", "1", "")
    assert _verify(link, _deps([_gh("1", TITLE)])) == (None, "company_mismatch")


def test_a_posting_without_text_is_no_text():
    assert _verify(GH_LINK, _deps([_gh("1", TITLE, description="")])) == (None, "no_text")


def test_a_fetch_error_is_reported_not_raised():
    async def boom(ats, token, title):
        raise RuntimeError("down")

    assert _verify(GH_LINK, _deps([], fetch_board=boom)) == (None, "error:RuntimeError")


def test_a_posting_now_in_another_state_is_title_drift():
    board = [_gh("1", TITLE, region="TX")]
    assert _verify(GH_LINK, _deps(board)) == (None, "title_drift")


def test_a_manual_link_is_never_fetched():
    async def fail(*a, **k):
        raise AssertionError("a manual link must not be fetched")

    deps = _deps([], fetch_board=fail, fetch_workday_detail=fail, page_description=fail)
    assert _verify(Link("manual", "https://x/y", "https://x/y"), deps) == (None, "manual")


def test_budget_exhaustion_propagates_out_of_verified_description():
    deps = cl.budgeted(_deps([_gh("1", TITLE)]), cl.RequestBudget(0))
    with pytest.raises(cl.BudgetExhausted):
        _verify(GH_LINK, deps)


def test_dayforce_is_revalidated_from_the_current_listing():
    board = [
        RawPosting(
            source="dayforce:acme", external_id="77", title=TITLE, region="OH", description=LONG_JD
        )  # fmt: skip
    ]
    url = "https://jobs.dayforcehcm.com/acme/CANDIDATEPORTAL/jobs/77"
    link = Link("dayforce:acme", "77", url)
    assert _verify(link, _deps(board)) == (LONG_JD, "ok")
    drifted = [board[0].model_copy(update={"title": "Janitor"})]
    assert _verify(link, _deps(drifted)) == (None, "title_drift")


# --- verified_description: Workday -----------------------------------------------

WD_URL = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Oakbrook-OH/VP-of-Operations_R1"
WD_LINK = Link("workday:acme", "R1", WD_URL)


def _wd_detail(title: str = TITLE, region: str = "OH", text: str = LONG_JD):
    seen: list[tuple[str, str]] = []

    async def fetch(token: str, url: str) -> RawPosting | None:
        seen.append((token, url))
        return RawPosting(source="workday:acme", external_id="R1", title=title, region=region,
                          description=text)  # fmt: skip

    return fetch, seen


def test_workday_detail_is_fetched_by_the_board_token_and_title_rechecked():
    fetch, seen = _wd_detail()
    text, reason = _verify(WD_LINK, _deps([], fetch_workday_detail=fetch))
    assert (text, reason) == (LONG_JD, "ok")
    assert seen == [("acme/wd5/careers", WD_URL)]


def test_workday_title_drift_is_refused():
    fetch, _ = _wd_detail(title="Chief Financial Officer")
    assert _verify(WD_LINK, _deps([], fetch_workday_detail=fetch)) == (None, "title_drift")


def test_workday_region_mismatch_is_refused():
    fetch, _ = _wd_detail(region="TX")
    assert _verify(WD_LINK, _deps([], fetch_workday_detail=fetch)) == (None, "title_drift")


def test_workday_404_is_gone():
    async def fetch(token, url):
        return None

    assert _verify(WD_LINK, _deps([], fetch_workday_detail=fetch)) == (None, "gone")


def test_workday_tenant_not_the_company_is_company_mismatch():
    fetch, seen = _wd_detail()
    link = Link("workday:other", "R1", WD_URL.replace("acme.", "other."))
    assert _verify(link, _deps([], fetch_workday_detail=fetch)) == (None, "company_mismatch")
    assert seen == []


# --- page links --------------------------------------------------------------------

HOST = "careers.acme.com"


@pytest.fixture(autouse=True)
def _acme_owns_its_careers_domain():
    """careers.acme.com is confirmed as Acme's own domain: a page there may
    supply text. An unlisted page bound only by its apply form never does (#23,
    tests/test_page_form_trust.py)."""
    employer_domains.set_employer_domains({"Acme": ["acme.com"]})


PAGE_URL = f"https://{HOST}/apply/jobs/details/AbC123"
OTHER_URL = f"https://{HOST}/apply/jobs/details/ZzZ999"
TRUSTED_TEXT = "<p>Run all of operations for Acme.</p>" + "<p>Own the P&L.</p>" * 50


def _ld(**over) -> dict:
    p = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": TITLE,
        "url": PAGE_URL,
        "validThrough": "2099-12-31",
        "description": TRUSTED_TEXT,
        "hiringOrganization": {"@type": "Organization", "name": "Acme Inc"},
        "jobLocation": {"@type": "Place", "address": {"addressRegion": "OH"}},
    }
    return {**p, **over}


def _html(*postings: dict, form: str = "https://acme1.applytojob.com/apply/AbC123") -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(p)}</script>' for p in postings
    )
    return f'<html><head>{scripts}</head><body><form action="{form}"></form></body></html>'


def _fetch(html: str, status: int = 200, url: str = PAGE_URL, cross: bool = False):
    async def fetch(u: str):
        return pf.Page(status=status, url=url, html=html, cross_host=cross)

    return fetch


PAGE_LINK = Link(f"page:{HOST}", PAGE_URL, PAGE_URL)
TODAY = datetime.date(2026, 10, 16)


def _describe(html: str, **kw):
    return _run(pl.page_description(PAGE_LINK, COMPANY, TITLE, REGION, fetch=_fetch(html, **kw)))


def test_page_description_returns_the_trusted_postings_own_text():
    text, reason = _describe(_html(_ld()))
    assert reason == "ok" and text is not None and "Own the P&L" in text


def test_page_description_never_takes_the_first_posting_on_the_page():
    other = _ld(title="Chief Financial Officer", url=OTHER_URL, description="WRONG ROLE " * 80)
    text, reason = _describe(_html(other, _ld()))
    assert reason == "ok" and text is not None and "WRONG ROLE" not in text


def test_page_description_refuses_another_employers_posting():
    foreign = _ld(hiringOrganization={"@type": "Organization", "name": "Other Co"})
    assert _describe(_html(foreign)) == (None, "company_mismatch")


def test_page_description_refuses_a_retitled_posting():
    assert _describe(_html(_ld(title="Janitor"))) == (None, "title_drift")


def test_page_description_without_an_ats_binding_is_company_mismatch():
    assert _describe(_html(_ld(), form="https://elsewhere.example.com/x")) == (
        None,
        "company_mismatch",
    )


def test_page_description_on_a_410_is_gone():
    assert _describe(_html(_ld()), status=410) == (None, "gone")


def test_page_description_when_the_posting_left_the_page_is_gone():
    assert _describe(_html(_ld(url=OTHER_URL))) == (None, "gone")


def test_page_description_on_an_expired_posting_is_gone():
    assert _describe(_html(_ld(validThrough="2020-01-01"))) == (None, "gone")


def test_page_description_without_text_is_no_text():
    assert _describe(_html(_ld(description=""))) == (None, "no_text")


def test_page_description_cross_host_redirect_is_not_trusted():
    text, reason = _describe(_html(_ld()), cross=True)
    assert text is None and reason != "ok"


def test_verified_description_routes_page_links_through_deps():
    seen = []

    async def page_description(link, company, title, region):
        seen.append(link)
        return "T", "ok"

    deps = _deps([], page_description=page_description)
    assert _verify(PAGE_LINK, deps) == ("T", "ok")
    assert seen == [PAGE_LINK]

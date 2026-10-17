"""Employer-JD review fix F3: the request budget counts REQUEST UNITS (each wrapper
call weighted by its worst-case HTTP fan-out), and the limits were raised to keep
the same number of lookups.

Weights (see `closure_links.budgeted`): probe_company 9, probe_workday 7,
probe_dayforce 14, fetch_board 1 (Dayforce 21), check_link 1 (Dayforce 21),
find_page 19, check_page 6, page_description 6, fetch_workday_detail 1.
"""

from __future__ import annotations

import asyncio

import pytest

from blastjob import daily_tools
from blastjob.core import closure_links as cl
from blastjob.core.closure_links import Link, LinkDeps
from blastjob.discovery.base import RawPosting
from blastjob.discovery.resolve import BoardHit
from blastjob.models.config import DiscoveryConfig
from blastjob.web import employer_jd as web_ejd


def _run(coro):
    return asyncio.run(coro)


async def _none(*_a, **_k):
    return None


def _plain_deps(**over) -> LinkDeps:
    base = {
        "jina_api_key": "k",
        "probe_company": lambda c: [],
        "probe_workday": lambda c, k: [],
        "probe_dayforce": lambda c, k: [],
        "fetch_board": _none,
        "check_link": _none,
        "find_page": _none,
        "check_page": _none,
        "fetch_workday_detail": _none,
        "page_description": _none,
    }
    return LinkDeps(**{**base, **over})


GH = Link("greenhouse:acme", "1", "")
DF = Link("dayforce:acme", "1", "https://jobs.dayforcehcm.com/en-US/acme/CANDIDATEPORTAL/jobs/1")


def test_one_resolve_match_consumes_the_documented_weight():
    posting = RawPosting(source="greenhouse:acme", external_id="1", title="VP of Ops", region="OH")

    async def fetch_board(ats, token, title):
        return [posting]

    deps = _plain_deps(
        probe_company=lambda c: [BoardHit(c, "greenhouse", "acme", 4)],
        fetch_board=fetch_board,
    )
    budget = cl.RequestBudget(1000)

    match = _run(cl.resolve_match("Acme", "VP of Ops", "OH", cl.budgeted(deps, budget)))

    assert match is not None
    # probe_company 9 + probe_workday 7 + probe_dayforce 14 + one GH listing 1
    assert budget.used == 9 + 7 + 14 + 1


@pytest.mark.parametrize(
    ("name", "call", "weight"),
    [
        ("probe_company", lambda d: asyncio.to_thread(d.probe_company, "Acme"), 9),
        ("probe_workday", lambda d: asyncio.to_thread(d.probe_workday, "Acme", "k"), 7),
        ("probe_dayforce", lambda d: asyncio.to_thread(d.probe_dayforce, "Acme", "k"), 14),
        ("board_gh", lambda d: d.fetch_board("greenhouse", "acme", ""), 1),
        ("board_workday", lambda d: d.fetch_board("workday", "acme/wd5/x", "T"), 1),
        ("board_dayforce", lambda d: d.fetch_board("dayforce", "acme/CP", ""), 21),
        ("check_gh", lambda d: d.check_link(GH), 1),
        ("check_dayforce", lambda d: d.check_link(DF), 21),
        ("find_page", lambda d: d.find_page("Acme", "T", "OH", "k"), 19),
        ("check_page", lambda d: d.check_page(GH, "Acme", "T", "OH"), 6),
        ("page_description", lambda d: d.page_description(GH, "Acme", "T", "OH"), 6),
        ("workday_detail", lambda d: d.fetch_workday_detail("t/wd1/s", "https://x"), 1),
    ],
)
def test_every_metered_call_costs_its_worst_case_fan_out(name, call, weight):
    budget = cl.RequestBudget(weight)
    wrapped = cl.budgeted(_plain_deps(), budget)

    _run(call(wrapped))

    assert budget.used == weight, name
    with pytest.raises(cl.BudgetExhausted):
        _run(call(wrapped))
    assert budget.used == weight, name  # a refused call spends nothing


def test_a_call_heavier_than_the_remainder_is_refused_whole():
    budget = cl.RequestBudget(10)
    wrapped = cl.budgeted(_plain_deps(), budget)

    with pytest.raises(cl.BudgetExhausted):
        _run(wrapped.find_page("Acme", "T", "OH", "k"))  # 19 > 10

    assert budget.used == 0


def test_the_limits_keep_the_same_number_of_lookups_per_budget():
    assert DiscoveryConfig().employer_jd_daily_requests == 300
    assert daily_tools.EMPLOYER_JD_BACKFILL_REQUESTS == 1000
    assert web_ejd.BUTTON_REQUEST_LIMIT == 40

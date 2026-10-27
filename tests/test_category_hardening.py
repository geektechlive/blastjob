# ruff: noqa: F811 - the cfg/conn fixtures are imported from test_category_search
"""Security-review hardening of the category search (#91): URL identity, URL filter, Adzuna
bound, archived jobs, and trust parity with search-firm pages."""

from __future__ import annotations

import datetime as dt

import pytest

from blastjob.discovery import category_search as cat
from tests.test_category_search import (  # noqa: F401 - fixtures
    GOOD,
    WEDNESDAY,
    FakeAdz,
    _category,
    _hit,
    _hits,
    _jobs,
    _posting,
    _read_of,
    _search_of,
    cfg,
    conn,
)

KEEP = ["cio", "cto"]


# ----------------------------------------------------------------------------- identity


@pytest.mark.parametrize(
    "key",
    [
        "gh_jid",
        "jobId",
        "JOBID",
        "job_id",
        "id",
        "req",
        "reqId",
        "requisitionId",
        "jk",
        "posting_id",
    ],
)
def test_identity_query_keys_are_kept(key):
    a = cat.canonical_url(f"https://careers.example.com/jobs?{key}=123")
    b = cat.canonical_url(f"https://careers.example.com/jobs?{key}=456")
    assert a != b and f"={123}" in a


def test_tracking_params_are_dropped_and_identity_params_kept():
    url = (
        "https://Careers.Example.com/jobs/?utm_source=x&gh_jid=123&gclid=a&fbclid=b&ref=c&source=d"
    )
    assert cat.canonical_url(url) == "https://careers.example.com/jobs?gh_jid=123"
    assert cat.canonical_url("https://x.example/jobs/1?utm_medium=m&ref=r#top") == (
        "https://x.example/jobs/1"
    )


def test_identity_params_are_ordered_so_the_identity_is_stable():
    assert cat.canonical_url("https://x.example/j?req=1&id=2") == cat.canonical_url(
        "https://x.example/j?id=2&req=1"
    )


async def test_two_postings_that_differ_only_by_an_id_param_are_two_jobs(cfg, conn):
    _category(conn)
    a = "https://careers.example.com/jobs?gh_jid=123"
    b = "https://careers.example.com/jobs?gh_jid=456"
    await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([_hit(a), _hit(b)]),
        read=_read_of(),
        adzuna_factory=lambda *_a: FakeAdz([]),
    )
    assert len(_jobs(conn, "category-web")) == 2


# ----------------------------------------------------------------------------- url filter


@pytest.mark.parametrize(
    "url",
    [
        "https://linkedin.com\\@evil.example/jobs/x",
        "https://evil.example\\@good.example/jobs/x",
        "https://user:pw@careers.example.com/jobs/x",
        "https://careers.example.com/jobs/x y",
        "https://careers.example.com/jobs/x\ty",
        "https://careers.example.com/jobs/x\x00",
        "https://lnkd.in/jobs/abc",
        "https://www.linkedin.com/jobs/view/1",
        "https://127.0.0.1/jobs/1",
        "https://10.0.0.5/jobs/1",
        "https://[::1]/jobs/1",
        "https://localhost/jobs/1",
        "https://printer.local/jobs/1",
        "https://wiki.internal/jobs/1",
        "https://nas.lan/jobs/1",
        "https://2130706433/jobs/1",
    ],
)
def test_unsafe_or_internal_urls_are_refused(url):
    assert cat.looks_like_category_posting(url, "CIO", KEEP, []) is False


def test_a_normal_url_still_passes():
    assert cat.looks_like_category_posting("https://careers.example.com/jobs/1", "CIO", KEEP, [])


async def test_a_backslash_url_never_reaches_a_read(cfg, conn):
    _category(conn)
    reads: list[str] = []
    await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([_hit("https://linkedin.com\\@evil.example/jobs/x")]),
        read=_read_of(seen=reads),
        adzuna_factory=lambda *_a: FakeAdz([]),
    )
    assert reads == [] and _jobs(conn) == []


# ----------------------------------------------------------------------------- adzuna bound


async def test_adzuna_queries_per_run_are_capped_at_ten(cfg, conn):
    for i in range(6):
        _category(conn, label=f"Group {i}", terms="alpha\nbeta\ngamma")
    seen: list = []

    def factory(cfg_, category):
        seen.append(category)
        return FakeAdz([])

    await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([]),
        read=_read_of(),
        adzuna_factory=factory,
    )
    assert sum(len(c.terms) for c in seen) == cat.MAX_SEARCHES_TOTAL


async def test_adzuna_terms_rotate_by_week_so_every_term_is_queried(cfg, conn):
    for i in range(4):
        _category(conn, label=f"Group {i}", terms="alpha\nbeta\ngamma")
    covered: set[tuple[int, str]] = set()
    for week in range(1, 5):
        day = dt.date.fromisocalendar(2026, week, WEDNESDAY.weekday() + 1)
        cfg.discovery.search_firm_weekday = day.weekday()

        def factory(cfg_, category):
            covered.update((category.id, t) for t in category.terms)
            return FakeAdz([])

        await cat.run_category_searches(
            cfg, conn, today=day, search=_search_of([]), read=_read_of(), adzuna_factory=factory
        )
    assert len(covered) == 12


def test_the_module_docstring_states_both_bounds():
    doc = cat.__doc__ or ""
    assert "Adzuna queries" in doc and "Jina searches" in doc and "ISO week" in doc


# ----------------------------------------------------------------------------- archived


async def test_a_hit_is_not_attached_to_an_archived_job(cfg, conn):
    from blastjob.db import client as dbc

    cid = _category(conn)
    jid = dbc.upsert_job(
        conn, source="category-web", external_id=GOOD, title="CIO", company="x", url=GOOD
    )
    conn.execute("UPDATE jobs SET review_state='archived' WHERE id=?", (jid,))
    conn.commit()
    reads: list[str] = []
    await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([_hit(GOOD)]),
        read=_read_of(seen=reads),
        adzuna_factory=lambda *_a: FakeAdz([_posting("arch")]),
    )
    assert _hits(conn) == [("arch", cid, 0)]
    assert reads == []


async def test_an_archived_adzuna_job_gets_no_hit(cfg, conn):
    from blastjob.db import client as dbc

    cid = _category(conn)
    jid = dbc.upsert_job(conn, source="adzuna", external_id="a1", title="CIO", company="Acme")
    conn.execute("UPDATE jobs SET review_state='archived' WHERE id=?", (jid,))
    conn.commit()
    await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([]),
        read=_read_of(),
        adzuna_factory=lambda *_a: FakeAdz([_posting("a1")]),
    )
    assert _hits(conn) == [] and cid


# ----------------------------------------------------------------------------- trust parity


def test_category_web_is_treated_exactly_like_a_search_firm_page():
    from blastjob.core import closure, closure_links
    from blastjob.core import employer_jd as ejd
    from blastjob.web import apply_link

    for source in ("category-web", "searchfirm:acme"):
        assert closure_links.is_authoritative(source) is False
        assert source not in ejd.AGGREGATOR_SOURCES
        for state in ("active", "stale"):
            assert closure._fallback_verdict({"source": source, "source_state": state}) == ""
        row = {
            "id": 1,
            "source": source,
            "company": "x",
            "url": "https://a.example/jobs/1",
            "source_state": "active",
            "cluster_id": None,
        }
        link = apply_link.best_apply_link([row])
        assert link is not None and link.url == "https://a.example/jobs/1"

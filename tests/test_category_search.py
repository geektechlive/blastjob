"""The weekly category role search (#91): Adzuna per category plus Jina web search."""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from blastjob import config as cfg_mod
from blastjob.core import category_store as cs
from blastjob.db import client as dbc
from blastjob.discovery import adzuna
from blastjob.discovery import category_search as cat
from blastjob.discovery.base import RawPosting
from blastjob.discovery.pipeline import run_discovery
from blastjob.models.config import BlastJobConfig
from tests.geo_fixture import apply_region

WEDNESDAY = dt.date(2026, 10, 28)
THURSDAY = dt.date(2026, 10, 29)
ID_ENV, KEY_ENV = "TEST_ADZUNA_ID", "TEST_ADZUNA_KEY"


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> BlastJobConfig:
    c = BlastJobConfig()
    apply_region(c)
    c.paths.data_dir = str(tmp_path)
    c.discovery.search_firm_weekday = WEDNESDAY.weekday()
    c.discovery.title_keep_terms = ["cio", "cto", "vp of it", "director of it", "chief"]
    c.discovery.title_drop_terms = ["engineer"]
    c.discovery.enable_aggregators = True
    c.discovery.adzuna_app_id_env = ID_ENV
    c.discovery.adzuna_app_key_env = KEY_ENV
    c.discovery.adzuna_where = "Harrowgate, OH"
    c.discovery.search_firm_places = ["Fairhaven"]
    cfg_mod.data_dir(c).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(ID_ENV, "id")
    monkeypatch.setenv(KEY_ENV, "key")
    return c


@pytest.fixture
def conn(cfg) -> sqlite3.Connection:
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _category(conn, label="Example robotics firms", terms="robotics startup", dream=False) -> int:
    return cs.add_category(conn, label=label, terms=terms, is_dream=dream)


def _hits(conn) -> list[tuple[str, int, int]]:
    return [
        (r["external_id"], r["category_id"], r["terms_version"])
        for r in conn.execute(
            "SELECT j.external_id, h.category_id, h.terms_version FROM category_hits h "
            "JOIN jobs j ON j.id = h.job_id ORDER BY j.external_id, h.category_id"
        )
    ]


def _jobs(conn, source: str | None = None) -> list[sqlite3.Row]:
    if source is None:
        return conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    return conn.execute("SELECT * FROM jobs WHERE source=? ORDER BY id", (source,)).fetchall()


def _posting(ext: str, title: str = "CIO", **kw) -> RawPosting:
    base = dict(
        source="adzuna",
        external_id=ext,
        company="Acme",
        title=title,
        url=f"https://example.com/{ext}",
        description="Lead technology.",
        location_raw="Fairhaven, OH",
        city="Fairhaven",
        region="OH",
    )
    return RawPosting(**{**base, **kw})


class FakeAdz:
    def __init__(self, postings, error: Exception | None = None):
        self.postings = postings
        self.error = error
        self.snapshot = False
        self.source = "adzuna"

    async def fetch(self):
        if self.error:
            raise self.error
        return self.postings


def _factory(by_slug: dict[str, FakeAdz], seen: list | None = None):
    def make(cfg, category):
        if seen is not None:
            seen.append(category)
        return by_slug.get(category.slug, FakeAdz([]))

    return make


def _forbid(name: str):
    async def _raise(*_a, **_k):
        pytest.fail(f"{name} must not run")

    return _raise


def _search_of(results_by_query: dict[str, list[dict]] | list[dict], seen: list[str] | None = None):
    async def search(query: str, key: str) -> list[dict]:
        if seen is not None:
            seen.append(query)
        if isinstance(results_by_query, dict):
            return results_by_query.get(query, [])
        return results_by_query

    return search


def _read_of(text: str = "Lead technology across the company.", seen: list[str] | None = None):
    async def read(url: str, key: str) -> str:
        if seen is not None:
            seen.append(url)
        return text

    return read


def _hit(url: str, title: str = "CIO") -> dict:
    return {"url": url, "title": title, "description": "snippet"}


GOOD = "https://boards.greenhouse.io/examplecorp/jobs/123"


# ----------------------------------------------------------------------------- weekly gate


async def test_nothing_runs_when_it_is_not_the_weekday(cfg, conn):
    _category(conn)
    res = await cat.run_category_searches(
        cfg,
        conn,
        today=THURSDAY,
        search=_forbid("search"),
        read=_forbid("read"),
        adzuna_factory=lambda *_a: pytest.fail("adzuna must not run"),
    )
    assert res.ran is False
    assert _jobs(conn) == []


async def test_one_adzuna_adapter_per_enabled_category(cfg, conn):
    a = _category(conn, label="Group A")
    b = _category(conn, label="Group B", dream=True)
    off = _category(conn, label="Group C")
    cs.set_enabled(conn, off, 0, False)
    seen: list = []
    res = await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([]),
        read=_read_of(),
        adzuna_factory=_factory({}, seen),
    )
    assert res.ran is True
    assert [c.id for c in seen] == [a, b]


async def test_adzuna_is_skipped_without_aggregators_or_keys(cfg, conn, monkeypatch):
    _category(conn)
    kw = dict(
        today=WEDNESDAY,
        search=_search_of([]),
        read=_read_of(),
        adzuna_factory=lambda *_a: pytest.fail("adzuna must not run"),
    )
    cfg.discovery.enable_aggregators = False
    assert (await cat.run_category_searches(cfg, conn, **kw)).ran is True
    cfg.discovery.enable_aggregators = True
    monkeypatch.delenv(KEY_ENV)
    assert (await cat.run_category_searches(cfg, conn, **kw)).ran is True


def test_the_adzuna_adapter_for_a_dream_category_searches_the_whole_country(cfg, conn):
    cid = _category(conn, terms="robotics startup\ndrone startup", dream=True)
    ad = cat.build_category_adzuna(cfg, cat.load_categories(conn)[0])
    assert (ad.queries, ad.where, ad.distance_km) == (["robotics startup", "drone startup"], "", 0)
    assert (
        ad.snapshot is False
        and ad.source == f"category-adzuna:{cs.get_category(conn, cid)['slug']}"
    )
    assert (ad.app_id_env, ad.app_key_env) == (ID_ENV, KEY_ENV)


def test_the_adzuna_adapter_for_a_normal_category_uses_the_normal_place(cfg, conn):
    _category(conn, dream=False)
    ad = cat.build_category_adzuna(cfg, cat.load_categories(conn)[0])
    assert ad.where == "Harrowgate, OH"
    assert ad.distance_km == int(cfg.discovery.commute_radius_miles * 1.60934)
    assert ad.snapshot is False


async def test_adzuna_sends_no_place_when_where_is_empty(monkeypatch):
    sent: list[dict] = []

    async def fake_get_json(url, *, params, **_kw):
        sent.append(params)
        return {"results": []}

    monkeypatch.setattr(adzuna, "get_json", fake_get_json)
    monkeypatch.setenv(ID_ENV, "id")
    monkeypatch.setenv(KEY_ENV, "key")
    kw = dict(queries=["q"], salary_min=0, app_id_env=ID_ENV, app_key_env=KEY_ENV)
    await adzuna.AdzunaAdapter(where="", distance_km=0, **kw).fetch()
    await adzuna.AdzunaAdapter(where="Harrowgate, OH", distance_km=40, **kw).fetch()
    assert "where" not in sent[0] and "distance" not in sent[0]
    assert sent[1]["where"] == "Harrowgate, OH" and sent[1]["distance"] == 40


# ----------------------------------------------------------------------------- adzuna results


async def test_adzuna_results_keep_the_adzuna_identity_and_get_a_hit(cfg, conn):
    cid = _category(conn)
    res = await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([]),
        read=_read_of(),
        adzuna_factory=_factory({"example-robotics-firms": FakeAdz([_posting("a1")])}),
    )
    assert res.saved == 1
    assert [(j["source"], j["external_id"]) for j in _jobs(conn)] == [("adzuna", "a1")]
    assert _hits(conn) == [("a1", cid, 0)]


async def test_the_main_search_and_two_categories_make_one_job_and_two_hits(cfg, conn):
    a = _category(conn, label="Group A")
    b = _category(conn, label="Group B")
    dbc.upsert_job(conn, source="adzuna", external_id="a1", title="CIO", company="Acme")
    first_seen = _jobs(conn)[0]["first_seen"]
    conn.execute("UPDATE jobs SET last_seen='2000-01-01T00:00:00'")
    conn.commit()
    shared = FakeAdz([_posting("a1")])
    res = await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([]),
        read=_read_of(),
        adzuna_factory=_factory({"group-a": shared, "group-b": shared}),
    )
    rows = _jobs(conn)
    assert len(rows) == 1 and res.saved == 0  # nothing new: one existing job
    assert rows[0]["first_seen"] == first_seen
    assert rows[0]["last_seen"] > "2000-01-01"  # refreshed like the main search
    assert _hits(conn) == [("a1", a, 0), ("a1", b, 0)]


async def test_a_title_outside_the_leadership_filter_is_never_saved(cfg, conn):
    _category(conn)
    adz = FakeAdz([_posting("a1", "Robotics Engineer"), _posting("a2", "CIO")])
    await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([]),
        read=_read_of(),
        adzuna_factory=_factory({"example-robotics-firms": adz}),
    )
    assert [j["external_id"] for j in _jobs(conn)] == ["a2"]


async def test_an_excluded_company_is_not_saved(cfg, conn):
    _category(conn)
    cfg.discovery.exclude_companies = ["Acme"]
    await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([]),
        read=_read_of(),
        adzuna_factory=_factory({"example-robotics-firms": FakeAdz([_posting("a1")])}),
    )
    assert _jobs(conn) == [] and _hits(conn) == []


async def test_new_jobs_per_category_are_capped(cfg, conn):
    _category(conn)
    adz = FakeAdz([_posting(f"a{i}") for i in range(cat.MAX_NEW_PER_CATEGORY + 5)])
    res = await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([]),
        read=_read_of(),
        adzuna_factory=_factory({"example-robotics-firms": adz}),
    )
    assert res.saved == cat.MAX_NEW_PER_CATEGORY
    assert len(_jobs(conn)) == cat.MAX_NEW_PER_CATEGORY
    assert any("cap" in note for note in res.notes.values())


# ----------------------------------------------------------------------------- queries


def test_the_query_scopes_the_term_over_every_leadership_title():
    q = cat.build_query("robotics startup")
    assert q == '"robotics startup" ("CIO" OR "VP of IT" OR "Director of IT" OR "CTO") job'


def test_a_quote_in_a_term_cannot_break_the_grouping():
    q = cat.build_query('robo "tics')
    assert q.startswith('"robo tics" (')
    assert q.count('"') == 10


async def test_one_search_per_term_with_the_built_query(cfg, conn):
    _category(conn, terms="alpha term\nbeta term")
    seen: list[str] = []
    await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([], seen),
        read=_read_of(),
        adzuna_factory=_factory({}),
    )
    assert seen == [cat.build_query("alpha term"), cat.build_query("beta term")]


# ----------------------------------------------------------------------------- the filter


@pytest.mark.parametrize(
    ("url", "title", "ok"),
    [
        (GOOD, "CIO", True),
        ("https://jobs.lever.co/examplecorp/abc", "CTO", True),
        ("https://jobs.ashbyhq.com/examplecorp/abc", "CTO", True),
        ("https://example.wd5.myworkdayjobs.com/en-US/site/job/x", "CIO", True),
        ("https://www.example.com/careers/chief-information-officer", "Chief Information", True),
        ("https://www.example.com/jobs/cio", "VP of IT", True),
        ("https://www.example.com/positions/12", "CIO", True),
        ("https://www.example.com/openings", "CIO", True),
        ("https://www.example.com/about-us", "CIO", False),  # no hint, not an ATS host
        ("https://www.linkedin.com/jobs/view/1", "CIO", False),
        ("https://uk.linkedin.com/jobs/view/1", "CIO", False),
        ("https://www.indeed.com/jobs/viewjob?jk=1", "CIO", False),
        ("https://www.glassdoor.com/job/1", "CIO", False),
        ("https://www.ziprecruiter.com/jobs/1", "CIO", False),
        ("https://www.adzuna.com/jobs/1", "CIO", False),
        ("https://www.example.com/blog/careers/why-a-cio", "CIO", False),
        ("https://www.example.com/news/jobs/new-cio", "CIO", False),
        ("https://www.example.com/jobs/robotics", "Robotics Engineer", False),  # title filter
        ("https://boards.greenhouse.io/x/jobs/1", "", False),
        ("javascript:alert(1)", "CIO", False),
        ("", "CIO", False),
    ],
)
def test_the_posting_classifier(cfg, url, title, ok):
    d = cfg.discovery
    assert cat.looks_like_category_posting(url, title, d.title_keep_terms, d.title_drop_terms) is ok


def test_a_lookalike_host_is_not_linkedin_or_an_aggregator(cfg):
    d = cfg.discovery
    assert cat.looks_like_category_posting(
        "https://notlinkedin.example/jobs/1", "CIO", d.title_keep_terms, d.title_drop_terms
    )


async def test_linkedin_and_unsafe_results_are_dropped_before_any_read(cfg, conn):
    _category(conn)
    reads: list[str] = []
    results = [
        _hit("https://www.linkedin.com/jobs/view/1"),
        _hit("javascript:alert(1)"),
        _hit("ftp://example.com/jobs/1"),
        _hit("https://www.example.com/blog/jobs/x"),
        _hit("https://www.example.com/jobs/robotics-eng", "Robotics Engineer"),
        _hit(GOOD),
    ]
    res = await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of(results),
        read=_read_of(seen=reads),
        adzuna_factory=_factory({}),
    )
    assert reads == [GOOD]
    assert [j["external_id"] for j in _jobs(conn)] == [GOOD]
    assert res.saved == 1


# ----------------------------------------------------------------------------- web saves


async def test_a_web_result_is_saved_under_category_web_with_its_url_as_identity(cfg, conn):
    cid = _category(conn)
    await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([_hit(GOOD + "?utm_source=x#top")]),
        read=_read_of("Lead technology. Based in Fairhaven."),
        adzuna_factory=_factory({}),
    )
    (job,) = _jobs(conn)
    assert (job["source"], job["external_id"]) == ("category-web", GOOD)
    assert job["url"] == GOOD + "?utm_source=x#top"
    assert "Fairhaven" in job["raw_jd"]
    assert _hits(conn) == [(GOOD, cid, 0)]


async def test_a_known_place_on_the_read_page_sets_the_location(cfg, conn):
    _category(conn)
    await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([_hit(GOOD)]),
        read=_read_of("CIO role based in Fairhaven."),
        adzuna_factory=_factory({}),
    )
    (job,) = _jobs(conn)
    assert (job["geo_location_raw"], job["geo_region"]) == ("Fairhaven", "OH")


async def test_an_unknown_place_is_left_unknown_never_guessed(cfg, conn):
    _category(conn)
    await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([_hit(GOOD)]),
        read=_read_of("CIO role at a robotics company."),
        adzuna_factory=_factory({}),
    )
    (job,) = _jobs(conn)
    assert (job["geo_location_raw"] or "", job["geo_region"] or "") == ("", "")
    assert not job["geo_remote"]


async def test_a_stored_url_gets_a_hit_without_a_re_read(cfg, conn):
    cid = _category(conn)
    dbc.upsert_job(
        conn, source="category-web", external_id=GOOD, title="CIO", company="x", url=GOOD
    )
    reads: list[str] = []
    res = await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([_hit(GOOD)]),
        read=_read_of(seen=reads),
        adzuna_factory=_factory({}),
    )
    assert reads == [] and res.saved == 0
    assert _hits(conn) == [(GOOD, cid, 0)]
    assert len(_jobs(conn)) == 1


async def test_a_url_stored_under_another_source_gets_the_hit_not_a_duplicate(cfg, conn):
    cid = _category(conn)
    dbc.upsert_job(
        conn,
        source="greenhouse:examplecorp",
        external_id="123",
        title="CIO",
        company="Example Corp",
        url=GOOD,
    )
    reads: list[str] = []
    await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([_hit(GOOD + "/")]),
        read=_read_of(seen=reads),
        adzuna_factory=_factory({}),
    )
    assert reads == []
    rows = _jobs(conn)
    assert [j["source"] for j in rows] == ["greenhouse:examplecorp"]
    assert _hits(conn) == [("123", cid, 0)]


def _many(n: int, host: str = "boards.greenhouse.io/c{i}/jobs/{i}") -> list[dict]:
    return [_hit("https://" + host.format(i=i)) for i in range(n)]


async def test_reads_are_capped_per_category_and_in_total(cfg, conn):
    for i in range(5):
        _category(conn, label=f"Group {i}", terms="one term")
    reads: list[str] = []
    seen: list[str] = []

    async def search(query: str, key: str) -> list[dict]:
        seen.append(query)
        n = len(seen)
        return [_hit(f"https://boards.greenhouse.io/c{n}x{i}/jobs/{i}") for i in range(8)]

    res = await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=search,
        read=_read_of(seen=reads),
        adzuna_factory=_factory({}),
    )
    assert len(reads) == cat.MAX_READS_TOTAL
    per_cat = {}
    for url in reads:
        per_cat[url.split("/")[3][:2]] = per_cat.get(url.split("/")[3][:2], 0) + 1
    assert max(per_cat.values()) <= cat.MAX_READS_PER_CATEGORY
    assert any("cap" in note for note in res.notes.values())


async def test_searches_are_capped_in_total(cfg, conn):
    for i in range(6):
        _category(conn, label=f"Group {i}", terms="alpha\nbeta\ngamma")
    seen: list[str] = []
    res = await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([], seen),
        read=_read_of(),
        adzuna_factory=_factory({}),
    )
    assert len(seen) == cat.MAX_SEARCHES_TOTAL == len(res.queries)


def test_the_search_window_rotates_by_iso_week_so_every_term_is_searched():
    cats = [
        cat.CategoryRef(i, f"c{i}", f"C{i}", ("a", "b", "c"), 0, False, True) for i in range(1, 5)
    ]  # 12 (category, term) pairs, more than the cap of 10
    covered: set[tuple[int, str]] = set()
    starts: set[tuple[int, str]] = set()
    for week in range(1, 5):
        day = dt.date.fromisocalendar(2026, week, 3)
        plan = cat.plan_searches(cats, day)
        assert len(plan) == cat.MAX_SEARCHES_TOTAL
        starts.add((plan[0][0].id, plan[0][1]))
        covered |= {(c.id, t) for c, t in plan}
    assert len(covered) == 12
    assert len(starts) > 1  # the window moved


def test_a_small_plan_is_searched_whole_every_week():
    cats = [cat.CategoryRef(1, "c", "C", ("a", "b"), 0, False, True)]
    for week in (1, 2, 3):
        plan = cat.plan_searches(cats, dt.date.fromisocalendar(2026, week, 3))
        assert [t for _c, t in plan] == ["a", "b"]


# ----------------------------------------------------------------------------- failure and edits


async def test_one_categorys_errors_do_not_stop_the_next(cfg, conn):
    bad = _category(conn, label="Group A")
    good = _category(conn, label="Group B")
    boom = FakeAdz([], error=RuntimeError("https://api.example.com/?app_key=SECRET failed"))
    ok = FakeAdz([_posting("a1")])

    async def search(query: str, key: str) -> list[dict]:
        if "Group A" in query or "alpha" in query:
            raise RuntimeError("search down")
        return []

    cs.update_category(conn, bad, 0, label="Group A", terms="alpha", is_dream=False)
    res = await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=search,
        read=_read_of(),
        adzuna_factory=_factory({"group-a": boom, "group-b": ok}),
    )
    assert _hits(conn) == [("a1", good, 0)]
    assert "group-a" in " ".join(res.errors)
    assert "SECRET" not in " ".join(res.errors.values())


async def test_a_failure_between_the_job_write_and_the_hit_rolls_both_back(cfg, conn, monkeypatch):
    _category(conn)

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("injected")

    monkeypatch.setattr(cs, "attach_hit", boom)
    res = await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([]),
        read=_read_of(),
        adzuna_factory=_factory({"example-robotics-firms": FakeAdz([_posting("a1")])}),
    )
    assert _jobs(conn) == [] and _hits(conn) == []
    assert res.errors


async def test_a_terms_change_during_the_run_discards_that_categorys_results(cfg, conn):
    cid = _category(conn, label="Group A")
    other = _category(conn, label="Group B", terms="drone startup")

    async def search(query: str, key: str) -> list[dict]:
        # The owner edits Group A's terms while the run is searching.
        row = cs.get_category(conn, cid)
        if row["terms"] == "robotics startup":
            cs.update_category(
                conn, cid, row["version"], label="Group A", terms="other", is_dream=False
            )
        return [_hit(GOOD)] if "robotics" in query else []

    res = await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=search,
        read=_read_of(),
        adzuna_factory=_factory(
            {"group-a": FakeAdz([_posting("a1")]), "group-b": FakeAdz([_posting("b1")])}
        ),
    )
    assert [j["external_id"] for j in _jobs(conn)] == ["b1"]
    assert _hits(conn) == [("b1", other, 0)]
    assert "group-a" in res.discarded


async def test_a_disabled_category_during_the_run_is_discarded_too(cfg, conn):
    cid = _category(conn)

    async def search(query: str, key: str) -> list[dict]:
        cs.set_enabled(conn, cid, cs.get_category(conn, cid)["version"], False)
        return [_hit(GOOD)]

    await cat.run_category_searches(
        cfg, conn, today=WEDNESDAY, search=search, read=_read_of(), adzuna_factory=_factory({})
    )
    assert _jobs(conn) == []


# ----------------------------------------------------------------------------- stale marking


class MainAdz:
    source = "adzuna"
    snapshot = True
    stale_after_days = 14

    def __init__(self, postings):
        self.postings = postings

    async def fetch(self):
        return self.postings


async def test_the_main_snapshot_does_not_stale_a_category_found_row(cfg, conn):
    _category(conn)
    await cat.run_category_searches(
        cfg,
        conn,
        today=WEDNESDAY,
        search=_search_of([]),
        read=_read_of(),
        adzuna_factory=_factory({"example-robotics-firms": FakeAdz([_posting("cat1")])}),
    )
    # A control row the main search stopped returning long ago: it SHOULD go stale.
    old = dbc.upsert_job(conn, source="adzuna", external_id="old", title="CIO", company="Old Co")
    conn.execute("UPDATE jobs SET last_seen='2000-01-01T00:00:00' WHERE id=?", (old,))
    for i in range(3):  # enough active rows that the thin-snapshot guard does not apply
        dbc.upsert_job(conn, source="adzuna", external_id=f"m{i}", title="CIO", company=f"Co {i}")
    conn.commit()
    main = MainAdz([_posting(f"m{i}", company=f"Co {i}") for i in range(3)])
    await run_discovery(cfg, conn, adapters=[main])
    states = {
        r["external_id"]: r["source_state"] for r in conn.execute("SELECT * FROM jobs").fetchall()
    }
    assert states["cat1"] == "active"
    assert states["old"] == "stale"

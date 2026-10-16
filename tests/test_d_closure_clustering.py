"""Group D review fixes: closure links vs 'different', metro region, recluster
transaction, aggregator hosts, filled outcomes, IP-pinned page fetches, and
research that refreshes facts without touching curated fields.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta

import httpx
import pytest

import tests.geo_fixture as geo_fixture
from blastjob.core import closure, recluster
from blastjob.core import company as co
from blastjob.core import packet_sends as ps
from blastjob.core import page_fetch as pf
from blastjob.core import role_clusters as rc
from blastjob.core.closure_links import LinkDeps
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import research

NOW = datetime(2026, 10, 16, 8, 0, 0)
GEO = geo_fixture.region_cfg()
RADIUS = BlastJobConfig().discovery.commute_radius_miles


@pytest.fixture
def cfg():
    c = BlastJobConfig()
    geo_fixture.apply_region(c)
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    c.discovery.closure_auto = True
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(conn, *, source, ext, company="Acme", title="VP of Ops", review="kept", app="",
         source_state="active", city="Oakbrook", region="OH", cluster_id=None) -> int:  # fmt: skip
    jid = dbc.upsert_job(
        conn, source=source, external_id=ext, company=company, title=title,
        geo_city=city, geo_region=region, geo_location_raw=f"{city}, {region}",
    )  # fmt: skip
    conn.execute(
        "UPDATE jobs SET review_state=?, application_state=?, source_state=?, cluster_id=?, "
        "first_seen='2026-10-01T00:00:00', last_seen='2026-10-15T00:00:00' WHERE id=?",
        (review, app, source_state, cluster_id, jid),
    )
    conn.commit()
    return jid


def _row(conn, jid):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


def _link_to(conn, jid: int, other: int) -> None:
    o = _row(conn, other)
    conn.execute(
        "UPDATE jobs SET employer_link_source=?, employer_link_external_id=?, "
        "employer_link_url='' WHERE id=?",
        (o["source"], o["external_id"], jid),
    )
    conn.commit()


def _deps() -> LinkDeps:
    async def none_list(*_a):
        return []

    async def no_check(_link):
        raise AssertionError("no network probe expected")

    return LinkDeps(
        probe_company=lambda *_a: [],
        probe_workday=lambda *_a: [],
        probe_dayforce=lambda *_a: [],
        fetch_board=none_list,
        check_link=no_check,
    )


# --- D1: a stored sibling link does not outlive a later 'different' -----------


def test_split_pair_clears_the_employer_links_between_the_pair(conn):
    board = _job(conn, source="greenhouse:acme", ext="g1", review="new")
    agg = _job(conn, source="linkedin", ext="l1")
    _link_to(conn, agg, board)

    rc.split_pair(conn, agg, board, RADIUS, GEO)

    row = _row(conn, agg)
    assert (row["employer_link_source"] or "") == ""
    assert (row["employer_link_external_id"] or "") == ""


def test_split_pair_keeps_a_link_to_an_unrelated_posting(conn):
    board = _job(conn, source="greenhouse:acme", ext="g1", review="new")
    other = _job(conn, source="greenhouse:acme", ext="g2", review="new")
    agg = _job(conn, source="linkedin", ext="l1")
    _link_to(conn, agg, other)

    rc.split_pair(conn, agg, board, RADIUS, GEO)

    assert _row(conn, agg)["employer_link_external_id"] == "g2"


def test_split_pair_does_not_clear_a_manual_link(conn):
    board = _job(conn, source="greenhouse:acme", ext="g1", review="new")
    agg = _job(conn, source="linkedin", ext="l1")
    conn.execute(
        "UPDATE jobs SET employer_link_source='manual', employer_link_external_id='g1' WHERE id=?",
        (agg,),
    )
    conn.commit()

    rc.split_pair(conn, agg, board, RADIUS, GEO)

    assert _row(conn, agg)["employer_link_source"] == "manual"


async def test_closure_drops_a_sibling_link_the_user_marked_different(cfg, conn):
    board = _job(conn, source="greenhouse:acme", ext="g1", review="new", source_state="stale")
    agg = _job(conn, source="linkedin", ext="l1")
    _link_to(conn, agg, board)
    rc.record_decision(conn, agg, board, rc.DIFFERENT)  # a stale link survives this
    conn.commit()

    await closure.run_closure(cfg, conn, deps=_deps(), now=NOW)

    row = _row(conn, agg)
    assert (row["employer_link_source"] or "") == ""
    assert row["posting_check"] == "unverifiable"
    assert row["application_state"] == ""  # no state move


async def test_closure_keeps_a_sibling_link_the_user_confirmed_same(cfg, conn):
    board = _job(conn, source="greenhouse:acme", ext="g1", review="new", source_state="stale")
    agg = _job(conn, source="linkedin", ext="l1", city="Elsewhere", region="TX")
    _link_to(conn, agg, board)
    rc.record_decision(conn, agg, board, rc.SAME)
    conn.commit()

    await closure.run_closure(cfg, conn, deps=_deps(), now=NOW)

    row = _row(conn, agg)
    assert row["employer_link_external_id"] == "g1"
    assert row["posting_check"] == "closed"


async def test_closure_drops_a_cluster_sibling_link_that_is_no_longer_same_metro(cfg, conn):
    cid = int(
        conn.execute(
            "INSERT INTO job_clusters (fingerprint, created_at) VALUES ('f','x')"
        ).lastrowid
    )
    board = _job(conn, source="greenhouse:acme", ext="g1", review="new", source_state="stale",
                 city="Rivermont", region="OH", cluster_id=cid)  # fmt: skip
    agg = _job(conn, source="linkedin", ext="l1", cluster_id=cid)
    _link_to(conn, agg, board)
    conn.commit()

    await closure.run_closure(cfg, conn, deps=_deps(), now=NOW)

    assert (_row(conn, agg)["employer_link_source"] or "") == ""


# --- D3: same city name is not the same metro across states --------------------


def _place(city, region):
    row = {
        "geo_city": city, "geo_region": region, "geo_location_raw": f"{city}, {region}",
        "geo_remote": 0,
    }  # fmt: skip
    return rc.place_of(row, GEO)


def test_same_city_name_in_two_states_is_not_the_same_metro():
    a, b = _place("Springfield", "IL"), _place("Springfield", "MO")
    assert not rc.same_metro(a, b, RADIUS, loose=False)


def test_same_city_name_in_the_same_state_is_the_same_metro():
    a, b = _place("Springfield", "IL"), _place("Springfield", "IL")
    assert rc.same_metro(a, b, RADIUS, loose=False)


def test_same_city_name_with_an_unknown_region_still_matches():
    a, b = _place("Springfield", ""), _place("Springfield", "MO")
    assert rc.same_metro(a, b, RADIUS, loose=False)


def test_centroid_radius_matching_still_crosses_state_lines():
    a = rc.Place(remote=False, city="alpha", centroid=(39.92, -83.00), region="OH")
    b = rc.Place(remote=False, city="beta", centroid=(39.95, -83.05), region="KY")
    assert rc.same_metro(a, b, RADIUS, loose=False)


# --- D4: apply_recluster is one write transaction ------------------------------


class _Spy:
    """Delegates to a real connection, recording the SQL that goes through."""

    def __init__(self, inner):
        self._inner = inner
        self.sql: list[str] = []

    def execute(self, sql, *args):
        self.sql.append(sql)
        return self._inner.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_apply_recluster_opens_with_begin_immediate(conn):
    spy = _Spy(conn)
    recluster.apply_recluster(spy, [])  # type: ignore[arg-type]
    assert spy.sql[:1] == ["BEGIN IMMEDIATE"]


def test_apply_recluster_still_merges(conn):
    a = _job(conn, source="adzuna", ext="a1", review="new")
    b = _job(conn, source="indeed", ext="i1", review="new")
    plan = [recluster.Merge(job_ids=(a, b), cluster_ids=(), company="Acme", title="VP of Ops")]
    assert recluster.apply_recluster(conn, plan) == 1
    assert _row(conn, a)["cluster_id"] == _row(conn, b)["cluster_id"] is not None
    assert not conn.in_transaction


# --- D5: link shorteners and the China LinkedIn domain are aggregators ---------


@pytest.mark.parametrize(
    "url",
    ["https://lnkd.in/abc", "https://www.linkedin.cn/jobs/view/1", "https://cn.linkedin.cn/x"],
)
def test_linkedin_shortener_and_cn_domain_are_never_fetchable(url):
    assert pf.is_aggregator_host(pf.host_of(url))
    assert not pf.fetchable(url)


# --- D6: the 21-day sweep records the packet outcome ---------------------------


def test_sweep_filled_records_a_filled_packet_outcome(conn):
    jid = _job(conn, source="linkedin", ext="l1", app="applied")
    conn.execute(
        "UPDATE jobs SET posting_check='closed', posting_closed_at='2026-09-01T00:00:00' "
        "WHERE id=?",
        (jid,),
    )
    conn.execute(
        "INSERT INTO packet_sends (job_id, resume_sha256, sent_at) VALUES (?, 'h', '2026-08-01')",
        (jid,),
    )
    conn.commit()

    assert closure.sweep_filled(conn, now=NOW, auto=True) == 1

    outcomes = [r["outcome"] for r in conn.execute("SELECT outcome FROM packet_outcomes")]
    assert outcomes == ["filled"]
    assert ps.latest_send_id(conn, jid) is not None


# --- SSRF: connect to the validated IP, keep Host and SNI ---------------------


def _recording_transport():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="<html></html>")

    return httpx.MockTransport(handler), seen


async def _resolver(_host: str) -> list[str]:
    return ["93.184.216.34"]


async def test_safe_get_connects_to_the_validated_ip_with_host_and_sni_of_the_name():
    transport, seen = _recording_transport()

    page = await pf.safe_get("https://careers.example.org/jobs/1", transport=transport,
                             resolve=_resolver)  # fmt: skip

    assert page is not None and page.url == "https://careers.example.org/jobs/1"
    request = seen[0]
    assert request.url.host == "93.184.216.34"
    assert request.headers["host"] == "careers.example.org"
    assert request.extensions["sni_hostname"] == "careers.example.org"


async def test_safe_get_keeps_a_non_default_port_in_the_host_header():
    transport, seen = _recording_transport()

    await pf.safe_get("http://careers.example.org:8080/x", transport=transport, resolve=_resolver)

    assert seen[0].url.port == 8080
    assert seen[0].headers["host"] == "careers.example.org:8080"


async def test_safe_get_does_not_resolve_a_second_time_after_validating():
    calls: list[str] = []

    async def counting(host: str) -> list[str]:
        calls.append(host)
        return ["93.184.216.34"]

    transport, _seen = _recording_transport()
    await pf.safe_get("https://careers.example.org/x", transport=transport, resolve=counting)
    assert calls == ["careers.example.org"]


def test_safe_get_client_ignores_proxy_environment(monkeypatch):
    seen: dict = {}
    real = httpx.AsyncClient

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(pf.httpx, "AsyncClient", spy)
    import asyncio

    transport, _ = _recording_transport()
    asyncio.run(pf.safe_get("https://careers.example.org/x", transport=transport,
                            resolve=_resolver))  # fmt: skip
    assert seen.get("trust_env") is False


# --- D2: research refreshes facts, never curated fields ------------------------


def _curated(**over) -> co.CompanyProfile:
    base = {
        "company": "Acme", "employees": 100, "revenue_usd": 5_000_000, "ceo": "Old Ceo",
        "workup_path": "/packets/acme/workup.md", "culture_rating": 4.5,
        "culture_notes": "my notes", "risks": "my risks", "questions": "my questions",
        "extra": {"mine": 1}, "summary": "my summary", "strategy": "my strategy",
    }  # fmt: skip
    return co.CompanyProfile(**{**base, **over})


def _fresh() -> co.CompanyProfile:
    return co.CompanyProfile(
        company="Acme", employees=250, revenue_usd=9_000_000, ceo="New Ceo",
        workup_path="/wrong", culture_rating=1.0, culture_notes="llm notes", risks="llm risks",
        questions="llm questions", extra={"llm": 1}, summary="llm summary", strategy="llm plan",
        tech_stack=[{"area": "erp", "vendor": "X"}],
    )  # fmt: skip


def test_refresh_updates_facts_and_leaves_curated_fields_alone(conn):
    co.upsert_profile(conn, _curated())
    seen = co.get_profile(conn, "Acme")["updated_at"]

    pid = co.refresh_research_facts(conn, _fresh(), expected_updated_at=seen)

    got = co.get_profile(conn, "Acme")
    assert pid == got["id"]
    assert (got["employees"], got["revenue_usd"], got["ceo"]) == (250, 9_000_000, "New Ceo")
    assert got["tech_stack"] == [{"area": "erp", "vendor": "X"}]
    assert got["workup_path"] == "/packets/acme/workup.md"
    assert got["culture_rating"] == 4.5 and got["culture_notes"] == "my notes"
    assert got["risks"] == "my risks" and got["questions"] == "my questions"
    assert got["extra"] == {"mine": 1}
    assert got["summary"] == "my summary" and got["strategy"] == "my strategy"


def test_refresh_fills_prose_that_was_empty(conn):
    co.upsert_profile(conn, _curated(summary="", strategy=""))
    seen = co.get_profile(conn, "Acme")["updated_at"]
    co.refresh_research_facts(conn, _fresh(), expected_updated_at=seen)
    got = co.get_profile(conn, "Acme")
    assert got["summary"] == "llm summary" and got["strategy"] == "llm plan"


def test_refresh_saves_nothing_when_the_profile_changed_meanwhile(conn):
    co.upsert_profile(conn, _curated())
    seen = co.get_profile(conn, "Acme")["updated_at"]
    conn.execute("UPDATE company_profiles SET updated_at='2099-01-01T00:00:00', ceo='Edited'")
    conn.commit()

    assert co.refresh_research_facts(conn, _fresh(), expected_updated_at=seen) is None

    got = co.get_profile(conn, "Acme")
    assert got["ceo"] == "Edited" and got["employees"] == 100


def test_refresh_inserts_a_new_profile_whole(conn):
    pid = co.refresh_research_facts(conn, _fresh(), expected_updated_at=None)
    got = co.get_profile(conn, "Acme")
    assert pid == got["id"] and got["risks"] == "llm risks" and got["employees"] == 250


def test_refresh_refuses_to_insert_over_a_profile_created_meanwhile(conn):
    co.upsert_profile(conn, _curated())
    assert co.refresh_research_facts(conn, _fresh(), expected_updated_at=None) is None
    assert co.get_profile(conn, "Acme")["ceo"] == "Old Ceo"


async def _run_work(monkeypatch, cfg, profile, *, on_research=None):
    events: list[tuple[str, str]] = []

    class _Run:
        def emit(self, kind, data):
            events.append((kind, data))

    async def fake_research(_cfg, _company, *, emit=None):
        if on_research:
            on_research()
        return profile

    monkeypatch.setattr(research, "research_company", fake_research)
    result = await research.make_research_work("Acme", lambda: cfg)(_Run())
    return result, events


async def test_research_work_refreshes_without_clobbering(monkeypatch, cfg, conn):
    co.upsert_profile(conn, _curated())

    result, events = await _run_work(monkeypatch, cfg, _fresh())

    got = co.get_profile(conn, "Acme")
    assert result == {"profile_id": got["id"]}
    assert got["ceo"] == "New Ceo" and got["risks"] == "my risks"
    assert not any(k == "error" for k, _ in events)


async def test_research_work_says_so_and_saves_nothing_on_a_conflict(monkeypatch, cfg, conn):
    co.upsert_profile(conn, _curated())

    def edit_meanwhile():
        conn.execute("UPDATE company_profiles SET updated_at='2099-01-01T00:00:00', ceo='Edited'")
        conn.commit()

    result, events = await _run_work(monkeypatch, cfg, _fresh(), on_research=edit_meanwhile)

    assert result is None
    assert co.get_profile(conn, "Acme")["ceo"] == "Edited"
    messages = [d for k, d in events if k == "error"]
    assert messages and "changed while" in messages[0]


def test_every_profile_column_has_exactly_one_owner():
    owned = [*co.RESEARCH_OWNED, *co.FILL_IF_EMPTY, *co.USER_OWNED, "company"]
    assert sorted(owned) == sorted(co.PROFILE_COLUMNS)


async def test_a_rejected_pairing_is_not_re_linked_by_the_network_search(cfg, conn):
    from blastjob.discovery.base import RawPosting
    from blastjob.discovery.resolve import BoardHit

    board = _job(conn, source="greenhouse:acme", ext="g1", review="new")
    agg = _job(conn, source="linkedin", ext="l1")
    _link_to(conn, agg, board)
    rc.split_pair(conn, agg, board, RADIUS, GEO)  # 'different': link cleared

    async def fetch_board(*_a):
        return [RawPosting(source="greenhouse:acme", external_id="g1", title="VP of Ops")]

    deps = LinkDeps(
        probe_company=lambda _n: [BoardHit("Acme", "greenhouse", "acme", 4)],
        probe_workday=lambda *_a: [],
        probe_dayforce=lambda *_a: [],
        fetch_board=fetch_board,
        check_link=_deps().check_link,
    )
    for day in (0, 1):
        await closure.run_closure(cfg, conn, deps=deps, now=NOW + timedelta(days=day))
        assert (_row(conn, agg)["employer_link_source"] or "") == ""


def test_safe_get_client_never_keeps_a_connection_for_reuse(monkeypatch):
    import asyncio

    seen: dict = {}
    real = httpx.AsyncClient

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(pf.httpx, "AsyncClient", spy)
    transport, _ = _recording_transport()
    asyncio.run(
        pf.safe_get("https://careers.example.org/x", transport=transport, resolve=_resolver)
    )
    assert seen["limits"].max_keepalive_connections == 0


async def test_safe_get_allow_aggregators_fetches_an_aggregator_but_never_linkedin():
    """enrich.py needs Adzuna/Indeed pages, so it opts in to aggregators; LinkedIn
    stays refused either way (project rule), and the default still refuses both."""
    import httpx

    from blastjob.core import page_fetch

    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("host", ""))
        return httpx.Response(200, text="ok")

    async def resolve(_host: str) -> list[str]:
        return ["93.184.216.34"]

    t = httpx.MockTransport(handler)
    url = "https://www.indeed.com/viewjob?jk=1"
    assert await page_fetch.safe_get(url, transport=t, resolve=resolve) is None
    page = await page_fetch.safe_get(url, transport=t, resolve=resolve, allow_aggregators=True)
    assert page is not None and page.status == 200
    for li in ("https://www.linkedin.com/jobs/view/1", "https://lnkd.in/abc"):
        assert (
            await page_fetch.safe_get(li, transport=t, resolve=resolve, allow_aggregators=True)
            is None
        )
    assert seen == ["www.indeed.com"]

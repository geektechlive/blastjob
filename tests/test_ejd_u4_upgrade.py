"""Employer-JD unit U4 (issue #17): `core/employer_jd.run_upgrade`.

Free steps (revalidate, sibling copy) and the budgeted network steps, with
injected `LinkDeps` fakes. Nothing here touches the network.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta

import pytest

import tests.geo_fixture as geo_fixture
from blastjob.core import employer_jd as ej
from blastjob.core import role_clusters as rc
from blastjob.core.closure_links import LinkDeps
from blastjob.db import client as dbc
from blastjob.discovery.base import RawPosting
from blastjob.discovery.resolve import BoardHit
from blastjob.models.config import BlastJobConfig

NOW = datetime(2026, 10, 16, 8, 0, 0)
SNIPPET = "Short aggregator snippet."
JD = "VP of Ops: you will lead operations across the whole company. " * 14
NO_TITLE_JD = "Responsibilities include budgeting and staffing for all regional sites. " * 12
JD2 = "VP of Ops: a revised description that now covers acquisitions as well. " * 14
BOARD = "greenhouse:acme"


@pytest.fixture
def cfg():
    c = BlastJobConfig()
    geo_fixture.apply_region(c)
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(
    conn, *, source="adzuna", ext="a1", company="Acme", title="VP of Ops", city="Oakbrook",
    review="kept", jd=SNIPPET, cluster_id=None, score=None, source_state="active",
) -> int:  # fmt: skip
    jid = dbc.upsert_job(
        conn, source=source, external_id=ext, company=company, title=title,
        geo_city=city, geo_region="OH", geo_location_raw=f"{city}, OH", raw_jd=jd,
    )  # fmt: skip
    conn.execute(
        "UPDATE jobs SET review_state=?, source_state=?, cluster_id=?, alignment_score=?, "
        "rationale=?, first_seen='2026-10-01T00:00:00', last_seen='2026-10-15T00:00:00' "
        "WHERE id=?",
        (review, source_state, cluster_id, score, "because" if score is not None else "", jid),
    )
    conn.commit()
    return jid


def _cluster(conn) -> int:
    cid = rc._new_cluster(conn, "t")
    conn.commit()
    return cid


def _pair(conn, *, board_city="Oakbrook", review="kept", score=None, board_jd=JD):
    """An aggregator snippet row and a clustered board sibling holding `board_jd`."""
    cid = _cluster(conn)
    board = _job(conn, source=BOARD, ext="g1", review="new", jd=board_jd, cluster_id=cid,
                 city=board_city)  # fmt: skip
    agg = _job(conn, ext="a1", review=review, cluster_id=cid, score=score)
    return agg, board


def _row(conn, jid):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


def _dump(conn) -> str:
    return "\n".join(conn.iterdump())


async def _run(cfg, conn, **kw):
    kw.setdefault("min_score", None)
    kw.setdefault("network", False)
    kw.setdefault("write", True)
    kw.setdefault("request_limit", 50)
    kw.setdefault("now", NOW)
    return await ej.run_upgrade(cfg, conn, **kw)


def _status(res, jid) -> str:
    found = [o.status for o in res.outcomes if o.job_id == jid]
    return found[0] if len(found) == 1 else f"<{len(found)} outcomes>"


# --- fakes -------------------------------------------------------------------------


class Net:
    """Injected board access; counts every call that would cost a request."""

    def __init__(self, *, text=JD, ext="g1", title="VP of Ops", hits=True, fail=False,
                 on_fetch=None, known=None, first_blank=False):  # fmt: skip
        self.text, self.ext, self.title = text, ext, title
        self.hits, self.fail, self.on_fetch = hits, fail, on_fetch
        self.known = known  # company names that have a board; None = all
        self.first_blank = first_blank  # listing without text; the 2nd fetch has it
        self.fetches = 0
        self.calls: list[str] = []

    def probe_company(self, name):
        self.calls.append(f"probe:{name}")
        if self.fail:
            raise RuntimeError("boom")
        if not self.hits or (self.known is not None and name not in self.known):
            return []
        return [BoardHit(name, "greenhouse", name.lower().replace(" ", ""), 4)]

    async def fetch_board(self, ats, token, title):
        self.calls.append("fetch")
        self.fetches += 1
        if self.on_fetch and (self.fetches == 2 or not self.first_blank):
            self.on_fetch()
        text = "" if self.first_blank and self.fetches == 1 else self.text
        return [RawPosting(source=f"greenhouse:{token}", external_id=self.ext, title=self.title,
                           url=f"https://x/{self.ext}", description=text, region="OH")]  # fmt: skip

    async def check_link(self, link):
        raise AssertionError("no probe expected")

    def deps(self) -> LinkDeps:
        return LinkDeps(
            probe_company=self.probe_company,
            probe_workday=lambda *_a: [],
            probe_dayforce=lambda *_a: [],
            fetch_board=self.fetch_board,
            check_link=self.check_link,
        )

    @property
    def probes(self) -> list[str]:
        return [c for c in self.calls if c.startswith("probe:")]


def _other_conn(cfg):
    c = dbc.connect(cfg)
    return c


# --- candidates --------------------------------------------------------------------


def test_aggregator_sources_are_the_real_source_values():
    assert ej.AGGREGATOR_SOURCES == ("adzuna", "linkedin", "indeed")


def test_candidates_select_kept_and_active_undecided_snippets_only(conn):
    kept = _job(conn, ext="k", review="kept")
    new = _job(conn, ext="n", review="new")
    queued = _job(conn, ext="q", review="queued")
    _job(conn, ext="arch", review="archived")
    _job(conn, ext="stale", review="new", source_state="closed")
    _job(conn, ext="full", review="kept", jd=JD)
    _job(conn, source="greenhouse:acme", ext="board", review="new")
    pasted = _job(conn, ext="paste", review="kept")
    conn.execute("UPDATE jobs SET jd_pasted_at='2026-10-01' WHERE id=?", (pasted,))
    emp = _job(conn, ext="emp", review="kept")
    conn.execute("UPDATE jobs SET jd_source='employer:greenhouse:acme' WHERE id=?", (emp,))
    li = _job(conn, source="linkedin", ext="l1", review="kept")
    conn.commit()

    ids = {r["id"] for r in ej.candidates(conn)}

    assert ids == {kept, new, queued, li}
    assert {r["id"] for r in ej.candidates(conn, job_ids=[new, li])} == {new, li}
    assert ej.candidates(conn, job_ids=[]) == []


# --- free copy ---------------------------------------------------------------------


async def test_sibling_copy_adopts_the_full_text_and_clears_an_undecided_score(cfg, conn):
    agg, _board = _pair(conn, review="surfaced", score=70)

    res = await _run(cfg, conn)

    row = _row(conn, agg)
    assert row["raw_jd"].strip() == JD.strip()
    assert row["jd_source"] == "employer:greenhouse:acme" and row["jd_source_ref"] == "g1"
    assert row["jd_prior"] == SNIPPET and row["alignment_score"] is None
    (o,) = [o for o in res.outcomes if o.job_id == agg]
    assert (o.status, o.via, o.before_chars) == ("upgraded", "copy", len(SNIPPET))
    assert o.after_chars == len(row["raw_jd"]) and o.jd_source == "employer:greenhouse:acme"
    assert res.upgraded == [agg] and res.requests_used == 0
    assert not conn.in_transaction


async def test_kept_row_keeps_its_score_and_is_flagged_stale(cfg, conn):
    agg, _board = _pair(conn, review="kept", score=82)

    res = await _run(cfg, conn)

    row = _row(conn, agg)
    assert row["alignment_score"] == 82 and row["score_stale"] == 1
    assert res.stale_kept == [agg]


async def test_free_copy_ignores_the_score_floor(cfg, conn):
    agg, _board = _pair(conn, review="surfaced", score=5)

    res = await _run(cfg, conn, min_score=60)

    assert _status(res, agg) == "upgraded"


async def test_wrong_metro_sibling_is_not_copied(cfg, conn):
    agg, _board = _pair(conn, board_city="Rivermont")

    res = await _run(cfg, conn)

    assert _row(conn, agg)["raw_jd"] == SNIPPET
    assert _status(res, agg) == "no_free_copy" and res.upgraded == []


async def test_aggregator_sourced_sibling_is_not_copied(cfg, conn):
    cid = _cluster(conn)
    _job(conn, source="linkedin", ext="l1", review="new", jd=JD, cluster_id=cid)
    agg = _job(conn, ext="a1", cluster_id=cid)

    await _run(cfg, conn)

    assert _row(conn, agg)["raw_jd"] == SNIPPET


async def test_pasted_sibling_is_not_copied(cfg, conn):
    agg, board = _pair(conn)
    conn.execute("UPDATE jobs SET jd_pasted_at='2026-10-02' WHERE id=?", (board,))
    conn.commit()

    res = await _run(cfg, conn)

    assert _row(conn, agg)["raw_jd"] == SNIPPET
    assert _status(res, agg) == "sibling_not_full"


async def test_a_snippet_sibling_is_not_copied(cfg, conn):
    agg, _board = _pair(conn, board_jd="tiny")

    res = await _run(cfg, conn)

    assert _row(conn, agg)["raw_jd"] == SNIPPET
    assert _status(res, agg) == "sibling_not_full"


async def test_two_qualifying_siblings_copy_nothing(cfg, conn):
    agg, _board = _pair(conn)
    _job(
        conn, source=BOARD, ext="g2", review="new", jd=JD2, cluster_id=_row(conn, agg)["cluster_id"]
    )

    await _run(cfg, conn)

    assert _row(conn, agg)["raw_jd"] == SNIPPET


async def test_a_rejected_pair_is_never_adopted(cfg, conn):
    agg, board = _pair(conn)
    rc.record_decision(conn, agg, board, rc.DIFFERENT)
    conn.commit()

    await _run(cfg, conn)

    assert _row(conn, agg)["raw_jd"] == SNIPPET


async def test_a_body_without_any_title_word_is_refused_as_not_a_jd(cfg, conn):
    agg, _board = _pair(conn, board_jd=NO_TITLE_JD)

    res = await _run(cfg, conn)

    assert _row(conn, agg)["raw_jd"] == SNIPPET
    assert _status(res, agg) == "not_a_jd"


async def test_a_sibling_text_too_short_after_normalizing_is_refused(cfg, conn):
    markup = "<div><span></span></div>" * 40 + "VP of Ops"
    agg, _board = _pair(conn, board_jd=markup)

    res = await _run(cfg, conn)

    assert _row(conn, agg)["raw_jd"] == SNIPPET
    assert _status(res, agg) == "short_text"


async def test_a_second_run_is_idempotent(cfg, conn):
    agg, _board = _pair(conn, review="kept", score=80)
    await _run(cfg, conn)
    conn.execute("UPDATE jobs SET score_stale=0 WHERE id=?", (agg,))
    conn.commit()
    before = _dump(conn)

    res = await _run(cfg, conn)

    assert _dump(conn) == before and res.outcomes == [] and res.upgraded == []
    assert _row(conn, agg)["score_stale"] == 0


async def test_write_false_leaves_the_database_byte_identical(cfg, conn):
    agg, _board = _pair(conn, review="surfaced", score=70)
    before = _dump(conn)
    changes = conn.total_changes

    res = await _run(cfg, conn, write=False)

    assert _dump(conn) == before and conn.total_changes == changes
    assert _status(res, agg) == "would_upgrade" and res.upgraded == []


async def test_job_ids_limits_the_run(cfg, conn):
    agg, _board = _pair(conn)
    other = _job(conn, ext="a9", company="Other", jd=SNIPPET)

    res = await _run(cfg, conn, job_ids=[other])

    assert _row(conn, agg)["raw_jd"] == SNIPPET
    assert all(o.job_id == other for o in res.outcomes)


# --- local revalidation of adopted rows ----------------------------------------------


async def test_a_split_after_adoption_reverts_the_text(cfg, conn):
    agg, board = _pair(conn, review="surfaced", score=70)
    await _run(cfg, conn)
    rc.record_decision(conn, agg, board, rc.DIFFERENT)
    conn.commit()

    res = await _run(cfg, conn)

    row = _row(conn, agg)
    assert row["raw_jd"] == SNIPPET and row["jd_source"] == ""
    assert res.reverted == [agg] and _status(res, agg) == "reverted"


async def test_revert_write_false_changes_nothing(cfg, conn):
    agg, board = _pair(conn)
    await _run(cfg, conn)
    rc.record_decision(conn, agg, board, rc.DIFFERENT)
    conn.commit()
    before = _dump(conn)

    res = await _run(cfg, conn, write=False)

    assert _dump(conn) == before and _status(res, agg) == "would_revert"
    assert res.reverted == []


async def test_a_revised_sibling_text_is_adopted_once(cfg, conn):
    agg, board = _pair(conn, review="kept", score=80)
    await _run(cfg, conn)
    conn.execute("UPDATE jobs SET score_stale=0 WHERE id=?", (agg,))
    conn.execute("UPDATE jobs SET raw_jd=? WHERE id=?", (JD2, board))
    conn.commit()

    res = await _run(cfg, conn)

    row = _row(conn, agg)
    assert row["raw_jd"].strip() == JD2.strip() and row["jd_prior"] == SNIPPET
    assert row["score_stale"] == 1 and _status(res, agg) == "revised"
    assert res.upgraded == [agg]
    conn.execute("UPDATE jobs SET score_stale=0 WHERE id=?", (agg,))
    conn.commit()
    assert (await _run(cfg, conn)).outcomes == []
    assert _row(conn, agg)["score_stale"] == 0


async def test_an_adoption_whose_posting_is_not_held_is_left_alone(cfg, conn):
    agg = _job(conn, ext="a1")
    conn.execute(
        "UPDATE jobs SET raw_jd=?, jd_source='employer:greenhouse:acme', jd_source_ref='gX', "
        "jd_prior=? WHERE id=?",
        (JD, SNIPPET, agg),
    )
    conn.commit()
    before = _dump(conn)

    res = await _run(cfg, conn)

    assert _dump(conn) == before and res.outcomes == []


# --- network: a fresh search, budget, backoff ----------------------------------------


async def test_a_fresh_search_match_lands_with_its_link_and_a_found_lookup(cfg, conn):
    agg = _job(conn, review="surfaced", score=80)
    net = Net()

    res = await _run(cfg, conn, network=True, deps=net.deps(), min_score=60)

    row = _row(conn, agg)
    assert row["raw_jd"].strip() == JD.strip()
    assert (row["jd_source"], row["jd_source_ref"]) == ("employer:greenhouse:acme", "g1")
    assert (row["employer_link_source"], row["employer_link_external_id"]) == (BOARD, "g1")
    assert row["jd_lookup_status"] == "found" and row["alignment_score"] is None
    (o,) = res.outcomes
    assert (o.status, o.via) == ("upgraded", "search")
    # probe_company (9 units) + one board listing (1)
    assert res.upgraded == [agg] and res.requests_used == 10
    assert not conn.in_transaction


async def test_write_false_network_report_writes_nothing(cfg, conn):
    agg = _job(conn, review="surfaced", score=80)
    before = _dump(conn)
    changes = conn.total_changes

    res = await _run(cfg, conn, network=True, deps=Net().deps(), write=False)

    assert _dump(conn) == before and conn.total_changes == changes
    assert _status(res, agg) == "would_upgrade" and res.requests_used == 10


async def test_the_request_budget_counts_requests_and_stops_cleanly(cfg, conn):
    first = _job(conn, ext="a1", company="Acme", score=90, review="surfaced")
    second = _job(conn, ext="a2", company="Beta", score=80, review="surfaced")
    third = _job(conn, ext="a3", company="Gamma", score=70, review="surfaced")
    net = Net()

    res = await _run(cfg, conn, network=True, deps=net.deps(), request_limit=10)

    assert _status(res, first) == "upgraded"
    assert _status(res, second) == "cap" and _status(res, third) == "cap"
    assert res.requests_used == 10 and len(net.calls) == 2
    assert _row(conn, second)["jd_lookup_status"] in (None, "")  # a cap records nothing
    assert _row(conn, second)["raw_jd"] == SNIPPET


async def test_zero_budget_makes_no_calls(cfg, conn):
    agg = _job(conn, review="surfaced", score=90)
    net = Net()

    res = await _run(cfg, conn, network=True, deps=net.deps(), request_limit=0)

    assert net.calls == [] and _status(res, agg) == "cap"


async def test_network_off_never_calls_deps(cfg, conn):
    agg = _job(conn, review="surfaced", score=90)
    net = Net()

    res = await _run(cfg, conn, network=False, deps=net.deps())

    assert net.calls == [] and _status(res, agg) == "no_free_copy"


async def test_no_match_backs_off_for_14_days(cfg, conn):
    agg = _job(conn, review="surfaced", score=80)
    net = Net(hits=False)
    res = await _run(cfg, conn, network=True, deps=net.deps())
    assert _status(res, agg) == "no_match"
    assert _row(conn, agg)["jd_lookup_status"] == "no_match"

    again = await _run(cfg, conn, network=True, deps=net.deps(), now=NOW + timedelta(days=13))
    assert _status(again, agg) == "backoff" and len(net.probes) == 1

    later = await _run(cfg, conn, network=True, deps=net.deps(), now=NOW + timedelta(days=15))
    assert _status(later, agg) == "no_match" and len(net.probes) == 2


async def test_transient_failure_retries_after_a_day_and_ignoring_backoff_works(cfg, conn):
    agg = _job(conn, review="surfaced", score=80)
    net = Net(fail=True)
    res = await _run(cfg, conn, network=True, deps=net.deps())
    row = _row(conn, agg)
    assert _status(res, agg) == "transient"
    assert (row["jd_lookup_status"], row["jd_lookup_failures"]) == ("transient", 1)

    soon = await _run(cfg, conn, network=True, deps=net.deps(), now=NOW + timedelta(hours=12))
    assert _status(soon, agg) == "backoff"
    day = await _run(cfg, conn, network=True, deps=net.deps(), now=NOW + timedelta(days=1))
    assert _status(day, agg) == "transient" and _row(conn, agg)["jd_lookup_failures"] == 2
    forced = await _run(
        cfg, conn, network=True, deps=net.deps(), now=NOW + timedelta(days=1), respect_backoff=False
    )
    assert _status(forced, agg) == "transient"


async def test_agencies_and_missing_companies_spend_no_budget(cfg, conn):
    agency = _job(conn, ext="a1", company="Northside Staffing", review="surfaced", score=90)
    nameless = _job(conn, ext="a2", company="", review="surfaced", score=90)
    net = Net()

    res = await _run(cfg, conn, network=True, deps=net.deps())

    assert _status(res, agency) == "agency" and _status(res, nameless) == "no_company"
    assert net.calls == [] and res.requests_used == 0


async def test_score_floor_applies_to_undecided_rows_only(cfg, conn):
    low = _job(conn, ext="a1", company="Acme", review="surfaced", score=50)
    unscored = _job(conn, ext="a2", company="Beta", review="surfaced", score=None)
    kept = _job(conn, ext="a3", company="Gamma", review="kept", score=None)
    net = Net()

    res = await _run(cfg, conn, network=True, deps=net.deps(), min_score=60)

    assert _status(res, low) == "below_floor" and _status(res, unscored) == "unscored"
    assert _status(res, kept) == "upgraded"
    assert net.probes == ["probe:Gamma"]


async def test_no_floor_means_every_undecided_row_is_eligible(cfg, conn):
    low = _job(conn, ext="a1", company="Acme", review="surfaced", score=5)

    res = await _run(cfg, conn, network=True, deps=Net().deps(), min_score=None)

    assert _status(res, low) == "upgraded"


async def test_network_order_is_kept_first_then_score_desc_then_id(cfg, conn):
    _job(conn, ext="a1", company="Low", review="surfaced", score=70)
    _job(conn, ext="a2", company="High", review="surfaced", score=95)
    _job(conn, ext="a3", company="Kept", review="kept", score=10)
    net = Net(hits=False)

    await _run(cfg, conn, network=True, deps=net.deps())

    assert net.probes == ["probe:Kept", "probe:High", "probe:Low"]


async def test_skip_search_ids_are_not_searched(cfg, conn):
    skipped = _job(conn, ext="a1", company="Acme", review="surfaced", score=90)
    other = _job(conn, ext="a2", company="Beta", review="surfaced", score=80)
    net = Net()

    res = await _run(cfg, conn, network=True, deps=net.deps(), skip_search_ids={skipped})

    assert _status(res, skipped) == "closure_searched" and _status(res, other) == "upgraded"
    assert net.probes == ["probe:Beta"]


async def test_a_body_without_the_title_is_refused_but_the_link_is_kept(cfg, conn):
    agg = _job(conn, review="surfaced", score=90)

    res = await _run(cfg, conn, network=True, deps=Net(text=NO_TITLE_JD).deps())

    row = _row(conn, agg)
    assert _status(res, agg) == "not_a_jd" and row["raw_jd"] == SNIPPET
    assert row["employer_link_source"] == BOARD and row["jd_lookup_status"] == "no_match"


async def test_a_match_the_user_rejected_is_not_adopted(cfg, conn):
    agg = _job(conn, review="surfaced", score=90)
    decoy = _job(conn, source=BOARD, ext="g1", company="Other Co", review="new", jd="x")
    rc.record_decision(conn, agg, decoy, rc.DIFFERENT)
    conn.commit()

    res = await _run(cfg, conn, network=True, deps=Net().deps())

    row = _row(conn, agg)
    assert _status(res, agg) == "rejected" and row["raw_jd"] == SNIPPET
    assert (row["employer_link_source"] or "") == ""


async def test_a_stored_link_not_held_in_the_db_is_fetched_and_revalidated(cfg, conn):
    agg = _job(conn, review="surfaced", score=90)
    conn.execute(
        "UPDATE jobs SET employer_link_source=?, employer_link_external_id='g1', "
        "employer_link_url='https://x/g1' WHERE id=?",
        (BOARD, agg),
    )
    conn.commit()

    res = await _run(cfg, conn, network=True, deps=Net().deps())

    (o,) = res.outcomes
    assert (o.status, o.via) == ("upgraded", "link")
    assert _row(conn, agg)["jd_source_ref"] == "g1"


async def test_a_stored_link_whose_id_now_names_another_role_is_refused(cfg, conn):
    agg = _job(conn, review="surfaced", score=90)
    conn.execute(
        "UPDATE jobs SET employer_link_source=?, employer_link_external_id='g1' WHERE id=?",
        (BOARD, agg),
    )
    conn.commit()

    res = await _run(cfg, conn, network=True, deps=Net(title="Janitor").deps())

    assert _status(res, agg) == "title_drift"
    assert _row(conn, agg)["raw_jd"] == SNIPPET
    assert _row(conn, agg)["jd_lookup_status"] == "no_match"


async def test_a_manual_link_row_is_never_fetched_or_searched(cfg, conn):
    agg = _job(conn, review="surfaced", score=90)
    conn.execute("UPDATE jobs SET employer_link_source='manual' WHERE id=?", (agg,))
    conn.commit()
    net = Net()

    res = await _run(cfg, conn, network=True, deps=net.deps())

    assert net.calls == [] and _status(res, agg) == "manual_link"


async def test_a_row_that_raises_is_reported_and_the_run_continues(cfg, conn, monkeypatch):
    bad = _job(conn, ext="a1", company="Acme", review="surfaced", score=90)
    good = _job(conn, ext="a2", company="Beta", review="surfaced", score=80)
    real = dbc.jd_snapshot

    def flaky(c, job_id):
        if job_id == bad:
            raise ValueError("kaput")
        return real(c, job_id)

    monkeypatch.setattr(dbc, "jd_snapshot", flaky)

    res = await _run(cfg, conn, network=True, deps=Net().deps())

    (o,) = [o for o in res.outcomes if o.job_id == bad]
    assert o.status == "error" and "ValueError" in o.note
    assert _status(res, good) == "upgraded"


# --- races during the network await ----------------------------------------------------


def _race(cfg, sql: str, params=()):
    def mutate():
        c = _other_conn(cfg)
        c.execute(sql, params)
        c.commit()
        c.close()

    return mutate


async def test_archiving_during_the_fetch_loses_the_race(cfg, conn):
    agg = _job(conn, review="surfaced", score=90)
    net = Net(on_fetch=_race(cfg, "UPDATE jobs SET review_state='archived' WHERE id=?", (agg,)))

    res = await _run(cfg, conn, network=True, deps=net.deps())

    row = _row(conn, agg)
    assert _status(res, agg) == "lost_race"
    assert row["raw_jd"] == SNIPPET and row["jd_source"] == "" and res.upgraded == []
    assert (row["employer_link_source"] or "") == ""


async def test_a_paste_during_the_fetch_wins(cfg, conn):
    agg = _job(conn, review="surfaced", score=90)
    net = Net(on_fetch=_race(
        cfg, "UPDATE jobs SET jd_pasted_at='2026-10-16', raw_jd='my paste' WHERE id=?", (agg,)
    ))  # fmt: skip

    res = await _run(cfg, conn, network=True, deps=net.deps())

    assert _status(res, agg) == "lost_race" and _row(conn, agg)["raw_jd"] == "my paste"


async def test_an_edit_of_the_snippet_during_the_fetch_loses_the_race(cfg, conn):
    agg = _job(conn, review="surfaced", score=90)
    net = Net(on_fetch=_race(cfg, "UPDATE jobs SET raw_jd='refreshed' WHERE id=?", (agg,)))

    res = await _run(cfg, conn, network=True, deps=net.deps())

    assert _status(res, agg) == "lost_race" and _row(conn, agg)["raw_jd"] == "refreshed"


async def test_a_split_during_the_fetch_loses_the_race(cfg, conn):
    agg = _job(conn, review="surfaced", score=90)
    decoy = _job(conn, source=BOARD, ext="g1", company="Other Co", review="new", jd="x")
    a, b = rc.pair_of(agg, decoy)
    net = Net(on_fetch=_race(
        cfg,
        "INSERT INTO same_role_decisions (job_a, job_b, decision, decided_at) VALUES (?,?,?,?)",
        (a, b, rc.DIFFERENT, "2026-10-16"),
    ), first_blank=True)  # fmt: skip

    res = await _run(cfg, conn, network=True, deps=net.deps())

    assert _status(res, agg) == "lost_race" and _row(conn, agg)["raw_jd"] == SNIPPET
    assert (_row(conn, agg)["employer_link_source"] or "") == ""
    assert _row(conn, agg)["jd_lookup_status"] in (None, "")


async def test_a_link_set_during_the_fetch_loses_the_race(cfg, conn):
    agg = _job(conn, review="surfaced", score=90)
    net = Net(on_fetch=_race(
        cfg, "UPDATE jobs SET employer_link_source='lever:zzz', employer_link_external_id='q' "
        "WHERE id=?", (agg,)
    ))  # fmt: skip

    res = await _run(cfg, conn, network=True, deps=net.deps())

    assert _status(res, agg) == "lost_race" and _row(conn, agg)["jd_source"] == ""

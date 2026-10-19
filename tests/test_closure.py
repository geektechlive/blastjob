"""Posting-closure lifecycle: only proof moves an application's state.

Every assertion reads the column back out of SQLite. A previous bug shipped
because tests asserted on a helper's return value while the stored row never
changed (see 7588671).
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import tests.geo_fixture as geo_fixture
from blastjob.core import closure, employer_domains
from blastjob.core.closure_links import Link, LinkDeps
from blastjob.db import client as dbc
from blastjob.discovery.base import RawPosting
from blastjob.discovery.resolve import BoardHit
from blastjob.models.config import BlastJobConfig

NOW = datetime(2026, 9, 13, 8, 0, 0)


@pytest.fixture
def cfg():
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    c.discovery.closure_auto = True
    geo_fixture.apply_region(c)
    (Path(c.paths.data_dir) / "work_history.md").write_text("exec", encoding="utf-8")
    return c


@pytest.fixture
def conn(cfg):
    conn = dbc.init_db(cfg)
    yield conn
    conn.close()


def _job(
    conn,
    *,
    source: str = "workday:acme",
    ext: str = "r1",
    company: str = "Acme",
    title: str = "VP of Ops",
    review: str = "kept",
    app: str = "",
    build: str = "none",
    source_state: str = "active",
    cluster_id: int | None = None,
    first_seen: str = "2026-09-01T00:00:00",
    last_seen: str = "2026-09-12T00:00:00",
) -> int:
    jid = dbc.upsert_job(conn, source=source, external_id=ext, company=company, title=title)
    conn.execute(
        "UPDATE jobs SET review_state=?, application_state=?, build_state=?, source_state=?, "
        "cluster_id=?, first_seen=?, last_seen=? WHERE id=?",
        (review, app, build, source_state, cluster_id, first_seen, last_seen, jid),
    )
    conn.commit()
    return jid


def _row(conn, jid: int):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


def _cluster(conn) -> int:
    cur = conn.execute(
        "INSERT INTO job_clusters (fingerprint, created_at) VALUES ('fp', '2026-09-01')"
    )
    conn.commit()
    return int(cur.lastrowid)


class Recorder:
    """Fake network layer. Every call is recorded; nothing leaves the process."""

    def __init__(
        self,
        *,
        boards: list[BoardHit] | None = None,
        postings: list[RawPosting] | None = None,
        check: bool | None | Exception = True,
        page_link: Link | None = None,
        page_check: bool | None = None,
    ) -> None:
        self.calls: list[tuple] = []
        self._boards = boards or []
        self._postings = postings or []
        self._check = check
        self._page_link = page_link
        self._page_check = page_check

    def probe_company(self, name: str) -> list[BoardHit]:
        self.calls.append(("probe_company", name))
        return list(self._boards)

    def probe_workday(self, name: str, key: str) -> list[BoardHit]:
        self.calls.append(("probe_workday", name))
        return []

    def probe_dayforce(self, name: str, key: str) -> list[BoardHit]:
        self.calls.append(("probe_dayforce", name))
        return []

    async def fetch_board(self, ats: str, token: str, title: str) -> list[RawPosting]:
        self.calls.append(("fetch_board", ats, token))
        return list(self._postings)

    async def check_link(self, link: Link) -> bool | None:
        self.calls.append(("check_link", link.source, link.external_id))
        if isinstance(self._check, Exception):
            raise self._check
        return self._check

    async def find_page(self, company: str, title: str, region: str, key: str) -> Link | None:
        self.calls.append(("find_page", company))
        return self._page_link

    async def check_page(self, link: Link, company: str, title: str, region: str) -> bool | None:
        self.calls.append(("check_page", link.url, company, title, region))
        return self._page_check

    def deps(self, jina: str = "") -> LinkDeps:
        return LinkDeps(
            jina_api_key=jina,
            probe_company=self.probe_company,
            probe_workday=self.probe_workday,
            probe_dayforce=self.probe_dayforce,
            fetch_board=self.fetch_board,
            check_link=self.check_link,
            find_page=self.find_page,
            check_page=self.check_page,
        )

    def searched(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "probe_company"]


async def _run(cfg, conn, rec: Recorder | None = None, **kw):
    return await closure.run_closure(cfg, conn, deps=(rec or Recorder()).deps(), now=NOW, **kw)


# --- decisions 1-4: what a closed posting does to each kind of job ----------


async def test_unapplied_kept_job_closes_and_remembers_prior_state(cfg, conn):
    jid = _job(conn, app="", source_state="stale")
    await _run(cfg, conn)
    row = _row(conn, jid)
    assert row["posting_check"] == "closed"
    assert row["application_state"] == "posting_closed"
    assert row["closed_prior_state"] == ""
    assert row["posting_closed_at"] == NOW.isoformat(timespec="seconds")


async def test_drafted_kept_job_saves_drafted_as_prior_state(cfg, conn):
    jid = _job(conn, app="drafted", source_state="stale")
    await _run(cfg, conn)
    row = _row(conn, jid)
    assert row["application_state"] == "posting_closed"
    assert row["closed_prior_state"] == "drafted"


async def test_tailored_packet_still_closes(cfg, conn):
    jid = _job(conn, app="", build="tailored", source_state="stale")
    await _run(cfg, conn)
    row = _row(conn, jid)
    assert row["application_state"] == "posting_closed"
    assert row["build_state"] == "tailored"


async def test_applied_job_is_badged_not_moved(cfg, conn):
    jid = _job(conn, app="applied", source_state="stale")
    await _run(cfg, conn)
    row = _row(conn, jid)
    assert row["application_state"] == "applied"
    assert row["posting_check"] == "closed"
    assert row["posting_closed_at"] != ""
    assert row["closed_prior_state"] == ""


@pytest.mark.parametrize("stage", ["screen", "interview", "offer"])
async def test_live_process_is_never_moved(cfg, conn, stage):
    jid = _job(conn, app=stage, source_state="stale")
    await _run(cfg, conn)
    later = NOW + timedelta(days=60)
    closure.sweep_filled(conn, now=later, auto=True)
    row = _row(conn, jid)
    assert row["application_state"] == stage
    assert row["posting_check"] == "closed"


@pytest.mark.parametrize("review", ["new", "surfaced", "queued", "archived"])
async def test_non_kept_job_only_gets_the_closed_stamp(cfg, conn, review):
    jid = _job(conn, review=review, app="", source_state="stale")
    await _run(cfg, conn)
    row = _row(conn, jid)
    assert row["posting_closed_at"] != ""
    assert row["application_state"] == ""
    assert row["review_state"] == review
    assert row["closed_prior_state"] == ""


async def test_own_authoritative_active_source_is_verified_open(cfg, conn):
    jid = _job(conn, source="greenhouse:acme", source_state="active")
    await _run(cfg, conn)
    row = _row(conn, jid)
    assert row["posting_check"] == "verified_open"
    assert row["posting_checked_at"] != ""
    assert row["application_state"] == ""


# --- 21-day sweep -------------------------------------------------------------


def _applied_closed(conn, days_ago: int) -> int:
    jid = _job(conn, app="applied")
    stamp = (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE jobs SET posting_check='closed', posting_closed_at=? WHERE id=?", (stamp, jid)
    )
    conn.commit()
    return jid


def test_sweep_leaves_applied_job_alone_at_day_20(conn):
    jid = _applied_closed(conn, 20)
    assert closure.sweep_filled(conn, now=NOW, auto=True) == 0
    assert _row(conn, jid)["application_state"] == "applied"


def test_sweep_moves_applied_job_to_filled_at_day_21(conn):
    jid = _applied_closed(conn, 21)
    assert closure.sweep_filled(conn, now=NOW, auto=True) == 1
    row = _row(conn, jid)
    assert row["application_state"] == "filled"
    assert row["closed_prior_state"] == "applied"


def test_sweep_does_nothing_in_shadow_mode(conn):
    jid = _applied_closed(conn, 30)
    assert closure.sweep_filled(conn, now=NOW, auto=False) == 0
    assert _row(conn, jid)["application_state"] == "applied"


# --- decision 7: reopen -------------------------------------------------------


async def test_reopen_restores_prior_state(cfg, conn):
    jid = _job(conn, app="drafted", source_state="stale")
    await _run(cfg, conn)
    conn.execute("UPDATE jobs SET source_state='active' WHERE id=?", (jid,))
    conn.commit()
    await _run(cfg, conn)
    row = _row(conn, jid)
    assert row["application_state"] == "drafted"
    assert row["posting_closed_at"] == ""
    assert row["closed_prior_state"] == ""
    assert row["posting_check"] == "verified_open"


async def test_reopen_of_a_filled_job_restores_applied(cfg, conn):
    jid = _applied_closed(conn, 25)
    conn.execute("UPDATE jobs SET source_state='stale' WHERE id=?", (jid,))
    conn.commit()
    closure.sweep_filled(conn, now=NOW, auto=True)
    conn.execute("UPDATE jobs SET source_state='active' WHERE id=?", (jid,))
    conn.commit()
    await _run(cfg, conn)
    assert _row(conn, jid)["application_state"] == "applied"


async def test_manual_state_change_blocks_restore(cfg, conn):
    jid = _job(conn, app="drafted", source_state="stale")
    await _run(cfg, conn)
    # The user moves it by hand, e.g. they applied through a recruiter anyway.
    conn.execute("UPDATE jobs SET application_state='applied' WHERE id=?", (jid,))
    conn.execute("UPDATE jobs SET source_state='active' WHERE id=?", (jid,))
    conn.commit()
    await _run(cfg, conn)
    assert _row(conn, jid)["application_state"] == "applied"


# --- decision 8: manual verdicts ---------------------------------------------


async def test_manual_still_open_survives_a_later_automated_closed_verdict(cfg, conn):
    jid = _job(conn, app="drafted", source_state="stale")
    await _run(cfg, conn)
    assert closure.set_manual_verdict(conn, jid, is_open=True, now=NOW) is True
    await _run(cfg, conn)
    row = _row(conn, jid)
    assert row["posting_check"] == "verified_open"
    assert row["posting_check_manual"] == 1
    assert row["application_state"] == "drafted"
    assert row["posting_closed_at"] == ""


def test_manual_posting_closed_moves_state_even_in_shadow_mode(conn):
    jid = _job(conn, app="")
    assert closure.set_manual_verdict(conn, jid, is_open=False, now=NOW) is True
    row = _row(conn, jid)
    assert row["posting_check"] == "closed"
    assert row["posting_check_manual"] == 1
    assert row["application_state"] == "posting_closed"
    assert row["posting_closed_at"] != ""


def test_manual_verdict_on_missing_job_returns_false(conn):
    assert closure.set_manual_verdict(conn, 9999, is_open=True, now=NOW) is False


# --- decision 9: shadow mode --------------------------------------------------


async def test_shadow_mode_writes_checks_and_links_but_no_transitions(cfg, conn):
    cfg.discovery.closure_auto = False
    cid = _cluster(conn)
    kept = _job(conn, app="", source_state="stale", ext="own")
    linked = _job(conn, source="linkedin", ext="li1", cluster_id=cid)
    board = _job(
        conn, source="workday:acme", ext="wd1", review="new", cluster_id=cid, source_state="stale"
    )
    # Two same-titled Acme board rows make the title match ambiguous, so the
    # link comes from the sibling, which must be provably the same metro.
    _geo(conn, linked, "Fairhaven", "Fairhaven, OH")
    _geo(conn, board, "Fairhaven", "Fairhaven, OH")
    await _run(cfg, conn)
    a, b = _row(conn, kept), _row(conn, linked)
    assert a["posting_check"] == "closed"
    assert a["application_state"] == ""
    assert b["employer_link_source"] == "workday:acme"
    assert b["posting_check"] == "closed"
    assert b["application_state"] == ""


async def test_forced_shadow_overrides_config(cfg, conn):
    jid = _job(conn, app="", source_state="stale")
    await _run(cfg, conn, shadow=True)
    assert _row(conn, jid)["application_state"] == ""


# --- decisions 5-6: jobs whose own source cannot prove closure ---------------


async def test_adzuna_ttl_stale_alone_is_only_likely_closed(cfg, conn):
    jid = _job(conn, source="adzuna", company="Acme", source_state="stale")
    await _run(cfg, conn)
    row = _row(conn, jid)
    assert row["posting_check"] == "likely_closed"
    assert row["application_state"] == ""
    assert row["posting_closed_at"] == ""


async def test_intermediary_is_unverifiable_and_never_probed(cfg, conn):
    jid = _job(conn, source="linkedin", company="Acme Staffing", title="VP of Ops")
    rec = Recorder(boards=[BoardHit("Acme Staffing", "greenhouse", "acmestaffing", 3)])
    await closure.run_closure(cfg, conn, deps=rec.deps(jina="key"), now=NOW)
    assert _row(conn, jid)["posting_check"] == "unverifiable"
    assert rec.calls == []


async def test_cluster_sibling_on_authoritative_board_is_linked_and_inherited(cfg, conn):
    cid = _cluster(conn)
    jid = _job(conn, source="linkedin", ext="li1", app="drafted", cluster_id=cid)
    board = _job(
        conn, source="workday:acme", ext="wd1", review="new", cluster_id=cid, source_state="stale"
    )
    # Proof needs a shared location (see the location tests below); without one,
    # this used to pass only through the title-alone fallback now closed off.
    _geo(conn, jid, "Fairhaven", "Fairhaven, OH")
    _geo(conn, board, "Fairhaven", "Fairhaven, OH")
    await _run(cfg, conn)
    row = _row(conn, jid)
    assert row["employer_link_source"] == "workday:acme"
    assert row["employer_link_external_id"] == "wd1"
    assert row["posting_check"] == "closed"
    assert row["application_state"] == "posting_closed"


async def test_sibling_that_was_gone_before_the_job_appeared_is_not_linked(cfg, conn):
    cid = _cluster(conn)
    jid = _job(conn, source="linkedin", ext="li1", cluster_id=cid, first_seen="2026-09-10T00:00:00")
    _job(
        conn,
        source="workday:acme",
        ext="old",
        review="archived",
        cluster_id=cid,
        source_state="stale",
        last_seen="2026-03-01T00:00:00",
    )
    await _run(cfg, conn)
    row = _row(conn, jid)
    assert row["employer_link_source"] == ""
    assert row["application_state"] == ""


async def test_confident_board_match_links_and_verifies_open(cfg, conn):
    jid = _job(conn, source="linkedin", ext="li1", company="Acme", title="VP of Ops")
    rec = Recorder(
        boards=[BoardHit("Acme", "greenhouse", "acme", 4)],
        postings=[
            RawPosting(
                source="greenhouse:acme", external_id="g1", title="VP of Ops", url="https://x/g1"
            ),
            RawPosting(source="greenhouse:acme", external_id="g2", title="Help Desk Tech"),
        ],
    )
    await _run(cfg, conn, rec)
    row = _row(conn, jid)
    assert row["employer_link_source"] == "greenhouse:acme"
    assert row["employer_link_external_id"] == "g1"
    assert row["employer_link_url"] == "https://x/g1"
    assert row["posting_check"] == "verified_open"


async def test_ambiguous_title_match_leaves_job_unlinked(cfg, conn):
    jid = _job(conn, source="linkedin", ext="li1", company="Acme", title="VP of Ops")
    rec = Recorder(
        boards=[BoardHit("Acme", "greenhouse", "acme", 4)],
        postings=[
            RawPosting(source="greenhouse:acme", external_id="g1", title="VP of Ops"),
            RawPosting(source="greenhouse:acme", external_id="g2", title="VP, of Ops"),
        ],
    )
    await _run(cfg, conn, rec)
    row = _row(conn, jid)
    assert row["employer_link_source"] == ""
    assert row["posting_check"] == ""


async def test_board_for_a_different_company_is_not_trusted(cfg, conn):
    jid = _job(conn, source="linkedin", ext="li1", company="Acme", title="VP of Ops")
    rec = Recorder(
        boards=[BoardHit("Acme", "greenhouse", "acmehealth", 4)],
        postings=[RawPosting(source="greenhouse:acmehealth", external_id="g1", title="VP of Ops")],
    )
    await _run(cfg, conn, rec)
    assert _row(conn, jid)["employer_link_source"] == ""


def _network_linked(conn, *, app: str = "", check: str = "verified_open") -> int:
    jid = _job(conn, source="linkedin", ext="li1", app=app)
    conn.execute(
        "UPDATE jobs SET employer_link_source='greenhouse:acme', employer_link_external_id='g1', "
        "posting_check=? WHERE id=?",
        (check, jid),
    )
    conn.commit()
    return jid


async def test_linked_posting_gone_from_board_closes_the_job(cfg, conn):
    jid = _network_linked(conn, app="drafted")
    await _run(cfg, conn, Recorder(check=False))
    row = _row(conn, jid)
    assert row["posting_check"] == "closed"
    assert row["application_state"] == "posting_closed"


async def test_a_probe_that_raises_keeps_the_prior_verdict(cfg, conn):
    jid = _network_linked(conn, app="drafted")
    await _run(cfg, conn, Recorder(check=RuntimeError("board down")))
    row = _row(conn, jid)
    assert row["posting_check"] == "verified_open"
    assert row["application_state"] == "drafted"
    assert row["posting_closed_at"] == ""


async def test_an_inconclusive_probe_keeps_the_prior_verdict(cfg, conn):
    jid = _network_linked(conn, app="drafted", check="closed")
    await _run(cfg, conn, Recorder(check=None))
    assert _row(conn, jid)["posting_check"] == "closed"


async def test_closed_kept_postings_are_reported_once(cfg, conn):
    _job(conn, app="", source_state="stale", company="Fabrikam Equipment", title="Chief Officer")
    first = await _run(cfg, conn)
    second = await _run(cfg, conn)
    expected = [("Fabrikam Equipment", "Chief Officer")]
    assert [(j["company"], j["title"]) for j in first.closed_kept] == expected
    assert second.closed_kept == []


# --- a cluster sibling is proof only when it is really the same role ---------
# Code review (feat/same-role-guard): a loose same-role merge put a Fairhaven
# board posting in one cluster with a "United States" LinkedIn posting, and the
# board posting going stale closed the LinkedIn job. Titles below differ
# enough for the closure title matcher ("VP Ops" vs "Vice President, Business
# Operations") so only the cluster path could link them.


def _geo(conn, jid: int, city: str, raw: str, remote: int = 0) -> None:
    conn.execute(
        "UPDATE jobs SET geo_city=?, geo_location_raw=?, geo_remote=? WHERE id=?",
        (city, raw, remote, jid),
    )
    conn.commit()


def _sibling_pair(conn, *, li_city: str, li_raw: str, li_remote: int = 0) -> tuple[int, int]:
    cid = _cluster(conn)
    jid = _job(conn, source="linkedin", ext="li1", title="VP Ops", app="drafted", cluster_id=cid)
    board = _job(
        conn,
        source="workday:acme",
        ext="wd1",
        title="Vice President, Business Operations",
        review="new",
        cluster_id=cid,
        source_state="stale",
    )
    _geo(conn, board, "Fairhaven", "Fairhaven, OH")
    _geo(conn, jid, li_city, li_raw, li_remote)
    return jid, board


async def test_unplaceable_cluster_sibling_is_not_proof_of_closure(cfg, conn):
    jid, _board = _sibling_pair(conn, li_city="", li_raw="United States")

    await _run(cfg, conn)

    row = _row(conn, jid)
    assert row["employer_link_source"] == ""
    assert row["posting_check"] != "closed"
    assert row["application_state"] == "drafted"


async def test_same_metro_cluster_sibling_is_still_proof(cfg, conn):
    jid, _board = _sibling_pair(conn, li_city="Oakbrook", li_raw="Oakbrook, OH")

    await _run(cfg, conn)

    row = _row(conn, jid)
    assert row["employer_link_external_id"] == "wd1"
    assert row["application_state"] == "posting_closed"


async def test_confirmed_same_cluster_sibling_is_proof(cfg, conn):
    from blastjob.core import role_clusters as rc

    jid, board = _sibling_pair(conn, li_city="", li_raw="United States")
    rc.record_decision(conn, jid, board, rc.SAME)
    conn.commit()

    await _run(cfg, conn)

    row = _row(conn, jid)
    assert row["employer_link_external_id"] == "wd1"
    assert row["posting_check"] == "closed"


# --- the company + title match path checks location too (2026-09-13) --
# With no cluster at all, db_link fell back to "same company, same title" over
# every board posting in the DB and never looked at location. The DB holds only
# the board postings discovery's queries happened to fetch, so a "United States"
# LinkedIn alert for req C could link to req A in Fairhaven, and close when A did.
# Unplaceable jobs now fall through to the network path, which reads the live
# board and refuses an ambiguous title.


def _title_pair(conn, *, li_city: str, li_raw: str, li_remote: int = 0) -> tuple[int, int]:
    jid = _job(conn, source="linkedin", ext="li9", title="VP of Ops", app="drafted")
    board = _job(
        conn,
        source="workday:acme",
        ext="wd9",
        title="VP of Ops",
        review="new",
        source_state="stale",
    )
    _geo(conn, board, "Fairhaven", "Fairhaven, OH")
    _geo(conn, jid, li_city, li_raw, li_remote)
    return jid, board


async def test_title_match_to_a_board_posting_elsewhere_is_not_proof(cfg, conn):
    jid, _board = _title_pair(conn, li_city="", li_raw="United States")

    await _run(cfg, conn)

    row = _row(conn, jid)
    assert row["employer_link_source"] == ""
    assert row["posting_check"] != "closed"
    assert row["application_state"] == "drafted"


async def test_title_match_in_the_same_metro_still_links_and_closes(cfg, conn):
    jid, _board = _title_pair(conn, li_city="Oakbrook", li_raw="Oakbrook, OH")

    await _run(cfg, conn)

    row = _row(conn, jid)
    assert row["employer_link_external_id"] == "wd9"
    assert row["application_state"] == "posting_closed"


async def test_title_match_confirmed_same_role_links_whatever_the_location(cfg, conn):
    from blastjob.core import role_clusters as rc

    jid, board = _title_pair(conn, li_city="", li_raw="United States")
    rc.record_decision(conn, jid, board, rc.SAME)
    conn.commit()

    await _run(cfg, conn)

    assert _row(conn, jid)["employer_link_external_id"] == "wd9"


# --- employer careers pages, and Surfaced/Queue get checked too --------------------


FABRIKAM_URL = "https://careers.fabrikam-services.example/apply/jobs/details/AbC123"


def _li(conn, ext: str, *, review: str, company: str = "Fabrikam Services", **cols) -> int:
    jid = _job(
        conn, source="linkedin", ext=ext, company=company, title="VP of Technology", review=review
    )
    cols = {"geo_region": "Ohio", **cols}
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), jid))
    conn.commit()
    return jid


def _list_fabrikam(cfg) -> None:
    """Confirm the careers host as Fabrikam's own: only then is its page evidence
    (an unlisted page bound by its apply form proves nothing either way, #23)."""
    cfg.discovery.employer_domains = {"Fabrikam Services": ["fabrikam-services.example"]}
    employer_domains.set_employer_domains(cfg.discovery.employer_domains)


def _page_linked(conn, review: str, app: str = "") -> int:
    return _li(
        conn,
        "li-page",
        review=review,
        application_state=app,
        employer_link_source="page:careers.fabrikam-services.example",
        employer_link_external_id=FABRIKAM_URL,
        employer_link_url=FABRIKAM_URL,
        posting_check="verified_open",
    )


def test_caps_were_raised_for_the_wider_sweep():
    assert closure.MAX_LINK_SEARCHES_PER_RUN == 15
    assert closure.MAX_LINK_PROBES_PER_RUN == 40


async def test_surfaced_job_whose_employer_page_is_gone_is_closed_but_not_moved(cfg, conn):
    _list_fabrikam(cfg)
    jid = _page_linked(conn, "surfaced")
    rec = Recorder(page_check=False)

    await _run(cfg, conn, rec)

    row = _row(conn, jid)
    assert row["posting_check"] == "closed"
    assert row["posting_closed_at"] != ""
    assert row["review_state"] == "surfaced"
    assert row["application_state"] == ""
    assert ("check_page", FABRIKAM_URL, "Fabrikam Services", "VP of Technology", "Ohio") in (
        rec.calls
    )
    assert not [c for c in rec.calls if c[0] == "check_link"]


async def test_kept_job_whose_employer_page_is_gone_moves_to_posting_closed(cfg, conn):
    _list_fabrikam(cfg)
    jid = _page_linked(conn, "kept")

    await _run(cfg, conn, Recorder(page_check=False))

    assert _row(conn, jid)["application_state"] == "posting_closed"


async def test_an_inconclusive_page_check_keeps_the_verdict(cfg, conn):
    _list_fabrikam(cfg)
    jid = _page_linked(conn, "queued")

    await _run(cfg, conn, Recorder(page_check=None))

    assert _row(conn, jid)["posting_check"] == "verified_open"
    assert _row(conn, jid)["posting_closed_at"] == ""


async def test_a_queued_job_gets_searched(cfg, conn):
    _li(conn, "q1", review="queued")
    rec = Recorder()

    await _run(cfg, conn, rec)

    assert rec.searched() == ["Fabrikam Services"]


async def test_a_page_link_found_for_a_surfaced_job_is_saved_and_verified(cfg, conn):
    _list_fabrikam(cfg)
    jid = _li(conn, "s1", review="surfaced")
    link = Link("page:careers.fabrikam-services.example", FABRIKAM_URL, FABRIKAM_URL)

    await closure.run_closure(cfg, conn, deps=Recorder(page_link=link).deps(jina="k"), now=NOW)

    row = _row(conn, jid)
    assert row["employer_link_source"] == "page:careers.fabrikam-services.example"
    assert row["employer_link_url"] == FABRIKAM_URL
    assert row["posting_check"] == "verified_open"


@pytest.mark.parametrize(("review", "state"), [("archived", "active"), ("surfaced", "stale")])
async def test_archived_and_inactive_rows_are_not_searched(cfg, conn, review, state):
    _li(conn, "x1", review=review, source_state=state)
    rec = Recorder()

    await _run(cfg, conn, rec)

    assert rec.searched() == []


async def test_kept_jobs_are_searched_before_surfaced_ones_when_the_cap_binds(
    cfg, conn, monkeypatch
):
    monkeypatch.setattr(closure, "MAX_LINK_SEARCHES_PER_RUN", 1)
    _li(conn, "s1", review="surfaced", company="Surfaced Co", alignment_score=95)
    _li(conn, "q1", review="queued", company="Queued Co", alignment_score=90)
    _li(conn, "k1", review="kept", company="Kept Co", alignment_score=80)
    rec = Recorder()

    await _run(cfg, conn, rec)

    assert rec.searched() == ["Kept Co"]


async def test_never_checked_rows_go_first_whatever_their_stage(cfg, conn, monkeypatch):
    monkeypatch.setattr(closure, "MAX_LINK_SEARCHES_PER_RUN", 1)
    _li(conn, "k1", review="kept", company="Kept Co", posting_checked_at="2026-09-12T08:00:00")
    _li(conn, "s1", review="surfaced", company="Surfaced Co")
    rec = Recorder()

    await _run(cfg, conn, rec)

    assert rec.searched() == ["Surfaced Co"]


async def test_best_fit_breaks_a_tie_within_a_stage(cfg, conn, monkeypatch):
    monkeypatch.setattr(closure, "MAX_LINK_SEARCHES_PER_RUN", 1)
    _li(conn, "s1", review="surfaced", company="Low Fit", alignment_score=70)
    _li(conn, "s2", review="surfaced", company="High Fit", alignment_score=92)
    rec = Recorder()

    await _run(cfg, conn, rec)

    assert rec.searched() == ["High Fit"]


async def test_empty_company_is_skipped_without_spending_the_cap(cfg, conn, monkeypatch):
    monkeypatch.setattr(closure, "MAX_LINK_SEARCHES_PER_RUN", 1)
    blank = _li(conn, "s0", review="surfaced", company="", alignment_score=99)
    _li(conn, "s1", review="surfaced", company="Acme", alignment_score=80)
    rec = Recorder()

    await _run(cfg, conn, rec)

    assert rec.searched() == ["Acme"]
    assert _row(conn, blank)["posting_check"] == ""
    assert _row(conn, blank)["posting_checked_at"] == ""


async def test_linked_surfaced_rows_are_probed_kept_first_within_the_cap(cfg, conn, monkeypatch):
    monkeypatch.setattr(closure, "MAX_LINK_PROBES_PER_RUN", 1)
    surfaced = _li(
        conn,
        "s1",
        review="surfaced",
        employer_link_source="greenhouse:a",
        employer_link_external_id="1",
        posting_check="verified_open",
    )
    kept = _li(
        conn,
        "k1",
        review="kept",
        employer_link_source="greenhouse:b",
        employer_link_external_id="2",
        posting_check="verified_open",
    )
    rec = Recorder(check=False)

    await _run(cfg, conn, rec)

    assert [c[1] for c in rec.calls if c[0] == "check_link"] == ["greenhouse:b"]
    assert _row(conn, kept)["posting_check"] == "closed"
    assert _row(conn, surfaced)["posting_check"] == "verified_open"


async def test_the_default_page_check_closes_on_a_410_from_the_employer(cfg, conn, monkeypatch):
    """The real wiring: LinkDeps' default check_page -> page_links.check_page ->
    page_links._safe_get (faked here, answering 410 from the linked host)."""
    from blastjob.core import page_fetch, page_links

    _list_fabrikam(cfg)
    jid = _page_linked(conn, "surfaced")
    fetched: list[str] = []

    async def gone(url: str) -> page_fetch.Page:
        fetched.append(url)
        return page_fetch.Page(410, url, "", False)

    monkeypatch.setattr(page_links, "_safe_get", gone)
    rec = Recorder()
    deps = LinkDeps(
        probe_company=rec.probe_company,
        probe_workday=rec.probe_workday,
        probe_dayforce=rec.probe_dayforce,
        fetch_board=rec.fetch_board,
        check_link=rec.check_link,
    )

    await closure.run_closure(cfg, conn, deps=deps, now=NOW)

    row = _row(conn, jid)
    assert fetched == [FABRIKAM_URL]
    assert row["posting_check"] == "closed"
    assert row["posting_closed_at"] != ""
    assert row["review_state"] == "surfaced"
    assert row["application_state"] == ""

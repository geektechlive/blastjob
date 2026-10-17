"""Review fixes for the posting-closure pass: no write lock held across network
I/O, no leaked partial writes, manual un-close wins, flip-day visibility,
secrets on dry runs, stale-row guard, and the real per-run board cache.

Every assertion reads stored state back out of SQLite.
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from blastjob import daily
from blastjob.core import closure
from blastjob.core import closure_links as cl
from blastjob.core.closure_links import default_deps as real_default_deps
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.models.tracking import STATUSES
from blastjob.web import service

NOW = datetime(2026, 9, 13, 8, 0, 0)


@pytest.fixture
def cfg():
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    c.discovery.closure_auto = True
    (Path(c.paths.data_dir) / "work_history.md").write_text("exec", encoding="utf-8")
    return c


@pytest.fixture
def conn(cfg):
    conn = dbc.init_db(cfg)
    yield conn
    conn.close()


def _job(conn, *, source="workday:acme", ext="r1", company="Acme", title="VP of Ops",
         review="kept", app="", source_state="active") -> int:  # fmt: skip
    jid = dbc.upsert_job(conn, source=source, external_id=ext, company=company, title=title)
    conn.execute(
        "UPDATE jobs SET review_state=?, application_state=?, source_state=?, "
        "first_seen='2026-09-01T00:00:00' WHERE id=?",
        (review, app, source_state, jid),
    )
    conn.commit()
    return jid


def _link(conn, jid: int, source="greenhouse:acme", ext="g1") -> None:
    conn.execute(
        "UPDATE jobs SET employer_link_source=?, employer_link_external_id=? WHERE id=?",
        (source, ext, jid),
    )
    conn.commit()


def _row(conn, jid):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


def _deps(check):
    async def none_list(*_a):
        return []

    return cl.LinkDeps(
        probe_company=lambda *_a: [],
        probe_workday=lambda *_a: [],
        probe_dayforce=lambda *_a: [],
        fetch_board=none_list,
        check_link=check,
    )


# --- 1: no write transaction held across a network await ---------------------


async def test_cockpit_can_write_while_a_probe_is_in_flight(cfg, conn):
    _job(conn, ext="own", source_state="stale")  # first loop writes a verdict
    linked = _job(conn, source="linkedin", ext="li1")
    _link(conn, linked)
    other = _job(conn, source="linkedin", ext="li2", company="Beta")
    seen: dict = {}

    async def check(link):
        # A cockpit click, on its own connection, with a short busy timeout.
        c2 = sqlite3.connect(dbc.db_path(cfg), timeout=0.2)
        try:
            c2.execute("UPDATE jobs SET app_notes='clicked' WHERE id=?", (other,))
            c2.commit()
            seen["ok"] = True
        except sqlite3.OperationalError as e:
            seen["error"] = str(e)
        finally:
            c2.close()
        return False

    await closure.run_closure(cfg, conn, deps=_deps(check), now=NOW)
    assert seen == {"ok": True}
    assert _row(conn, other)["app_notes"] == "clicked"
    assert _row(conn, linked)["posting_check"] == "closed"


async def test_unchanged_own_source_verdict_is_not_rewritten(cfg, conn):
    jid = _job(conn, source="greenhouse:acme", source_state="active")
    await closure.run_closure(cfg, conn, deps=_deps(None), now=NOW)
    first = _row(conn, jid)["posting_checked_at"]
    await closure.run_closure(cfg, conn, deps=_deps(None), now=NOW + timedelta(days=1))
    assert first == NOW.isoformat(timespec="seconds")
    assert _row(conn, jid)["posting_checked_at"] == first


async def test_a_real_network_check_refreshes_the_checked_date(cfg, conn):
    jid = _job(conn, source="linkedin", ext="li1")
    _link(conn, jid)

    async def still_open(link):
        return True

    await closure.run_closure(cfg, conn, deps=_deps(still_open), now=NOW)
    later = NOW + timedelta(days=1)
    await closure.run_closure(cfg, conn, deps=_deps(still_open), now=later)
    assert _row(conn, jid)["posting_checked_at"] == later.isoformat(timespec="seconds")


# --- 2: a failed pass leaks nothing into a later commit -----------------------


def _stub_pipeline(monkeypatch):
    from blastjob.discovery import pipeline
    from blastjob.discovery.pipeline import DiscoveryResult
    from blastjob.matching import pipeline as mpipe
    from blastjob.matching.pipeline import MatchResult

    async def fake_discovery(cfg, conn, adapters=None):
        return DiscoveryResult()

    async def fake_matching(cfg, conn, tracker, on_progress=None, **kw):
        return MatchResult()

    monkeypatch.setattr(pipeline, "run_discovery", fake_discovery)
    monkeypatch.setattr(mpipe, "run_matching", fake_matching)


async def test_failed_pass_rolls_back_and_the_digest_still_sends(cfg, conn, monkeypatch):
    _stub_pipeline(monkeypatch)
    victim = _job(conn, source="linkedin", ext="li1", app="drafted")
    _link(conn, victim)
    real_apply = closure._apply

    def apply_then_crash(ctx, row, verdict, **kw):
        real_apply(ctx, row, verdict, **kw)
        if row["id"] == victim:
            raise RuntimeError("boom mid-pass")

    async def closed(link):
        return False

    monkeypatch.setattr(closure, "_apply", apply_then_crash)
    monkeypatch.setattr(cl, "default_deps", lambda: _deps(closed))
    sent: list[str] = []

    async def send(text):
        sent.append(text)

    res = await daily.run_daily(cfg, conn, send=send)

    assert res.sent is True and sent
    assert "closure" in res.errors
    row = _row(conn, victim)
    assert row["posting_check"] == ""
    assert row["application_state"] == "drafted"
    assert row["posting_closed_at"] == ""


# --- 3: a manual un-close is never re-closed ----------------------------------


@pytest.mark.parametrize("target", ["drafted", ""])
async def test_manual_move_out_of_posting_closed_sticks(cfg, conn, target):
    jid = _job(conn, app="drafted", source_state="stale")
    await closure.run_closure(cfg, conn, deps=_deps(None), now=NOW)
    assert _row(conn, jid)["application_state"] == "posting_closed"

    assert service.set_application_state(conn, jid, target, ["", *STATUSES])
    await closure.run_closure(cfg, conn, deps=_deps(None), now=NOW + timedelta(days=1))

    row = _row(conn, jid)
    assert row["application_state"] == target
    assert row["posting_check_manual"] == 1


def test_manual_move_out_of_filled_is_not_refiled(conn):
    jid = _job(conn, app="filled", source_state="stale")
    conn.execute(
        "UPDATE jobs SET posting_check='closed', closed_prior_state='applied', "
        "posting_closed_at=? WHERE id=?",
        ((NOW - timedelta(days=40)).isoformat(timespec="seconds"), jid),
    )
    conn.commit()
    service.set_application_state(conn, jid, "applied", ["", *STATUSES])
    closure.sweep_filled(conn, now=NOW, auto=True)
    assert _row(conn, jid)["application_state"] == "applied"


def test_ordinary_status_change_does_not_pin_the_verdict(conn):
    jid = _job(conn, app="applied")
    service.set_application_state(conn, jid, "screen", ["", *STATUSES])
    assert _row(conn, jid)["posting_check_manual"] == 0


# --- 4: flip-day visibility ---------------------------------------------------


def test_would_move_to_predicts_transitions_without_writing(conn):
    closed_at_old = (NOW - timedelta(days=30)).isoformat(timespec="seconds")
    closed_at_new = (NOW - timedelta(days=2)).isoformat(timespec="seconds")
    a = _job(conn, ext="a", app="drafted")
    b = _job(conn, ext="b", app="applied")
    c = _job(conn, ext="c", app="applied")
    d = _job(conn, ext="d", app="interview")
    e = _job(conn, ext="e", app="drafted")
    conn.execute("UPDATE jobs SET posting_check='closed', posting_closed_at=?", (closed_at_new,))
    conn.execute("UPDATE jobs SET posting_closed_at=? WHERE id=?", (closed_at_old, b))
    conn.execute("UPDATE jobs SET posting_check='likely_closed' WHERE id=?", (e,))
    conn.commit()
    before = [dict(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id")]

    rows = {r["id"]: closure.would_move_to(r, now=NOW) for r in conn.execute("SELECT * FROM jobs")}

    assert rows == {a: "posting_closed", b: "filled", c: "—", d: "—", e: "—"}
    assert [dict(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id")] == before


def test_closure_report_has_a_would_move_to_column():
    rows = [
        {
            "id": 7,
            "company": "Fabrikam Equipment",
            "title": "Chief Officer",
            "source": "workday:fabrikam",
            "employer_link_source": "",
            "employer_link_external_id": "",
            "posting_check": "closed",
            "posting_check_manual": 0,
            "application_state": "",
            "would_move_to": "posting_closed",
        }
    ]
    text = daily.format_closure_report(rows)
    assert "would_move_to" in text.splitlines()[0]
    assert text.splitlines()[1].endswith("| posting_closed")


def test_closure_report_names_a_description_it_would_revert():
    """Shadow closure no longer reverts an adopted employer description; the report
    says which kept rows WOULD lose it, so the owner sees it before a real run."""
    row = {
        "id": 8,
        "company": "Fabrikam Equipment",
        "title": "Chief Officer",
        "source": "adzuna",
        "employer_link_source": "greenhouse:fabrikam",
        "employer_link_external_id": "12",
        "posting_check": "unverifiable",
        "posting_check_manual": 0,
        "application_state": "",
        "would_move_to": "",
        "would_revert_jd": True,
    }
    line = daily.format_closure_report([row]).splitlines()[1]
    assert "employer description would revert" in line


# --- 5: secrets are loaded for the closure pass, dry run included ------------


async def test_dry_run_loads_secrets_before_the_closure_pass(cfg, conn, monkeypatch):
    import blastjob.secrets as secrets_mod

    _stub_pipeline(monkeypatch)
    order: list[str] = []
    monkeypatch.setattr(secrets_mod, "load_secrets_env", lambda *a, **k: order.append("secrets"))

    async def fake_closure(cfg_, conn_, **kw):
        order.append("closure")
        return closure.ClosureResult()

    monkeypatch.setattr(closure, "run_closure", fake_closure)

    async def send(text):
        raise AssertionError("dry run must not send")

    await daily.run_daily(cfg, conn, send=send, dry_run=True)
    assert "closure" in order
    assert "secrets" in order[: order.index("closure")]


# --- 6: a manual verdict landing mid-pass wins --------------------------------


async def test_manual_verdict_between_select_and_update_is_not_overridden(cfg, conn):
    jid = _job(conn, app="drafted", source_state="stale")
    ctx = closure._Pass(
        conn=conn,
        deps=_deps(None),
        now=NOW,
        auto=True,
        result=closure.ClosureResult(),
        index={},
    )
    stale_row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    closure.set_manual_verdict(conn, jid, is_open=True, now=NOW)

    closure._apply(ctx, stale_row, "closed")
    conn.commit()

    row = _row(conn, jid)
    assert row["posting_check"] == "verified_open"
    assert row["application_state"] == "drafted"
    assert row["posting_closed_at"] == ""
    assert ctx.result.closed_kept == []


# --- 7: the real default_deps caches one board fetch per run ------------------


async def test_default_deps_fetches_a_shared_greenhouse_board_once(cfg, conn, monkeypatch):
    urls: list[str] = []

    async def send(self, request, **kw):
        urls.append(str(request.url))
        return httpx.Response(
            200,
            json={"jobs": [{"id": 1, "title": "VP of Ops", "absolute_url": "https://x/1"}]},
            request=request,
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    a = _job(conn, source="linkedin", ext="li1")
    b = _job(conn, source="adzuna", ext="az1", title="Chief Officer")
    _link(conn, a, ext="1")
    _link(conn, b, ext="2")

    await closure.run_closure(cfg, conn, deps=real_default_deps(), now=NOW)

    board_calls = [u for u in urls if "boards-api.greenhouse.io/v1/boards/acme/jobs" in u]
    assert len(board_calls) == 1
    assert _row(conn, a)["posting_check"] == "verified_open"
    assert _row(conn, b)["posting_check"] == "closed"

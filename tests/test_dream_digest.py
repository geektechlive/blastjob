"""The weekly Dream list section of the digest and its delivery state (#90)."""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from blastjob import daily, daily_dream
from blastjob.core import target_store as ts
from blastjob.core.company import company_key
from blastjob.db import client as dbc
from blastjob.discovery.pipeline import DiscoveryResult
from blastjob.llm.cost import CallCost
from blastjob.models.config import BlastJobConfig, TargetBoard

WEDNESDAY = dt.date(2026, 10, 28)
THURSDAY = dt.date(2026, 10, 29)
THRESHOLD = 78


@pytest.fixture
def cfg(tmp_path) -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = str(tmp_path)
    c.discovery.search_firm_weekday = 2  # Wednesday
    c.discovery.alignment_threshold = THRESHOLD
    c.discovery.target_boards = [
        TargetBoard(
            company="Northwind Traders",
            ats="greenhouse",
            token="northwind",
            identity_basis="ats_name",
        )
    ]
    return c


@pytest.fixture
def conn(cfg) -> sqlite3.Connection:
    c = dbc.init_db(cfg)
    ts.add_targets(c, [ts.TargetInput(name="Northwind Traders")], "dream")
    yield c
    c.close()


def _job(
    conn,
    n: int,
    *,
    company: str = "Northwind Traders",
    score: int = 90,
    state: str = "surfaced",
    far: int = 1,
    source: str = "greenhouse:northwind",
    **extra,
) -> int:
    jid = dbc.upsert_job(
        conn,
        source=source,
        external_id=str(n),
        company=company,
        title=f"VP of Technology {n}",
        url=f"https://example.com/{n}",
        geo_location_raw="Phoenix, AZ",
    )
    sets = {"review_state": state, "alignment_score": score, "dream_far": far, **extra}
    cols = ", ".join(f"{k}=?" for k in sets)
    conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*sets.values(), jid))
    conn.commit()
    return jid


def _ids(rows) -> list[int]:
    return [int(r["id"]) for r in rows]


# ----------------------------------------------------------------------------- selection


def test_the_daily_digest_never_carries_a_dream_far_role(conn):
    far = _job(conn, 1, far=1)
    near = _job(conn, 2, far=0)
    assert _ids(daily.pending_jobs(conn)) == [near]
    assert far not in _ids(daily.pending_jobs(conn))


def test_pending_dream_jobs_best_fit_first(conn):
    a = _job(conn, 1, score=82)
    b = _job(conn, 2, score=95)
    c = _job(conn, 3, score=88, state="queued")
    assert _ids(daily_dream.pending_dream_jobs(conn, THRESHOLD)) == [b, c, a]


def test_pending_dream_jobs_leaves_out_everything_not_eligible(conn):
    ok = _job(conn, 1)
    _job(conn, 2, score=70)  # under the threshold
    _job(conn, 3, source_state="stale")
    _job(conn, 4, state="archived")
    _job(conn, 5, state="kept")
    _job(conn, 6, state="new")  # comp gate or prefilter turned it away
    _job(conn, 7, dream_notified_at="2026-10-21T08:00:00")
    _job(conn, 8, far=0)
    _job(conn, 9, company="Fabrikam")  # not a dream company
    assert _ids(daily_dream.pending_dream_jobs(conn, THRESHOLD)) == [ok]


def test_a_role_whose_cluster_holds_a_decided_member_is_not_announced(conn):
    conn.execute("INSERT INTO job_clusters (fingerprint, created_at) VALUES ('k', '')")
    cid = conn.execute("SELECT max(id) FROM job_clusters").fetchone()[0]
    decided = _job(conn, 1, state="archived", cluster_id=cid)
    twin = _job(conn, 2, cluster_id=cid)
    assert twin not in _ids(daily_dream.pending_dream_jobs(conn, THRESHOLD))
    assert decided not in _ids(daily_dream.pending_dream_jobs(conn, THRESHOLD))


def test_a_company_that_left_the_dream_tier_drops_out(conn):
    _job(conn, 1)
    assert ts.set_tier(conn, company_key("Northwind Traders"), "core")
    assert daily_dream.pending_dream_jobs(conn, THRESHOLD) == []


def test_a_dream_far_row_from_a_source_without_the_exception_is_not_announced(cfg, conn):
    ok = _job(conn, 1)
    _job(conn, 2, source="adzuna")
    assert _ids(daily_dream.weekly_jobs(cfg, conn, WEDNESDAY)) == [ok]
    cfg.discovery.target_boards[0].identity_basis = ""
    assert daily_dream.weekly_jobs(cfg, conn, WEDNESDAY) == []


def test_weekly_jobs_only_on_the_search_firm_weekday(cfg, conn):
    jid = _job(conn, 1)
    assert _ids(daily_dream.weekly_jobs(cfg, conn, WEDNESDAY)) == [jid]
    assert daily_dream.weekly_jobs(cfg, conn, THURSDAY) == []


# ----------------------------------------------------------------------------- the run


def _stub_pipeline(monkeypatch):
    async def fake_discovery(cfg, conn, adapters=None):
        return DiscoveryResult(fetched=0, per_source={}, errors={})

    class M:
        cost = CallCost()
        scored = 0
        surfaced = 0
        queued = 0
        scoring_failures = 0

    async def fake_matching(cfg, conn, tracker, on_progress=None, **kw):
        return M()

    import blastjob.discovery.pipeline as dp
    import blastjob.matching.pipeline as mp

    monkeypatch.setattr(dp, "run_discovery", fake_discovery)
    monkeypatch.setattr(mp, "run_matching", fake_matching)


def _stamps(conn, column: str) -> set[int]:
    rows = conn.execute(f"SELECT id FROM jobs WHERE {column}<>''").fetchall()
    return {int(r["id"]) for r in rows}


async def test_the_weekly_section_lists_far_roles_best_first_and_stamps_them(
    cfg, conn, monkeypatch
):
    _stub_pipeline(monkeypatch)
    low = _job(conn, 1, score=82)
    high = _job(conn, 2, score=95)
    sent: list[str] = []

    async def ok(text):
        sent.append(text)

    res = await daily.run_daily(cfg, conn, send=ok, today=WEDNESDAY)
    assert res.sent
    text = sent[0]
    assert "Dream list" in text
    assert text.index("VP of Technology 2") < text.index("VP of Technology 1")
    assert "on-site" in text
    assert _stamps(conn, "dream_notified_at") == {low, high}
    assert _stamps(conn, "notified_at") == set()  # the daily stamp is separate


async def test_no_dream_section_on_other_weekdays(cfg, conn, monkeypatch):
    _stub_pipeline(monkeypatch)
    _job(conn, 1)
    near = _job(conn, 2, far=0)
    sent: list[str] = []

    async def ok(text):
        sent.append(text)

    await daily.run_daily(cfg, conn, send=ok, today=THURSDAY)
    assert "Dream list" not in sent[0]
    assert "VP of Technology 1" not in sent[0]
    assert "VP of Technology 2" in sent[0]  # a near dream role goes out daily, as usual
    assert _stamps(conn, "dream_notified_at") == set()
    assert _stamps(conn, "notified_at") == {near}


async def test_a_week_with_only_dream_roles_still_sends(cfg, conn, monkeypatch):
    _stub_pipeline(monkeypatch)
    _job(conn, 1)
    sent: list[str] = []

    async def ok(text):
        sent.append(text)

    res = await daily.run_daily(cfg, conn, send=ok, today=WEDNESDAY)
    assert res.sent
    assert len(sent) == 1


async def test_a_failed_send_stamps_nothing(cfg, conn, monkeypatch):
    _stub_pipeline(monkeypatch)
    _job(conn, 1)

    async def boom(text):
        raise RuntimeError("discord down")

    with pytest.raises(RuntimeError):
        await daily.run_daily(cfg, conn, send=boom, today=WEDNESDAY)
    assert _stamps(conn, "dream_notified_at") == set()
    assert _stamps(conn, "notified_at") == set()


async def test_a_send_that_fails_after_accepting_text_stamps_nothing_and_resends(
    cfg, conn, monkeypatch
):
    """Discord may accept the first chunks of a long message and fail a later one. The sender
    then raises, nothing is stamped, and next week the whole section is sent again."""
    _stub_pipeline(monkeypatch)
    jid = _job(conn, 1)
    accepted: list[str] = []

    async def partial(text):
        accepted.append(text)  # the first chunk went out...
        raise RuntimeError("chunk 2 failed")  # ...a later one did not

    with pytest.raises(RuntimeError):
        await daily.run_daily(cfg, conn, send=partial, today=WEDNESDAY)
    assert "Dream list" in accepted[0]
    assert _stamps(conn, "dream_notified_at") == set()

    resent: list[str] = []

    async def ok(text):
        resent.append(text)

    await daily.run_daily(cfg, conn, send=ok, today=WEDNESDAY + dt.timedelta(days=7))
    assert "VP of Technology 1" in resent[0]
    assert _stamps(conn, "dream_notified_at") == {jid}


async def test_only_the_rendered_dream_roles_are_stamped(cfg, conn, monkeypatch):
    _stub_pipeline(monkeypatch)
    total = daily_dream.MAX_DREAM_JOBS + 3
    ids = [_job(conn, n, score=100 - n) for n in range(1, total + 1)]
    sent: list[str] = []

    async def ok(text):
        sent.append(text)

    await daily.run_daily(cfg, conn, send=ok, today=WEDNESDAY)
    assert _stamps(conn, "dream_notified_at") == set(ids[: daily_dream.MAX_DREAM_JOBS])
    assert "3 more" in sent[0]
    # The next Wednesday carries the three that did not fit.
    sent.clear()
    await daily.run_daily(cfg, conn, send=ok, today=WEDNESDAY + dt.timedelta(days=7))
    assert _stamps(conn, "dream_notified_at") == set(ids)


async def test_a_dry_run_stamps_nothing(cfg, conn, monkeypatch):
    _stub_pipeline(monkeypatch)
    _job(conn, 1)

    async def ok(text):
        return None

    res = await daily.run_daily(cfg, conn, send=ok, dry_run=True, today=WEDNESDAY)
    assert "Dream list" in res.digest
    assert _stamps(conn, "dream_notified_at") == set()

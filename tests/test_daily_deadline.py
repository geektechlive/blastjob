"""The daily run's deadline and run lock (#95): it stops cleanly, says so honestly, records why,
and leaves the rest for the next run. Discovery and matching are stubbed; no network, no LLM."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
from types import SimpleNamespace

import pytest

from blastjob import daily
from blastjob.core import run_lock
from blastjob.db import client as dbc
from blastjob.discovery.pipeline import DiscoveryResult
from blastjob.llm import gateway
from blastjob.llm.cost import CallCost
from blastjob.models.config import BlastJobConfig
from tests.test_run_lock import _spawn_holder

TODAY = dt.date(2026, 9, 8)
FIVE_MINUTES = 5 * 60


class Clock:
    """The gateway's monotonic clock, so a test can spend the deadline without waiting."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def spend(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(gateway, "time", SimpleNamespace(monotonic=c.monotonic))
    return c


@pytest.fixture
def db():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    cfg.limits.daily_deadline_minutes = 5
    conn = dbc.init_db(cfg)
    scored = dbc.upsert_job(
        conn, source="t", external_id="1", company="Acme", title="CIO", url="https://a/1"
    )
    conn.execute(
        "UPDATE jobs SET review_state='surfaced', alignment_score=88 WHERE id=?", (scored,)
    )
    for n in (2, 3):  # two never-scored jobs, still waiting
        dbc.upsert_job(
            conn, source="t", external_id=str(n), company=f"Co{n}", title="VP", url=f"https://c/{n}"
        )
    conn.commit()
    yield cfg, conn
    conn.close()


class Matched:
    cost = CallCost()
    scored = 0
    notes: list[str] = []
    surfaced = 0
    queued = 0
    scoring_failures = 0


def _stub(monkeypatch, *, discovery=None, matching=None):
    async def fake_discovery(cfg, conn, adapters=None):
        if discovery:
            discovery()
        return DiscoveryResult(fetched=0)

    async def fake_matching(cfg, conn, tracker, on_progress=None, **kw):
        if matching:
            return matching()
        return Matched()

    import blastjob.discovery.pipeline as dp
    import blastjob.matching.pipeline as mp

    calls = {"matching": 0, "targets": 0}

    async def counted_matching(*a, **k):
        calls["matching"] += 1
        return await fake_matching(*a, **k)

    async def fake_targets(*a, **k):
        calls["targets"] += 1

    monkeypatch.setattr(dp, "run_discovery", fake_discovery)
    monkeypatch.setattr(mp, "run_matching", counted_matching)
    monkeypatch.setattr(daily.daily_targets, "targets_step", fake_targets)
    return calls


async def _run(cfg, conn, sent: list[str], **kw) -> daily.DailyResult:
    async def send(text: str) -> None:
        sent.append(text)

    return await daily.run_daily(cfg, conn, send=send, today=TODAY, **kw)


def _last(conn):
    return conn.execute("SELECT * FROM daily_runs ORDER BY id DESC LIMIT 1").fetchone()


@pytest.mark.asyncio
async def test_a_run_inside_the_limit_is_complete(db, monkeypatch, clock):
    cfg, conn = db
    calls = _stub(monkeypatch)
    sent: list[str] = []

    res = await _run(cfg, conn, sent)

    assert res.stopped_reason == ""
    assert calls == {"matching": 1, "targets": 1}
    assert _last(conn)["stopped_reason"] == ""
    assert not any("Stopped at" in s for s in sent)


@pytest.mark.asyncio
async def test_deadline_passing_during_discovery_stops_before_scoring(db, monkeypatch, clock):
    cfg, conn = db
    calls = _stub(monkeypatch, discovery=lambda: clock.spend(FIVE_MINUTES + 1))
    sent: list[str] = []

    res = await _run(cfg, conn, sent)

    assert res.stopped_reason == "deadline"
    assert calls == {"matching": 0, "targets": 0}  # no new work after the cutoff
    assert _last(conn)["stopped_reason"] == "deadline"
    assert not res.errors  # a cutoff is not a failure
    # honest wording, with the real count of jobs still waiting, and the digest goes out
    assert res.sent and len(sent) == 1
    assert "Stopped at the 5-minute limit; up to 2 jobs left for tomorrow" in sent[0]


@pytest.mark.asyncio
async def test_a_refused_call_mid_scoring_stops_the_run_with_an_honest_status(
    db, monkeypatch, clock
):
    cfg, conn = db

    def scoring_cut_off():
        clock.spend(FIVE_MINUTES - 5)  # 5 s left: under the 30 s minimum to start a call
        with pytest.raises(gateway.DeadlineReached):
            gateway.call_limit(900.0)
        raise gateway.DeadlineReached("not starting a model call")

    calls = _stub(monkeypatch, matching=scoring_cut_off)
    sent: list[str] = []

    res = await _run(cfg, conn, sent)

    assert res.stopped_reason == "deadline"
    assert "matching" not in res.errors  # not reported as a pipeline failure
    assert calls["targets"] == 0
    assert "up to 2 jobs left for tomorrow" in sent[0]
    assert _last(conn)["stopped_reason"] == "deadline"


@pytest.mark.asyncio
async def test_a_refused_call_that_the_scorer_swallowed_still_marks_the_stop(
    db, monkeypatch, clock
):
    """The scorer counts a refused batch as a failed batch. The run must still say it stopped
    at the limit, and not blame the batch."""
    cfg, conn = db

    def swallowed():
        clock.spend(FIVE_MINUTES - 5)
        with pytest.raises(gateway.DeadlineReached):
            gateway.call_limit(900.0)
        m = Matched()
        m.scoring_failures = 1
        return m

    _stub(monkeypatch, matching=swallowed)
    sent: list[str] = []

    res = await _run(cfg, conn, sent)

    assert res.stopped_reason == "deadline"
    assert "scoring" not in res.errors
    assert "Stopped at the 5-minute limit" in sent[0]


@pytest.mark.asyncio
async def test_the_stop_is_never_reported_as_complete_in_status(db, monkeypatch, clock):
    cfg, conn = db
    _stub(monkeypatch, discovery=lambda: clock.spend(FIVE_MINUTES + 1))
    await _run(cfg, conn, [])

    status = daily._format_status(_last(conn), today=TODAY)

    assert status.startswith("STOPPED EARLY: hit the daily time limit (deadline)")


@pytest.mark.asyncio
async def test_a_dry_run_stops_at_the_deadline_but_records_nothing(db, monkeypatch, clock):
    cfg, conn = db
    _stub(monkeypatch, discovery=lambda: clock.spend(FIVE_MINUTES + 1))

    res = await _run(cfg, conn, [], dry_run=True)

    assert res.stopped_reason == "deadline"
    assert conn.execute("SELECT COUNT(*) n FROM daily_runs").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_a_held_lock_skips_the_run_and_records_lock_held(db, monkeypatch):
    cfg, conn = db
    calls = _stub(monkeypatch)
    holder = _spawn_holder(daily.cfg_mod.data_dir(cfg))
    sent: list[str] = []
    try:
        res = await _run(cfg, conn, sent)
    finally:
        holder.kill()
        holder.join()

    assert res.stopped_reason == "lock_held"
    assert calls == {"matching": 0, "targets": 0}
    assert sent == []  # a skip does not spam; the next run's digest carries everything
    row = _last(conn)
    assert row["stopped_reason"] == "lock_held"
    assert "daily run" in json.loads(row["errors"]).get("lock", "")
    assert daily._format_status(row, today=TODAY).startswith("SKIPPED: another run held")
    # nothing was stamped as announced
    assert conn.execute("SELECT COUNT(*) n FROM jobs WHERE notified_at<>''").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_a_dry_run_that_finds_the_lock_held_records_nothing(db, monkeypatch):
    cfg, conn = db
    _stub(monkeypatch)
    holder = _spawn_holder(daily.cfg_mod.data_dir(cfg))
    try:
        res = await _run(cfg, conn, [], dry_run=True)
    finally:
        holder.kill()
        holder.join()

    assert res.stopped_reason == "lock_held"
    assert conn.execute("SELECT COUNT(*) n FROM daily_runs").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_pause_wins_over_the_lock_and_records_nothing(db, monkeypatch):
    cfg, conn = db
    _stub(monkeypatch)
    (daily.cfg_mod.data_dir(cfg) / daily.PAUSE_FILE).touch()

    res = await _run(cfg, conn, [])

    assert res.paused and res.stopped_reason == ""
    assert conn.execute("SELECT COUNT(*) n FROM daily_runs").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_the_lock_is_held_for_the_whole_run_and_released_after(db, monkeypatch):
    cfg, conn = db
    seen: list[object] = []
    _stub(monkeypatch, discovery=lambda: seen.append(run_lock.holder_of(_dir(cfg))))

    await _run(cfg, conn, [])

    assert seen and seen[0] is not None and seen[0].what == "daily run"
    assert run_lock.holder_of(_dir(cfg)) is None


def _dir(cfg):
    return daily.cfg_mod.data_dir(cfg)


def _skip_row(conn) -> None:
    conn.execute(
        "INSERT INTO daily_runs (started_at, finished_at, duration_s, fetched, per_source, scored, "
        "scoring_batches, scoring_s, new_jobs, followups, sent, errors, stopped_reason) "
        "VALUES ('2026-09-07T07:30:00', '2026-09-07T07:30:00', 0, 0, '{}', 0, 0, 0, 0, 0, 0, '{}', "
        "'lock_held')"
    )
    conn.commit()


@pytest.mark.asyncio
async def test_a_digest_that_goes_out_mentions_earlier_skipped_runs(db, monkeypatch, clock):
    cfg, conn = db
    _stub(monkeypatch)
    _skip_row(conn)
    sent: list[str] = []

    await _run(cfg, conn, sent)

    assert "1 earlier run skipped: another run held the run lock" in sent[0]
    # once a digest has gone out the skip is not repeated
    _skip_row_count = conn.execute("SELECT COUNT(*) n FROM daily_runs").fetchone()["n"]
    assert _skip_row_count == 2
    await _run(cfg, conn, sent)
    assert len(sent) == 1  # nothing new: quiet


@pytest.mark.asyncio
async def test_a_skipped_run_never_makes_a_quiet_day_send(db, monkeypatch, clock):
    cfg, conn = db
    _stub(monkeypatch)
    sent: list[str] = []
    await _run(cfg, conn, sent)  # announces everything
    _skip_row(conn)

    await _run(cfg, conn, sent)

    assert len(sent) == 1


def test_a_skipped_run_does_not_count_in_the_weekly_totals(db):
    _, conn = db
    _skip_row(conn)
    assert daily.week_totals(conn, dt.date(2026, 9, 8))["runs"] == 0


def test_main_prints_the_skip_not_a_misleading_summary(monkeypatch, capsys, tmp_path):
    from blastjob import config as cfg_mod

    holder = _spawn_holder(cfg_mod.resolve_data_dir())
    try:
        code = daily.main(["--no-score"])
    finally:
        holder.kill()
        holder.join()

    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("daily run skipped: Another run is in progress (daily run")
    assert "backup" not in out


@pytest.mark.asyncio
async def test_after_the_cutoff_only_the_cheap_local_steps_still_run(db, monkeypatch, clock):
    """The reply scan and packet filing make no model call, so they still run; every model or
    network step is skipped and named as not run (never as done)."""
    from blastjob import daily_steps

    cfg, conn = db
    calls = _stub(monkeypatch, discovery=lambda: clock.spend(FIVE_MINUTES + 1))
    ran: list[str] = []

    async def replies(*a, **k):
        ran.append("replies")

    async def closure_pass(*a, **k):
        ran.append("closure")

    async def lookup(*a, **k):
        ran.append("lookup")

    monkeypatch.setattr(daily_steps, "_replies_pass", replies)
    monkeypatch.setattr(daily_steps, "_closure_pass", closure_pass)
    monkeypatch.setattr(
        daily_steps.daily_filing, "filing_pass", lambda *a, **k: ran.append("filing")
    )
    monkeypatch.setattr(daily_steps.daily_employer_jd, "lookup_step", lookup)
    sent: list[str] = []

    res = await _run(cfg, conn, sent)

    assert ran == ["filing", "replies"]
    assert calls == {"matching": 0, "targets": 0}
    assert res.stopped_reason == "deadline"
    not_run = sent[0].split("Not run (time limit): ")[1].splitlines()[0]
    assert "employer-description copies" in not_run and "scoring" in not_run
    assert "closure checks" in not_run and "target scans" in not_run
    assert "repl" not in not_run and "filing" not in not_run and "discovery" not in not_run
    assert "Not run (time limit): " in daily._format_status(_last(conn), today=TODAY)


@pytest.mark.asyncio
async def test_a_refused_target_scan_is_named_not_counted_failed(db, monkeypatch, clock):
    cfg, conn = db
    _stub(monkeypatch)

    async def targets(cfg_, conn_, res, **kw):
        res.skipped_steps.append("target scans (2 still due)")
        clock.spend(FIVE_MINUTES - 5)
        with pytest.raises(gateway.DeadlineReached):
            gateway.call_limit(900.0)

    monkeypatch.setattr(daily.daily_targets, "targets_step", targets)
    sent: list[str] = []

    res = await _run(cfg, conn, sent)

    assert res.target_failed == 0 and "target_watch" not in res.errors
    assert res.stopped_reason == "deadline"
    assert "target scans (2 still due)" in sent[0]
    assert "failed" not in sent[0]


@pytest.mark.asyncio
async def test_a_run_cut_at_the_limit_sends_even_on_a_quiet_day(db, monkeypatch, clock):
    """Intended: the owner wants to know when a run was cut off, so a deadline stop makes a
    digest go out although nothing else would have been announced."""
    cfg, conn = db
    _stub(monkeypatch)
    sent: list[str] = []
    await _run(cfg, conn, sent)  # announces everything pending
    assert len(sent) == 1
    await _run(cfg, conn, sent)  # an ordinary quiet day: nothing sent
    assert len(sent) == 1
    _stub(monkeypatch, discovery=lambda: clock.spend(FIVE_MINUTES + 1))

    res = await _run(cfg, conn, sent)

    assert res.stopped_reason == "deadline" and len(sent) == 2
    assert "Stopped at the 5-minute limit" in sent[1]


@pytest.mark.asyncio
async def test_a_failed_lock_acquisition_is_recorded_like_a_held_lock(db, monkeypatch):
    import errno
    import fcntl

    cfg, conn = db
    calls = _stub(monkeypatch)

    def no_locks(_fd, _op):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", no_locks)

    res = await _run(cfg, conn, [])

    assert res.stopped_reason == "lock_held" and calls["matching"] == 0
    assert _last(conn)["stopped_reason"] == "lock_held"
    assert "No locks available" in json.loads(_last(conn)["errors"])["lock"]


def test_the_funnel_ignores_skipped_runs(db):
    from blastjob.web.funnel import build_funnel

    cfg, conn = db
    _skip_row(conn)
    conn.execute(
        "INSERT INTO daily_runs (started_at, finished_at, duration_s, fetched, per_source, scored, "
        "scoring_batches, scoring_s, new_jobs, followups, sent, errors, stopped_reason) "
        "VALUES ('2026-09-06T07:30:00', '2026-09-06T07:40:00', 1, 12, '{}', 1, 1, 1, 1, 0, "
        "1, '{}', '')"
    )
    conn.commit()

    runs = build_funnel(conn, cfg).runs

    assert [r["fetched"] for r in runs] == [12]  # the skipped run is not a zero-fetch day

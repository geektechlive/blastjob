"""Quiet-application nudge and weekly pace line (#99). Invented data, offline."""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest
from pydantic import ValidationError

from blastjob import daily, daily_pace
from blastjob.core import events
from blastjob.daily_digest import build_digest
from blastjob.db import client as dbc
from blastjob.discovery.pipeline import DiscoveryResult
from blastjob.models.config import NotifyConfig
from blastjob.web import stage_lists as sl
from tests.asks_fixture import add_job, add_network, make_cfg

TODAY = dt.date(2026, 10, 26)  # a Monday


def _day(n: int) -> str:
    """The ISO date `n` days before TODAY."""
    return (TODAY - dt.timedelta(days=n)).isoformat()


@pytest.fixture
def conn():
    cfg = make_cfg()
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(
    conn: sqlite3.Connection,
    ext: str,
    state: str,
    *,
    changed: str | None = None,
    applied_at: str = "",
    due: str = "",
) -> int:
    jid = add_job(conn, ext)
    conn.execute(
        "UPDATE jobs SET application_state=?, stage_changed_at=?, applied_at=?, "
        "next_action_due=? WHERE id=?",
        (state, changed if changed is not None else "", applied_at, due, jid),
    )
    conn.commit()
    return jid


def _event(conn: sqlite3.Connection, jid: int, kind: str, at: str) -> None:
    events.insert_event(conn, jid, kind, at)
    conn.commit()


def _reason(conn: sqlite3.Connection, jid: int, quiet: int = 21) -> str:
    rows = {j["id"]: j for j in sl.applied_jobs(conn, TODAY, quiet_after_days=quiet)}
    return rows[jid]["followup_reason"]


# --- quiet threshold ---------------------------------------------------------


@pytest.mark.parametrize("state", ["screen", "interview"])
def test_quiet_boundary_for_screen_and_interview(conn, state):
    just = _job(conn, "a", state, changed=_day(21) + "T09:00:00")
    short = _job(conn, "b", state, changed=_day(20) + "T09:00:00")
    assert _reason(conn, just) == "no update in 21 days: follow up, or mark it ghosted"
    assert _reason(conn, short) == ""


def test_newer_event_resets_the_anchor(conn):
    jid = _job(conn, "a", "screen", changed=_day(40))
    assert "no update in 40 days" in _reason(conn, jid)
    _event(conn, jid, "note", _day(5))
    assert _reason(conn, jid) == ""
    _event(conn, jid, "note", _day(25) + "T10:00:00")  # older than the newest: no effect
    assert _reason(conn, jid) == ""


def test_backdated_event_older_than_the_stage_change_does_not_reset(conn):
    jid = _job(conn, "a", "interview", changed=_day(30))
    _event(conn, jid, "note", _day(60))
    assert "no update in 30 days" in _reason(conn, jid)


def test_same_day_and_future_events_are_not_quiet(conn):
    same = _job(conn, "a", "screen", changed=_day(50))
    _event(conn, same, "note", TODAY.isoformat())
    future = _job(conn, "b", "screen", changed=_day(50))
    _event(conn, future, "note", (TODAY + dt.timedelta(days=3)).isoformat())
    assert _reason(conn, same) == ""
    assert _reason(conn, future) == ""


def test_malformed_dates_are_skipped(conn):
    bad = _job(conn, "a", "screen", changed="not a date")
    assert _reason(conn, bad) == ""
    # A bad stage date with a good event still anchors on the event.
    mixed = _job(conn, "b", "screen", changed="garbage")
    _event(conn, mixed, "note", _day(30))
    assert "no update in 30 days" in _reason(conn, mixed)


def test_next_action_due_still_wins(conn):
    later = (TODAY + dt.timedelta(days=2)).isoformat()
    future = _job(conn, "a", "screen", changed=_day(60), due=later)
    arrived = _job(conn, "b", "screen", changed=_day(60), due=_day(1))
    assert _reason(conn, future) == ""
    assert _reason(conn, arrived) == f"follow-up due {_day(1)}"


def test_the_threshold_is_configurable(conn):
    jid = _job(conn, "a", "screen", changed=_day(10))
    assert _reason(conn, jid, quiet=21) == ""
    assert _reason(conn, jid, quiet=7).startswith("no update in 10 days")


def test_job_page_reason_reads_the_stored_threshold(conn):
    jid = _job(conn, "a", "screen", changed=_day(10) + "T08:00:00")
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert sl.followup_reason(row, conn, today=TODAY) == ""
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('notify.quiet_after_days','7','x')"
    )
    conn.commit()
    assert sl.followup_reason(row, conn, today=TODAY).startswith("no update in 10 days")


# --- applied reason ----------------------------------------------------------


def test_applied_reason_gains_ghost_suffix_at_the_threshold(conn):
    mid = _job(conn, "a", "applied", changed=_day(20), applied_at=_day(20))
    edge = _job(conn, "b", "applied", changed=_day(21), applied_at=_day(21))
    assert _reason(conn, mid) == "applied 20 days ago, no update logged"
    assert _reason(conn, edge) == ("applied 21 days ago, no update logged, or mark it ghosted")


def test_applied_under_seven_days_is_not_due(conn):
    jid = _job(conn, "a", "applied", changed=_day(3), applied_at=_day(3))
    assert _reason(conn, jid) == ""


def test_ordering_is_unchanged(conn):
    older = _job(conn, "a", "screen", changed=_day(60), due=_day(10))
    newer = _job(conn, "b", "screen", changed=_day(60), due=_day(2))
    applied = _job(conn, "c", "applied", changed=_day(30), applied_at=_day(30))
    ids = [j["id"] for j in sl.followups_due(conn, TODAY, quiet_after_days=21)]
    assert ids == [applied, older, newer]  # earliest due or applied date first


# --- settings ----------------------------------------------------------------


def test_notify_defaults():
    n = NotifyConfig()
    assert n.quiet_after_days == 21 and n.pace_weekday == "mon"


@pytest.mark.parametrize("bad", [6, 91, 0, -1])
def test_quiet_after_days_out_of_range_is_rejected(bad):
    with pytest.raises(ValidationError):
        NotifyConfig(quiet_after_days=bad)


@pytest.mark.parametrize("ok", [7, 90])
def test_quiet_after_days_bounds_are_accepted(ok):
    assert NotifyConfig(quiet_after_days=ok).quiet_after_days == ok


@pytest.mark.parametrize("bad", ["monday", "", "funday"])
def test_pace_weekday_rejects_unknown_values(bad):
    with pytest.raises(ValidationError):
        NotifyConfig(pace_weekday=bad)


@pytest.mark.parametrize("ok", ["mon", "tue", "wed", "thu", "fri", "sat", "sun", "off"])
def test_pace_weekday_accepts_each_day_and_off(ok):
    assert NotifyConfig(pace_weekday=ok).pace_weekday == ok


# --- pace line ---------------------------------------------------------------


def _ask(conn, asked_on: str) -> None:
    n = add_network(conn, f"Person {asked_on}")
    conn.execute(
        "INSERT INTO outreach_asks (network_contact_id, recipient_name, kind, channel, "
        "asked_on, created_at, updated_at) VALUES (?, 'P', 'advice', 'email', ?, 'x', 'x')",
        (n, asked_on),
    )
    conn.commit()


def test_previous_week_is_monday_to_sunday():
    start, end, label = daily_pace.previous_iso_week(dt.date(2026, 10, 26))
    assert (start, end, label) == (dt.date(2026, 10, 19), dt.date(2026, 10, 25), "2026-W43")
    # A mid-week run reports the same previous week.
    assert daily_pace.previous_iso_week(dt.date(2026, 10, 29))[2] == "2026-W43"


def test_previous_week_across_the_iso_year_boundary():
    start, end, label = daily_pace.previous_iso_week(dt.date(2027, 1, 4))
    assert (start, end) == (dt.date(2026, 12, 28), dt.date(2027, 1, 3))
    assert label == "2026-W53"


def test_counts_match_fixtures(conn):
    a = _job(conn, "a", "applied")
    b = _job(conn, "b", "applied")
    _event(conn, a, "applied", "2026-10-19")  # Monday, in
    _event(conn, b, "applied", "2026-10-25T23:30:00")  # Sunday, in
    _event(conn, b, "applied", "2026-10-26")  # next Monday, out
    _event(conn, a, "applied", "2026-10-18")  # prior Sunday, out
    _event(conn, a, "recruiter_reply", "2026-10-22")
    _event(conn, a, "screen", "2026-10-23")
    _event(conn, a, "note", "2026-10-23")  # not a reply kind
    _ask(conn, "2026-10-20")
    _ask(conn, "2026-10-25")
    _ask(conn, "2026-10-26")
    counts = daily_pace.week_counts(conn, dt.date(2026, 10, 19), dt.date(2026, 10, 25))
    assert (counts.applied, counts.asks, counts.replies) == (2, 2, 2)


def test_pace_due_on_its_day_and_on_a_delayed_run_once(conn):
    mon = daily_pace.pace_report(conn, "mon", dt.date(2026, 10, 26))
    assert mon is not None and mon.on_day and mon.label == "2026-W43"
    thu = daily_pace.pace_report(conn, "mon", dt.date(2026, 10, 29))
    assert thu is not None and not thu.on_day and thu.label == "2026-W43"
    daily_pace.stamp(conn, thu.label)
    assert daily_pace.pace_report(conn, "mon", dt.date(2026, 10, 29)) is None
    assert daily_pace.pace_report(conn, "mon", dt.date(2026, 11, 2)) is not None  # next week


def test_pace_not_due_before_its_weekday_and_off_disables(conn):
    assert daily_pace.pace_report(conn, "wed", dt.date(2026, 10, 27)) is None
    on_wed = daily_pace.pace_report(conn, "wed", dt.date(2026, 10, 28))
    assert on_wed is not None and on_wed.on_day
    assert daily_pace.pace_report(conn, "off", dt.date(2026, 10, 26)) is None


def test_pace_line_text(conn):
    a = _job(conn, "a", "applied")
    _event(conn, a, "applied", "2026-10-20")
    rep = daily_pace.pace_report(conn, "mon", TODAY)
    assert rep is not None
    assert "Oct 19" in rep.line and "Oct 25" in rep.line
    assert "1 application" in rep.line and "0 outreach asks" in rep.line
    assert "0 reply events" in rep.line
    assert "—" not in rep.line


def test_build_digest_pace_only_counts_as_news_on_its_day():
    assert build_digest([], {}, [], pace="the pace", pace_is_news=False) == ""
    on_day = build_digest([], {}, [], pace="the pace", pace_is_news=True)
    assert "the pace" in on_day
    with_news = build_digest([], {"s": "boom"}, [], pace="the pace", pace_is_news=False)
    assert "the pace" in with_news


# --- daily integration -------------------------------------------------------


class Sink:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def __call__(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("down")
        self.sent.append(text)


@pytest.fixture
def env(monkeypatch):
    import blastjob.discovery.pipeline as dp
    import blastjob.matching.pipeline as mp

    async def fake_discovery(cfg, conn, adapters=None):
        return DiscoveryResult(fetched=0, per_source={}, errors={})

    async def fake_matching(cfg, conn, tracker, on_progress=None, **kw):
        return mp.MatchResult()

    monkeypatch.setattr(dp, "run_discovery", fake_discovery)
    monkeypatch.setattr(mp, "run_matching", fake_matching)
    cfg = make_cfg()
    c = dbc.init_db(cfg)
    yield cfg, c
    c.close()


def _stamp(conn) -> str | None:
    row = conn.execute("SELECT value FROM digest_stamps WHERE name='pace'").fetchone()
    return row[0] if row else None


@pytest.mark.asyncio
async def test_quiet_day_with_only_the_pace_line_sends_on_its_day(env):
    cfg, conn = env
    cfg.notify.pace_weekday = "tue"
    sink = Sink()
    res = await daily.run_daily(cfg, conn, send=sink, today=dt.date(2026, 10, 27))
    assert res.sent and "applications" in sink.sent[0]
    assert _stamp(conn) == "2026-W43"
    await daily.run_daily(cfg, conn, send=sink, today=dt.date(2026, 10, 28))
    assert len(sink.sent) == 1  # once per week


@pytest.mark.asyncio
async def test_delayed_pace_line_rides_a_digest_sent_anyway_but_never_sends_alone(env):
    cfg, conn = env
    cfg.notify.pace_weekday = "tue"
    sink = Sink()
    await daily.run_daily(cfg, conn, send=sink, today=dt.date(2026, 10, 29))
    assert sink.sent == [] and _stamp(conn) is None
    jid = _job(conn, "a", "screen", changed=_day(40))
    await daily.run_daily(cfg, conn, send=sink, today=dt.date(2026, 10, 29))
    assert len(sink.sent) == 1
    assert "Follow-ups due" in sink.sent[0] and "no update in" in sink.sent[0]
    assert "applications" in sink.sent[0] and f"/jobs/{jid}>" in sink.sent[0]
    assert _stamp(conn) == "2026-W43"


@pytest.mark.asyncio
async def test_off_disables_the_pace_line(env):
    cfg, conn = env
    cfg.notify.pace_weekday = "off"
    sink = Sink()
    await daily.run_daily(cfg, conn, send=sink, today=dt.date(2026, 10, 27))
    assert sink.sent == [] and _stamp(conn) is None


@pytest.mark.asyncio
async def test_a_raising_send_and_a_dry_run_stamp_nothing(env):
    cfg, conn = env
    cfg.notify.pace_weekday = "tue"
    with pytest.raises(RuntimeError):
        await daily.run_daily(cfg, conn, send=Sink(fail=True), today=dt.date(2026, 10, 27))
    assert _stamp(conn) is None
    await daily.run_daily(cfg, conn, send=Sink(), dry_run=True, today=dt.date(2026, 10, 27))
    assert _stamp(conn) is None


@pytest.mark.asyncio
async def test_notify_off_mode_stamps_after_its_no_op_send(env):
    cfg, conn = env
    cfg.notify.mode = "off"
    cfg.notify.pace_weekday = "tue"
    sender = daily.sender_for(cfg, dry_run=False)
    await daily.run_daily(cfg, conn, send=sender, today=dt.date(2026, 10, 27))
    assert _stamp(conn) == "2026-W43"


def _reason_stored(conn, jid: int) -> str:
    rows = {j["id"]: j for j in sl.applied_jobs(conn, TODAY)}
    return rows[jid]["followup_reason"]


@pytest.mark.parametrize("stored", ['"abc"', "500", "6", "true", "null", "{bad json"])
def test_an_invalid_stored_threshold_falls_back_to_21_and_pages_still_render(conn, stored, caplog):
    jid = _job(conn, "a", "screen", changed=_day(25))
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('notify.quiet_after_days', ?, 'x')",
        (stored,),
    )
    conn.commit()
    assert sl.stored_quiet_after_days(conn) == 21
    assert "using 21" in caplog.text
    assert _reason_stored(conn, jid).startswith("no update in 25 days")
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert sl.followup_reason(row, conn, today=TODAY).startswith("no update in 25 days")


# --- review fixes: defanged follow-up lines, weekday validation, one warning per bad value ---


def test_followup_line_defangs_feed_text():
    hostile = {
        "id": 7,
        "company": "@everyone Acme",
        "title": "@everyone [x](http://evil.example)",
        "followup_reason": "no update in 30 days: follow up, or mark it ghosted",
    }
    text = build_digest([], {}, [hostile])
    line = next(ln for ln in text.splitlines() if "no update in 30 days" in ln)
    assert "@everyone" not in line
    assert "](http://evil.example)" not in line
    assert "/jobs/7>" in line


def test_a_bad_weekday_is_refused_with_its_own_message_and_stores_nothing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from blastjob import config as cfg_mod
    from blastjob.web.app import create_app
    from tests.test_web_settings_db import _form

    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(tmp_path / "data"))
    client = TestClient(create_app())
    r = client.post("/settings", data=_form(client, notify_pace_weekday="funday"))
    assert r.status_code == 400
    assert "Weekly pace line: choose mon to sun, or off" in r.text
    assert cfg_mod.load_config().notify.pace_weekday == "mon"


def test_a_cross_field_notify_error_names_its_own_field():
    from blastjob.web.settings_form import FormError, _notify_recruiter

    form = {"notify_recruiter_touch_days": "120", "notify_recruiter_lookback_days": "90"}
    with pytest.raises(FormError, match="Recruiter reminder lookback"):
        _notify_recruiter(NotifyConfig(), form)


def test_the_invalid_threshold_warning_is_logged_once_per_distinct_value(conn, caplog):
    sl._WARNED_QUIET_VALUES.clear()
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('notify.quiet_after_days','500','x')"
    )
    conn.commit()
    for _ in range(5):
        assert sl.stored_quiet_after_days(conn) == 21
    assert caplog.text.count("using 21") == 1
    conn.execute("UPDATE settings SET value='\"abc\"' WHERE key='notify.quiet_after_days'")
    conn.commit()
    sl.stored_quiet_after_days(conn)
    assert caplog.text.count("using 21") == 2

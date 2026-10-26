"""Recruiter status, last touch and reminder selection (#98, ADR 0062)."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from blastjob.core import outreach_asks as oa
from blastjob.core import recruiter_status as rs
from blastjob.db import client as dbc
from blastjob.db.schema import MIGRATIONS
from blastjob.models.config import NotifyConfig
from tests.asks_fixture import add_job, add_network, add_recruiter, make_cfg

TODAY = dt.date(2026, 10, 26)


@pytest.fixture
def conn():
    c = dbc.init_db(make_cfg())
    yield c
    c.close()


def _ask(conn, rid, asked_on="2026-10-20", status="waiting", kind="recruiter", job_id=None):
    ask = oa.log_ask(
        conn,
        job_id=job_id,
        network_contact_id=rid,
        contact_id=None,
        kind=kind,
        channel="email",
        asked_on=asked_on,
        today=TODAY,
    )
    if status != "waiting":
        oa.update_ask(conn, ask, status=status, follow_up_on="", note="")
    return ask


def _standing(conn, rid) -> rs.Standing:
    return rs.standings(conn)[rid]


def test_migrations_66_to_68_add_the_columns_one_alter_each():
    added = {v: sql for v, sql in MIGRATIONS if v in (66, 67, 68)}
    assert sorted(added) == [66, 67, 68]
    assert all(sql.count("ALTER TABLE") == 1 for sql in added.values())


def test_no_ask_and_no_messages_is_not_contacted(conn):
    rid = add_recruiter(conn, "Ria Fox")
    assert _standing(conn, rid).status == "not_contacted"


@pytest.mark.parametrize(
    "ask_status,expected",
    [
        ("waiting", "reached_out"),
        ("no_reply", "reached_out"),
        ("replied", "spoke"),
        ("helped", "spoke"),
        ("declined", "spoke"),
    ],
)
def test_automatic_status_from_each_ask_status(conn, ask_status, expected):
    rid = add_recruiter(conn, "Ria Fox")
    _ask(conn, rid, status=ask_status)
    s = _standing(conn, rid)
    assert (s.status, s.source) == (expected, "auto")


def test_message_history_counts_as_spoke(conn):
    rid = add_recruiter(conn, "Ria Fox", messages=3, last_message_at="2026-03-01")
    assert _standing(conn, rid).status == "spoke"


def test_an_answered_ask_wins_over_a_newer_unanswered_one(conn):
    rid = add_recruiter(conn, "Ria Fox")
    _ask(conn, rid, asked_on="2026-09-01", status="replied")
    _ask(conn, rid, asked_on="2026-10-20", status="waiting")
    assert _standing(conn, rid).status == "spoke"


def test_a_job_linked_ask_counts_too(conn):
    rid = add_recruiter(conn, "Ria Fox")
    job = add_job(conn, "1")
    conn.execute(
        "INSERT INTO outreach_asks (job_id, network_contact_id, recipient_name, kind, channel,"
        " asked_on, status, created_at, updated_at) VALUES (?, ?, 'Ria Fox', 'intro', 'email',"
        " '2026-10-01', 'helped', 'x', 'x')",
        (job, rid),
    )
    conn.commit()
    assert _standing(conn, rid).status == "spoke"


def test_a_stored_status_overrides_and_auto_clears_it(conn):
    rid = add_recruiter(conn, "Ria Fox", messages=2, last_message_at="2026-03-01")
    assert rs.set_status(conn, rid, "active")
    s = _standing(conn, rid)
    assert (s.status, s.source) == ("active", "set")
    rs.set_status(conn, rid, "auto")
    s = _standing(conn, rid)
    assert (s.status, s.source) == ("spoke", "auto")


def test_the_automatic_status_never_yields_the_owner_only_values(conn):
    rid = add_recruiter(conn, "Ria Fox", messages=9, last_message_at="2026-10-25")
    _ask(conn, rid, status="helped")
    assert _standing(conn, rid).status not in rs.OWNER_ONLY


def test_do_not_contact_needs_a_reason(conn):
    rid = add_recruiter(conn, "Ria Fox")
    with pytest.raises(rs.StatusError):
        rs.set_status(conn, rid, "do_not_contact", "  ")
    assert _standing(conn, rid).source == "auto"


def test_reason_limit_is_120_characters(conn):
    rid = add_recruiter(conn, "Ria Fox")
    with pytest.raises(rs.StatusError):
        rs.set_status(conn, rid, "do_not_contact", "x" * 121)
    assert rs.set_status(conn, rid, "do_not_contact", "x" * 120)
    assert _standing(conn, rid).reason == "x" * 120


def test_a_reason_is_kept_only_for_do_not_contact(conn):
    rid = add_recruiter(conn, "Ria Fox")
    rs.set_status(conn, rid, "spoke", "left over")
    assert _standing(conn, rid).reason == ""


def test_unknown_status_and_unknown_contact(conn):
    rid = add_recruiter(conn, "Ria Fox")
    with pytest.raises(rs.StatusError):
        rs.set_status(conn, rid, "bogus")
    assert rs.set_status(conn, 9999, "spoke") is False


@pytest.mark.parametrize(
    "ask_on,msg,expected",
    [
        ("2026-10-01", "2026-09-01T10:00:00", ("2026-10-01", "ask")),
        ("2026-08-01", "2026-09-01", ("2026-09-01", "LinkedIn message")),
        ("2026-09-01", "2026-09-01T23:59:59", ("2026-09-01", "ask")),  # tie reads as the ask
        (None, "2026-09-01", ("2026-09-01", "LinkedIn message")),
        ("2026-09-01", "", ("2026-09-01", "ask")),
        (None, "not a date", ("", "")),
        (None, "", ("", "")),
    ],
)
def test_last_touch_is_the_later_source(ask_on, msg, expected):
    assert rs.last_touch(ask_on, msg) == expected


def test_last_touch_ignores_row_order(conn):
    rid = add_recruiter(conn, "Ria Fox")
    _ask(conn, rid, asked_on="2026-10-10")
    _ask(conn, rid, asked_on="2026-07-01")  # entered later, older
    assert _standing(conn, rid).last_touch == "2026-10-10"


# --- reminders ---------------------------------------------------------------------------


def _due(conn, touch=42, lookback=365, today=TODAY):
    return rs.due_reminders(conn, today, touch_days=touch, lookback_days=lookback)


def _spoke(conn, name, days_ago, messages=1):
    when = (TODAY - dt.timedelta(days=days_ago)).isoformat()
    return add_recruiter(conn, name, messages=messages, last_message_at=when)


def test_threshold_boundary_is_inclusive(conn):
    at = _spoke(conn, "At Edge", 42)
    _spoke(conn, "Just Inside", 41)
    assert [r.contact_id for r in _due(conn)] == [at]
    assert _due(conn)[0].days_since == 42


def test_do_not_contact_and_not_contacted_are_never_due(conn):
    dnc = _spoke(conn, "No Contact", 100)
    rs.set_status(conn, dnc, "do_not_contact", "asked me to stop")
    add_recruiter(conn, "Never Met")
    assert _due(conn) == []


def test_not_reminded_twice_for_the_same_last_touch(conn):
    rid = _spoke(conn, "Ria Fox", 60)
    [r] = _due(conn)
    rs.mark_reminded(conn, [(r.contact_id, r.last_touch)])
    assert _due(conn) == []
    assert rid == r.contact_id


def test_a_new_ask_rearms_the_reminder(conn):
    rid = _spoke(conn, "Ria Fox", 200)
    [r] = _due(conn)
    rs.mark_reminded(conn, [(rid, r.last_touch)])
    _ask(conn, rid, asked_on="2026-08-01")  # 86 days ago: a new touch, again past the threshold
    assert [x.contact_id for x in _due(conn)] == [rid]


def test_a_touch_logged_between_render_and_stamp_keeps_the_new_cycle_armed(conn):
    rid = _spoke(conn, "Ria Fox", 100)
    [r] = _due(conn)
    _ask(conn, rid, asked_on="2026-09-01")  # logged while the digest was being sent
    rs.mark_reminded(conn, [(rid, r.last_touch)])  # stamps the OLD date
    later = TODAY + dt.timedelta(days=42)
    assert [x.contact_id for x in _due(conn, today=later)] == [rid]


def test_at_most_five_warmest_first(conn):
    ids = [_spoke(conn, f"Pat {i}", 60, messages=i + 1) for i in range(7)]
    got = [r.contact_id for r in _due(conn)]
    assert len(got) == 5
    assert got == ids[::-1][:5]


def test_lookback_limits_spoke_but_not_active(conn):
    old_spoke = _spoke(conn, "Old Spoke", 400)
    old_active = _spoke(conn, "Old Active", 400)
    rs.set_status(conn, old_active, "active")
    assert [r.contact_id for r in _due(conn)] == [old_active]
    assert old_spoke in [r.contact_id for r in _due(conn, lookback=500)]


def test_active_without_any_touch_is_skipped(conn):
    rid = add_recruiter(conn, "Ria Fox")
    rs.set_status(conn, rid, "active")
    assert _due(conn) == []


def test_a_reached_out_recruiter_is_not_a_touch_again_reminder(conn):
    rid = add_recruiter(conn, "Ria Fox")
    _ask(conn, rid, asked_on="2026-08-01")  # unanswered: #97's follow-up date owns this
    assert _due(conn) == []


def test_not_a_recruiter_is_never_listed(conn):
    rid = _spoke(conn, "Ria Fox", 60)
    conn.execute(
        "UPDATE network_contacts SET recruiter_override='not_recruiter' WHERE id=?", (rid,)
    )
    conn.commit()
    assert _due(conn) == []


# --- log_ask: the effective-recruiter check ----------------------------------------------


def test_jobless_recruiter_note_to_a_non_recruiter_is_rejected(conn):
    n = add_network(conn, "Ann Lee")
    with pytest.raises(oa.AskError):
        _ask(conn, n)
    assert conn.execute("SELECT COUNT(*) FROM outreach_asks").fetchone()[0] == 0


def test_jobless_recruiter_note_to_a_recruiter_is_accepted(conn):
    rid = add_recruiter(conn, "Ria Fox")
    assert _ask(conn, rid)


def test_jobless_advice_to_a_non_recruiter_still_works(conn):
    n = add_network(conn, "Ann Lee")
    assert _ask(conn, n, kind="advice")


# --- the settings --------------------------------------------------------------------------


def test_touch_and_lookback_defaults():
    n = NotifyConfig()
    assert (n.recruiter_touch_days, n.recruiter_lookback_days) == (42, 365)


@pytest.mark.parametrize("bad", [0, 13, 121, -1])
def test_touch_days_outside_14_to_120_is_rejected(bad):
    with pytest.raises(ValidationError):
        NotifyConfig(recruiter_touch_days=bad)


@pytest.mark.parametrize("ok", [14, 120])
def test_touch_days_bounds_are_accepted(ok):
    assert NotifyConfig(recruiter_touch_days=ok).recruiter_touch_days == ok


@pytest.mark.parametrize("bad", [0, 89, 1096])
def test_lookback_outside_90_to_1095_is_rejected(bad):
    with pytest.raises(ValidationError):
        NotifyConfig(recruiter_lookback_days=bad)


@pytest.mark.parametrize("ok", [90, 1095])
def test_lookback_bounds_are_accepted(ok):
    assert NotifyConfig(recruiter_lookback_days=ok).recruiter_lookback_days == ok


def test_lookback_shorter_than_touch_days_is_rejected():
    with pytest.raises(ValidationError):
        NotifyConfig(recruiter_touch_days=120, recruiter_lookback_days=100)
    assert NotifyConfig(recruiter_touch_days=120, recruiter_lookback_days=120)


@pytest.mark.parametrize("asked_on", ["２０２６-１０-２０", "2026-10-٢٠"])
def test_unicode_digits_in_a_date_are_rejected(conn, asked_on):
    rid = add_recruiter(conn, "Ria Fox")
    with pytest.raises(oa.AskError):
        _ask(conn, rid, asked_on=asked_on)


def test_stored_dates_are_the_normalised_isoformat(conn):
    rid = add_recruiter(conn, "Ria Fox")
    ask = _ask(conn, rid, asked_on="2026-10-20")
    row = oa.get_ask(conn, ask)
    assert row["asked_on"] == "2026-10-20" and row["follow_up_on"] == "2026-10-27"


def test_mark_reminded_skips_a_row_already_stamped_with_that_date(conn):
    rid = add_recruiter(conn, "Ria Fox")
    rs.mark_reminded(conn, [(rid, "2026-09-01")])
    before = conn.total_changes
    rs.mark_reminded(conn, [(rid, "2026-09-01")])
    assert conn.total_changes == before

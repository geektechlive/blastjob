"""The recruiter-label reader (#83): one label, read-only, a watermark per UIDVALIDITY, the
sender checks, the size rule and the triage table. A strict fake IMAP object; no network."""

from __future__ import annotations

import itertools
import logging

import pytest

from blastjob.core import events
from blastjob.discovery import label_mail as lm
from tests.label_fixture import (
    GOOD_AUTH,
    LABEL,
    FakeImap,
    Msg,
    connector,
    no_connection,
    raw_message,
)


def _msgs(n: int, start: int = 1) -> dict[int, Msg]:
    return {
        u: Msg(raw_message(message_id=f"<m{u}@firm.example.com>", subject=f"Role {u}"))
        for u in range(start, start + n)
    }


def _rows(conn) -> list:
    return conn.execute("SELECT * FROM mail_triage ORDER BY uid").fetchall()


def _kinds(fake: FakeImap) -> set[str]:
    return {c[0] for c in fake.calls}


# --- off switches -----------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_label_opens_no_connection(mail_env):
    cfg, conn = mail_env
    cfg.discovery.recruiter_label = ""
    scan = await lm.scan_label(cfg, conn, connect=no_connection)
    assert scan.recorded == 0 and not scan.ran


@pytest.mark.asyncio
async def test_missing_credentials_open_no_connection(monkeypatch, mail_env):
    cfg, conn = mail_env
    monkeypatch.delenv("TEST_LABEL_MAIL_PASSWORD")
    scan = await lm.scan_label(cfg, conn, connect=no_connection)
    assert scan.recorded == 0 and not scan.ran


@pytest.mark.asyncio
async def test_a_label_the_validator_refuses_is_never_selected(mail_env):
    cfg, conn = mail_env
    cfg.discovery.recruiter_label = "INBOX"
    scan = await lm.scan_label(cfg, conn, connect=no_connection)
    assert not scan.ran


# --- exactly one folder, read-only --------------------------------------------


@pytest.mark.asyncio
async def test_only_the_configured_label_is_selected_quoted_and_readonly(mail_env):
    cfg, conn = mail_env
    fake = FakeImap(_msgs(2))
    await lm.scan_label(cfg, conn, connect=connector(fake))
    selects = [c for c in fake.calls if c[0] == "select"]
    assert selects == [("select", f'"{LABEL}"', True)]
    assert _kinds(fake) <= {"login", "select", "response", "uid", "logout"}
    assert all(c[1] in ("SEARCH", "FETCH") for c in fake.calls if c[0] == "uid")


@pytest.mark.asyncio
async def test_every_fetch_peeks_so_nothing_is_marked_read(mail_env):
    cfg, conn = mail_env
    fake = FakeImap(_msgs(2))
    await lm.scan_label(cfg, conn, connect=connector(fake))
    body_specs = [s for s in fake.fetch_specs if "BODY" in s]
    assert body_specs and all("PEEK" in s for s in body_specs)
    assert not any("Seen" in s for s in fake.fetch_specs)


@pytest.mark.asyncio
async def test_a_missing_label_warns_records_nothing_and_does_not_raise(mail_env, caplog):
    cfg, conn = mail_env
    fake = FakeImap(_msgs(2), labels=("Other",))
    with caplog.at_level(logging.WARNING):
        scan = await lm.scan_label(cfg, conn, connect=connector(fake))
    assert scan.label_missing and scan.recorded == 0 and _rows(conn) == []
    assert any(LABEL in r.getMessage() for r in caplog.records)
    assert not any(c[0] == "uid" for c in fake.calls)


@pytest.mark.asyncio
async def test_no_uidvalidity_reads_nothing(mail_env, caplog):
    cfg, conn = mail_env
    fake = FakeImap(_msgs(2), uidvalidity=None)
    with caplog.at_level(logging.WARNING):
        scan = await lm.scan_label(cfg, conn, connect=connector(fake))
    assert scan.recorded == 0 and _rows(conn) == []


# --- recording -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_records_a_row_without_storing_the_body(mail_env):
    cfg, conn = mail_env
    body = "Hello <b>Chris</b>, a role.\n\nOn Mon, Ria wrote:\n> quoted"
    fake = FakeImap({1: Msg(raw_message(body=body, html=True))})
    scan = await lm.scan_label(cfg, conn, connect=connector(fake))
    (row,) = _rows(conn)
    assert scan.recorded == 1
    assert row["sender_name"] == "Ria Fox" and row["sender_address"] == "ria@firm.example.com"
    assert row["subject"] == "A VP role you might like"
    assert "<b>" not in row["snippet"] and "Hello Chris" in row["snippet"]
    assert "quoted" not in row["snippet"]
    assert row["status"] == "new" and row["version"] == 0
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mail_triage)")}
    assert not {"body", "html", "raw"} & cols


@pytest.mark.asyncio
async def test_subject_and_snippet_are_capped(mail_env):
    cfg, conn = mail_env
    fake = FakeImap({1: Msg(raw_message(subject="S" * 900, body="word " * 900))})
    await lm.scan_label(cfg, conn, connect=connector(fake))
    (row,) = _rows(conn)
    assert len(row["subject"]) <= 300 and len(row["snippet"]) <= 500


@pytest.mark.asyncio
async def test_second_run_records_nothing_new_and_leaves_the_reply_ledger_alone(mail_env):
    cfg, conn = mail_env
    conn.execute(
        "INSERT INTO mail_ledger (message_id, source, uid, status, postings, processed_at) "
        "VALUES ('<m1@firm.example.com>', 'reply', '9', 'ok', 1, '2026-10-26')"
    )
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM mail_ledger").fetchone()[0]
    fake = FakeImap(_msgs(3))
    first = await lm.scan_label(cfg, conn, connect=connector(fake))
    second = await lm.scan_label(cfg, conn, connect=connector(fake))
    assert first.recorded == 3 and second.recorded == 0
    assert conn.execute("SELECT COUNT(*) FROM mail_ledger").fetchone()[0] == before
    assert len(_rows(conn)) == 3


@pytest.mark.asyncio
async def test_a_message_without_a_message_id_is_recorded(mail_env):
    cfg, conn = mail_env
    fake = FakeImap({1: Msg(raw_message(message_id=None))})
    await lm.scan_label(cfg, conn, connect=connector(fake))
    (row,) = _rows(conn)
    assert row["message_id"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header_date, shown",
    [
        ("not a date at all", ""),
        ("Mon, 26 Oct 2031 09:00:00 +0000", "2031-10-26"),
        (None, ""),
    ],
)
async def test_received_at_comes_from_internaldate_not_the_header(mail_env, header_date, shown):
    cfg, conn = mail_env
    fake = FakeImap(
        {1: Msg(raw_message(date=header_date), internaldate="25-Oct-2026 08:30:00 +0000")}
    )
    await lm.scan_label(cfg, conn, connect=connector(fake))
    (row,) = _rows(conn)
    assert row["received_at"].startswith("2026-10-25T08:30:00")
    assert row["header_date"] == shown


# --- sender checks ------------------------------------------------------------------


def test_the_topmost_gmail_header_wins_over_a_forged_lower_one():
    top = "mx.google.com; spf=fail; dkim=none; dmarc=fail"
    forged = "mx.google.com; spf=pass; dkim=pass; dmarc=pass"
    got = lm.parse_auth_results([top, forged])
    assert (got.spf, got.dkim, got.dmarc) == ("fail", "none", "fail")


def test_a_top_header_from_another_server_is_not_trusted_and_nothing_below_is_used():
    forged_top = "evil.example.net; spf=pass; dkim=pass; dmarc=pass"
    got = lm.parse_auth_results([forged_top, GOOD_AUTH])
    assert (got.spf, got.dkim, got.dmarc) == ("unknown", "unknown", "unknown")


def test_no_header_means_every_result_is_unknown():
    got = lm.parse_auth_results([])
    assert (got.spf, got.dkim, got.dmarc) == ("unknown", "unknown", "unknown")


def test_an_odd_result_word_is_unknown():
    got = lm.parse_auth_results(["mx.google.com; spf=temperror; dkim=pass; dmarc=bestguesspass"])
    assert (got.spf, got.dkim, got.dmarc) == ("unknown", "pass", "unknown")


@pytest.mark.asyncio
async def test_recorded_results_come_from_the_topmost_gmail_header(mail_env):
    cfg, conn = mail_env
    raw = raw_message(auth=("mx.google.com; dmarc=fail; spf=fail; dkim=fail", GOOD_AUTH))
    await lm.scan_label(cfg, conn, connect=connector(FakeImap({1: Msg(raw)})))
    (row,) = _rows(conn)
    assert (row["auth_spf"], row["auth_dkim"], row["auth_dmarc"]) == ("fail", "fail", "fail")


# --- the cap, the watermark and UIDVALIDITY ------------------------------------------


@pytest.mark.asyncio
async def test_sixty_messages_record_the_fifty_oldest_then_the_rest_next_run(mail_env):
    cfg, conn = mail_env
    fake = FakeImap(_msgs(60))
    first = await lm.scan_label(cfg, conn, connect=connector(fake))
    assert first.recorded == lm.MAX_PER_RUN == 50
    assert [r["uid"] for r in _rows(conn)] == list(range(1, 51))
    second = await lm.scan_label(cfg, conn, connect=connector(fake))
    assert second.recorded == 10
    assert [r["uid"] for r in _rows(conn)] == list(range(1, 61))


@pytest.mark.asyncio
async def test_a_restart_mid_backlog_resumes_at_the_watermark(mail_env):
    cfg, conn = mail_env
    fake = FakeImap(_msgs(60))
    await lm.scan_label(cfg, conn, connect=connector(fake))
    from blastjob.db import client as dbc

    conn.close()
    reopened = dbc.init_db(cfg)
    fresh = FakeImap(_msgs(60))
    scan = await lm.scan_label(cfg, reopened, connect=connector(fresh))
    assert scan.recorded == 10 and fresh.fetched_full == list(range(51, 61))
    reopened.close()


@pytest.mark.asyncio
async def test_a_failed_fetch_stops_the_run_so_that_message_is_retried(mail_env):
    cfg, conn = mail_env
    fake = FakeImap(_msgs(5), fail_uids={3})
    first = await lm.scan_label(cfg, conn, connect=connector(fake))
    assert first.recorded == 2 and [r["uid"] for r in _rows(conn)] == [1, 2]
    fake.fail_uids.clear()
    second = await lm.scan_label(cfg, conn, connect=connector(fake))
    assert second.recorded == 3 and [r["uid"] for r in _rows(conn)] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_a_message_that_vanished_is_passed_not_blocking(mail_env):
    cfg, conn = mail_env
    fake = FakeImap(_msgs(3), vanished_uids={2})
    scan = await lm.scan_label(cfg, conn, connect=connector(fake))
    assert scan.recorded == 2
    again = await lm.scan_label(cfg, conn, connect=connector(fake))
    assert again.recorded == 0


@pytest.mark.asyncio
async def test_the_deadline_stops_the_sweep_and_keeps_the_watermark_contiguous(mail_env):
    cfg, conn = mail_env
    ticks = itertools.chain([0.0, 0.0], itertools.repeat(1000.0))
    fake = FakeImap(_msgs(4))
    scan = await lm.scan_label(cfg, conn, connect=connector(fake), clock=lambda: next(ticks))
    assert scan.recorded == 1 and [r["uid"] for r in _rows(conn)] == [1]
    rest = await lm.scan_label(cfg, conn, connect=connector(fake))
    assert rest.recorded == 3


@pytest.mark.asyncio
async def test_a_uidvalidity_change_skips_seen_message_ids_and_still_drains(mail_env):
    cfg, conn = mail_env
    first = FakeImap(_msgs(60), uidvalidity=7)
    await lm.scan_label(cfg, conn, connect=connector(first))
    await lm.scan_label(cfg, conn, connect=connector(first))
    assert len(_rows(conn)) == 60
    # The server renumbered: the same 60 messages plus one new one under a new UIDVALIDITY.
    renumbered = _msgs(60)
    renumbered[61] = Msg(raw_message(message_id="<fresh@firm.example.com>"))
    second = FakeImap(renumbered, uidvalidity=8)
    runs = [await lm.scan_label(cfg, conn, connect=connector(second)) for _ in range(3)]
    assert sum(r.recorded for r in runs) == 1
    rows = conn.execute("SELECT * FROM mail_triage WHERE uidvalidity=8").fetchall()
    assert [r["message_id"] for r in rows] == ["<fresh@firm.example.com>"]
    assert runs[-1].recorded == 0


@pytest.mark.asyncio
async def test_the_same_message_in_both_scanners_is_fine(mail_env):
    cfg, conn = mail_env
    conn.execute(
        "INSERT INTO mail_ledger (message_id, source, uid, status, postings, processed_at) "
        "VALUES ('<m1@firm.example.com>', 'reply', '9', 'ok', 1, '2026-10-26')"
    )
    conn.commit()
    scan = await lm.scan_label(cfg, conn, connect=connector(FakeImap(_msgs(1))))
    assert scan.recorded == 1


# --- size --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_message_over_two_megabytes_is_recorded_from_headers_only(mail_env):
    cfg, conn = mail_env
    big = Msg(raw_message(), size=lm.MAX_FULL_FETCH_BYTES + 1)
    fake = FakeImap({1: big, 2: Msg(raw_message(message_id="<small@firm.example.com>"))})
    await lm.scan_label(cfg, conn, connect=connector(fake))
    first, second = _rows(conn)
    assert first["snippet"] == lm.TOO_LARGE_SNIPPET
    assert first["sender_address"] == "ria@firm.example.com"
    assert fake.fetched_full == [2]
    assert second["snippet"] != lm.TOO_LARGE_SNIPPET


@pytest.mark.asyncio
async def test_a_message_just_under_the_limit_is_fetched_whole(mail_env):
    cfg, conn = mail_env
    fake = FakeImap({1: Msg(raw_message(), size=lm.MAX_FULL_FETCH_BYTES)})
    await lm.scan_label(cfg, conn, connect=connector(fake))
    assert fake.fetched_full == [1]


# --- the dry run and the event source ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_dry_run_records_nothing(mail_env):
    cfg, conn = mail_env
    scan = await lm.scan_label(cfg, conn, connect=connector(FakeImap(_msgs(2))), record=False)
    assert scan.recorded == 0 and _rows(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM mail_triage_cursor").fetchone()[0] == 0


def test_mail_label_is_a_known_event_source():
    assert "mail_label" in events.EVENT_SOURCES


# --- hardening after the security review --------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "<script " + "x" * 1_900_000,
        "<" * 1_900_000,
        "<script>" * 200_000,
        "From: a " * 200_000,
        "<a " * 600_000,
    ],
    ids=["unclosed-script", "many-lt", "many-script-opens", "many-from", "many-open-tags"],
)
async def test_a_hostile_body_cannot_stall_the_sweep(mail_env, body):
    import time

    cfg, conn = mail_env
    fake = FakeImap({1: Msg(raw_message(body=body, html=True))})
    started = time.monotonic()
    scan = await lm.scan_label(cfg, conn, connect=connector(fake))
    assert scan.recorded == 1
    assert time.monotonic() - started < 2.0
    assert len(_rows(conn)[0]["snippet"]) <= 500


def test_the_reply_scanners_snippet_is_bounded_and_linear_too():
    import time

    from blastjob.discovery import replies

    started = time.monotonic()
    out = replies._snippet("<script " + "<" * 2_000_000)
    assert time.monotonic() - started < 2.0 and len(out) <= replies.MAX_SNIPPET_CHARS


@pytest.mark.asyncio
async def test_the_header_fetch_is_capped_and_size_comes_without_a_literal(mail_env):
    cfg, conn = mail_env
    fake = FakeImap(_msgs(1))
    await lm.scan_label(cfg, conn, connect=connector(fake))
    assert any("<0.65536>" in s for s in fake.fetch_specs)
    assert any(s.strip("()") == "RFC822.SIZE INTERNALDATE" for s in fake.fetch_specs)


@pytest.mark.asyncio
async def test_a_missing_size_is_treated_as_too_large(mail_env):
    cfg, conn = mail_env
    fake = FakeImap({1: Msg(raw_message(), omit_size=True)})
    await lm.scan_label(cfg, conn, connect=connector(fake))
    (row,) = _rows(conn)
    assert row["snippet"] == lm.TOO_LARGE_SNIPPET and fake.fetched_full == []


@pytest.mark.asyncio
async def test_a_missing_internaldate_uses_the_recorded_time_and_flags_the_row(mail_env):
    cfg, conn = mail_env
    fake = FakeImap({1: Msg(raw_message(), omit_internaldate=True)})
    await lm.scan_label(cfg, conn, connect=connector(fake))
    (row,) = _rows(conn)
    from datetime import UTC, datetime

    age = datetime.now(UTC) - datetime.fromisoformat(row["received_at"])
    assert row["received_estimated"] == 1 and abs(age.total_seconds()) < 60


@pytest.mark.asyncio
async def test_a_given_internaldate_is_not_flagged(mail_env):
    cfg, conn = mail_env
    await lm.scan_label(cfg, conn, connect=connector(FakeImap(_msgs(1))))
    assert _rows(conn)[0]["received_estimated"] == 0


def test_a_comment_cannot_forge_a_result():
    top = "mx.google.com; dkim=fail (test dkim=pass here) header.i=@x; spf=none; dmarc=none"
    got = lm.parse_auth_results([top])
    assert got.dkim == "fail"
    nested = "mx.google.com; spf=fail (a (b dmarc=pass) c); dmarc=none"
    assert lm.parse_auth_results([nested]).dmarc == "none"


def test_an_enormous_unclosed_comment_is_cheap():
    import time

    started = time.monotonic()
    got = lm.parse_auth_results(["mx.google.com; spf=pass " + "(" * 500_000])
    assert time.monotonic() - started < 1.0 and got.spf == "pass"


# --- a message that never fetches ----------------------------------------------------------------


def _failures(conn):
    return conn.execute("SELECT * FROM mail_triage_failures").fetchall()


@pytest.mark.asyncio
async def test_a_message_that_fails_three_runs_is_skipped_on_the_fourth(mail_env):
    cfg, conn = mail_env
    fake = FakeImap(_msgs(3), fail_uids={2})
    for _ in range(3):
        scan = await lm.scan_label(cfg, conn, connect=connector(fake))
        assert [r["uid"] for r in _rows(conn)] == [1]
    assert scan.recorded == 0
    (fail,) = _failures(conn)
    assert fail["runs"] == 3 and fail["given_up"] == 0
    fourth = await lm.scan_label(cfg, conn, connect=connector(fake))
    assert fourth.recorded == 1 and [r["uid"] for r in _rows(conn)] == [1, 3]
    (fail,) = _failures(conn)
    assert fail["given_up"] == 1
    again = await lm.scan_label(cfg, conn, connect=connector(fake))
    assert again.recorded == 0


@pytest.mark.asyncio
async def test_a_success_clears_the_failure_count(mail_env):
    cfg, conn = mail_env
    fake = FakeImap(_msgs(2), fail_uids={2})
    await lm.scan_label(cfg, conn, connect=connector(fake))
    await lm.scan_label(cfg, conn, connect=connector(fake))
    fake.fail_uids.clear()
    await lm.scan_label(cfg, conn, connect=connector(fake))
    assert _failures(conn) == [] and [r["uid"] for r in _rows(conn)] == [1, 2]


@pytest.mark.asyncio
async def test_a_dry_run_counts_no_failure(mail_env):
    cfg, conn = mail_env
    fake = FakeImap(_msgs(2), fail_uids={2})
    await lm.scan_label(cfg, conn, connect=connector(fake), record=False)
    assert _failures(conn) == []

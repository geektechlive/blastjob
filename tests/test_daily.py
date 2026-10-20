"""Daily runner + Discord digest: formatting, chunking, notified_at stamping
(only after a confirmed send), pause switch, heartbeat. Discovery/matching are
stubbed; no network, no LLM."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import tempfile
from pathlib import Path

import httpx
import pytest

from blastjob import daily
from blastjob.db import client as dbc
from blastjob.discovery.pipeline import DiscoveryResult
from blastjob.llm.cost import CallCost
from blastjob.models.config import BlastJobConfig
from blastjob.notify import discord


@pytest.fixture
def db():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)
    a = dbc.upsert_job(
        conn,
        source="t",
        external_id="1",
        company="Acme",
        title="Chief Officer",
        url="https://a/1",
        geo_location_raw="Fairhaven, OH",
        comp_min=210000,
        comp_max=240000,
        comp_provenance="posted",
    )
    b = dbc.upsert_job(
        conn,
        source="t",
        external_id="2",
        company="Beta",
        title="VP Ops",
        url="https://b/2",
        geo_remote=1,
    )
    c = dbc.upsert_job(conn, source="t", external_id="3", company="Gamma", title="Dir", url="")
    conn.execute("UPDATE jobs SET review_state='surfaced', alignment_score=88 WHERE id=?", (a,))
    conn.execute("UPDATE jobs SET review_state='queued', alignment_score=79 WHERE id=?", (b,))
    conn.execute("UPDATE jobs SET review_state='archived', alignment_score=90 WHERE id=?", (c,))
    conn.commit()
    yield cfg, conn, {"a": a, "b": b, "c": c}
    conn.close()


def _stub_pipeline(monkeypatch, errors=None):
    async def fake_discovery(cfg, conn, adapters=None):
        return DiscoveryResult(
            fetched=3, per_source={"adzuna": 2, "workday:x": 1}, errors=dict(errors or {})
        )

    class M:
        cost = CallCost()
        scored = 2
        notes: list[str] = []
        surfaced = 1
        queued = 1
        scoring_failures = 0

    async def fake_matching(cfg, conn, tracker, on_progress=None, **kw):
        if on_progress is not None:
            on_progress(1, 3)
            on_progress(2, 3)
            on_progress(3, 3)
        return M()

    import blastjob.discovery.pipeline as dp
    import blastjob.matching.pipeline as mp

    monkeypatch.setattr(dp, "run_discovery", fake_discovery)
    monkeypatch.setattr(mp, "run_matching", fake_matching)


def test_pending_jobs_excludes_archived_and_orders_by_score(db):
    _, conn, ids = db
    rows = daily.pending_jobs(conn)
    assert [r["id"] for r in rows] == [ids["a"], ids["b"]]


def test_pending_jobs_skips_a_repost_clustered_with_a_decided_role(db):
    """A repost merged into a kept (applied) or archived role is not a new role:
    the cockpit hides it, and the digest must not announce it either."""
    _, conn, ids = db
    kept = dbc.upsert_job(conn, source="t", external_id="4", company="Acme", title="CIO", url="")
    conn.execute("INSERT INTO job_clusters (fingerprint, created_at) VALUES ('k', '')")
    cid = conn.execute("SELECT max(id) FROM job_clusters").fetchone()[0]
    conn.execute(
        "UPDATE jobs SET review_state='kept', application_state='applied', cluster_id=? WHERE id=?",
        (cid, kept),
    )
    conn.execute("UPDATE jobs SET cluster_id=? WHERE id=?", (cid, ids["a"]))
    conn.execute("INSERT INTO job_clusters (fingerprint, created_at) VALUES ('x', '')")
    cid2 = conn.execute("SELECT max(id) FROM job_clusters").fetchone()[0]
    conn.execute("UPDATE jobs SET cluster_id=? WHERE id IN (?, ?)", (cid2, ids["b"], ids["c"]))
    conn.commit()
    assert daily.pending_jobs(conn) == []


def test_digest_format_has_header_lines_and_badges(db):
    _, conn, _ = db
    text = daily.build_digest(daily.pending_jobs(conn), {}, [])
    assert text.startswith("🎯 **blastjob** — 2 new roles")
    assert "**88** Acme — Chief Officer" in text
    assert "$240K posted" in text
    assert "Beta — VP Ops (review)" in text
    assert "Remote" in text
    assert "<http://127.0.0.1:8765/jobs/" in text


def test_digest_empty_when_nothing_to_say():
    assert daily.build_digest([], {}, []) == ""
    assert "Source errors" in daily.build_digest([], {"workday:x": "boom"}, [])


def test_heartbeat_only_on_monday(db):
    _, conn, _ = db
    assert daily.heartbeat_line(conn, dt.date(2026, 9, 7)) != ""  # Monday
    assert daily.heartbeat_line(conn, dt.date(2026, 9, 8)) == ""


@pytest.mark.asyncio
async def test_run_daily_stamps_only_after_send(db, monkeypatch):
    cfg, conn, ids = db
    _stub_pipeline(monkeypatch)
    sent: list[str] = []

    async def ok(text):
        sent.append(text)

    res = await daily.run_daily(cfg, conn, send=ok, today=dt.date(2026, 9, 8))
    assert res.sent and res.new_jobs == 2 and len(sent) == 1
    stamped = conn.execute("SELECT COUNT(*) n FROM jobs WHERE notified_at<>''").fetchone()["n"]
    assert stamped == 2
    # second run: nothing new -> quiet, nothing sent
    res2 = await daily.run_daily(cfg, conn, send=ok, today=dt.date(2026, 9, 8))
    assert not res2.sent and res2.digest == "" and len(sent) == 1


@pytest.mark.asyncio
async def test_run_daily_does_not_stamp_when_send_fails(db, monkeypatch):
    cfg, conn, _ = db
    _stub_pipeline(monkeypatch)

    async def boom(text):
        raise httpx.HTTPError("discord down")

    with pytest.raises(httpx.HTTPError):
        await daily.run_daily(cfg, conn, send=boom, today=dt.date(2026, 9, 8))
    stamped = conn.execute("SELECT COUNT(*) n FROM jobs WHERE notified_at<>''").fetchone()["n"]
    assert stamped == 0


@pytest.mark.asyncio
async def test_run_daily_dry_run_sends_and_stamps_nothing(db, monkeypatch):
    cfg, conn, _ = db
    _stub_pipeline(monkeypatch)
    calls: list[str] = []

    async def spy(text):
        calls.append(text)

    res = await daily.run_daily(cfg, conn, send=spy, dry_run=True, today=dt.date(2026, 9, 8))
    assert res.digest and not res.sent and calls == []
    assert conn.execute("SELECT COUNT(*) n FROM jobs WHERE notified_at<>''").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_run_daily_respects_pause_file(db, monkeypatch):
    cfg, conn, _ = db
    _stub_pipeline(monkeypatch)
    Path(cfg.paths.data_dir, daily.PAUSE_FILE).touch()

    async def never(text):
        raise AssertionError("should not send")

    res = await daily.run_daily(cfg, conn, send=never)
    assert res.paused and not res.sent


@pytest.mark.asyncio
async def test_run_daily_reports_source_errors(db, monkeypatch):
    cfg, conn, _ = db
    _stub_pipeline(monkeypatch, errors={"workday:acme": "HTTPStatusError: 500"})
    sent: list[str] = []

    async def ok(text):
        sent.append(text)

    await daily.run_daily(cfg, conn, send=ok, today=dt.date(2026, 9, 8))
    assert "**Source errors**" in sent[0] and "workday:acme" in sent[0]


# ---------- a pipeline-level throw (not a single adapter's isolated failure) ----------


def _stub_discovery_raises(monkeypatch, message="HTTPStatusError: 500"):
    import blastjob.discovery.pipeline as dp

    async def boom(cfg, conn, adapters=None):
        raise RuntimeError(message)

    monkeypatch.setattr(dp, "run_discovery", boom)


def _stub_matching_raises(monkeypatch, message="RuntimeError: LLM call failed"):
    import blastjob.discovery.pipeline as dp
    import blastjob.matching.pipeline as mp

    async def fake_discovery(cfg, conn, adapters=None):
        return DiscoveryResult(fetched=1, per_source={"adzuna": 1})

    async def boom(cfg, conn, tracker, on_progress=None, **kw):
        raise RuntimeError(message)

    monkeypatch.setattr(dp, "run_discovery", fake_discovery)
    monkeypatch.setattr(mp, "run_matching", boom)


@pytest.mark.asyncio
async def test_run_daily_discovery_failure_records_run_and_attempts_discord(db, monkeypatch):
    cfg, conn, _ = db
    _stub_discovery_raises(monkeypatch)
    sent: list[str] = []

    async def ok(text):
        sent.append(text)

    res = await daily.run_daily(cfg, conn, send=ok, today=dt.date(2026, 9, 8))
    assert res.errors.get("discovery", "").startswith("RuntimeError")
    row = daily.last_run(conn)
    assert row is not None
    assert "discovery" in json.loads(row["errors"])
    assert sent  # Discord was still attempted
    assert "discovery" in sent[0]


@pytest.mark.asyncio
async def test_run_daily_matching_failure_records_run_and_attempts_discord(db, monkeypatch):
    cfg, conn, _ = db
    _stub_matching_raises(monkeypatch)
    sent: list[str] = []

    async def ok(text):
        sent.append(text)

    res = await daily.run_daily(cfg, conn, send=ok, today=dt.date(2026, 9, 8))
    assert res.errors.get("matching", "").startswith("RuntimeError")
    row = daily.last_run(conn)
    assert row is not None
    assert "matching" in json.loads(row["errors"])
    assert sent


@pytest.mark.asyncio
async def test_run_daily_discovery_failure_does_not_propagate(db, monkeypatch):
    """The defect this whole change fixes: a pipeline throw must not escape
    run_daily and skip record_run/notification."""
    cfg, conn, _ = db
    _stub_discovery_raises(monkeypatch)

    async def ok(text):
        pass

    # Must not raise.
    await daily.run_daily(cfg, conn, send=ok, today=dt.date(2026, 9, 8))


@pytest.mark.asyncio
async def test_run_daily_discovery_failure_rolls_back_mid_flight_write(db, monkeypatch):
    """A discovery throw after a mid-flight (uncommitted) write must not have
    that write ride along on record_run's later commit."""
    cfg, conn, _ = db
    import blastjob.discovery.pipeline as dp

    async def half_written(cfg, conn, adapters=None):
        now = dt.datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO jobs (source, external_id, company, title, url, review_state, "
            "source_state, first_seen, last_seen) "
            "VALUES ('t','mid-flight','MidCo','X','','surfaced','active',?,?)",
            (now, now),
        )
        raise RuntimeError("boom mid-write")

    monkeypatch.setattr(dp, "run_discovery", half_written)

    async def ok(text):
        pass

    await daily.run_daily(cfg, conn, send=ok, today=dt.date(2026, 9, 8))
    row = conn.execute("SELECT COUNT(*) n FROM jobs WHERE external_id='mid-flight'").fetchone()
    assert row["n"] == 0


@pytest.mark.asyncio
async def test_run_daily_pipeline_error_is_redacted_before_storing_and_sending(db, monkeypatch):
    secret_msg = (
        "HTTPStatusError: GET https://api.adzuna.com/v1/search?app_id=aaa&app_key=secret 401"
    )
    _stub_discovery_raises(monkeypatch, message=secret_msg)
    cfg, conn, _ = db
    sent: list[str] = []

    async def ok(text):
        sent.append(text)

    res = await daily.run_daily(cfg, conn, send=ok, today=dt.date(2026, 9, 8))
    stored = json.loads(daily.last_run(conn)["errors"])
    assert "secret" not in stored["discovery"]
    assert "[REDACTED]" in stored["discovery"]
    assert "secret" not in sent[0]
    assert "secret" not in res.errors["discovery"]


# ---------- daily_runs observability (migration 13, --status, footer) ----------


def test_migration_13_creates_daily_runs_table(db):
    _, conn, _ = db
    row = conn.execute("SELECT name FROM sqlite_master WHERE name='daily_runs'").fetchone()
    assert row is not None


def test_record_run_and_last_run_round_trip(db):
    _, conn, _ = db
    res = daily.DailyResult(
        started_at="2026-09-05T07:33:00",
        duration_s=125.4,
        fetched=10,
        per_source={"adzuna": 7, "workday:x": 3},
        scored=5,
        scoring_batches=2,
        scoring_s=60.0,
        new_jobs=3,
        followups=1,
        errors={"workday:acme": "boom"},
    )
    run_id = daily.record_run(conn, res)
    row = daily.last_run(conn)
    assert row["id"] == run_id
    assert row["sent"] == 0
    assert json.loads(row["per_source"]) == {"adzuna": 7, "workday:x": 3}
    assert json.loads(row["errors"]) == {"workday:acme": "boom"}
    daily.mark_run_sent(conn, run_id)
    assert daily.last_run(conn)["sent"] == 1


@pytest.mark.asyncio
async def test_run_daily_footer_in_sent_digest_and_records_batches(db, monkeypatch):
    cfg, conn, _ = db
    _stub_pipeline(monkeypatch)
    sent: list[str] = []

    async def ok(text):
        sent.append(text)

    res = await daily.run_daily(cfg, conn, send=ok, today=dt.date(2026, 9, 8))
    assert res.sent
    assert "Run " in sent[0]
    assert "adzuna 2" in sent[0]
    row = daily.last_run(conn)
    assert row["sent"] == 1
    assert row["scoring_batches"] == 3


@pytest.mark.asyncio
async def test_run_daily_quiet_run_records_row_with_sent_zero(db, monkeypatch):
    cfg, conn, ids = db
    _stub_pipeline(monkeypatch)
    daily.mark_notified(conn, [ids["a"], ids["b"]])

    async def never(text):
        raise AssertionError("should not send when quiet")

    res = await daily.run_daily(cfg, conn, send=never, today=dt.date(2026, 9, 8))
    assert res.digest == "" and not res.sent
    row = daily.last_run(conn)
    assert row is not None and row["sent"] == 0


@pytest.mark.asyncio
async def test_run_daily_dry_run_writes_no_daily_runs_row(db, monkeypatch):
    cfg, conn, _ = db
    _stub_pipeline(monkeypatch)

    async def spy(text):
        pass

    await daily.run_daily(cfg, conn, send=spy, dry_run=True, today=dt.date(2026, 9, 8))
    assert daily.last_run(conn) is None


def test_heartbeat_includes_last_7_days_after_recorded_run(db):
    _, conn, _ = db
    res = daily.DailyResult(
        started_at=dt.date(2026, 9, 3).isoformat(), duration_s=300, fetched=5, scored=2
    )
    daily.record_run(conn, res)
    line = daily.heartbeat_line(conn, dt.date(2026, 9, 7))  # Monday
    assert "Last 7 days" in line


# ---------- --sweep-search-firms ----------


def _stub_search_firm_discovery(monkeypatch, conn):
    """Like _stub_pipeline but writes searchfirm rows so the sweep has
    something to print, and asserts run_matching/Discord are never touched
    unless explicitly wired by the caller."""
    from blastjob.discovery.pipeline import DiscoveryResult

    dbc.upsert_job(
        conn,
        source="searchfirm:acmesearch",
        external_id="acmesearch.example/opportunities/1",
        company="Acme Executive Search (retained search)",
        title="Chief Operations Officer",
        url="https://acmesearch.example/opportunities/1",
    )

    async def fake_discovery(cfg, conn, adapters=None):
        return DiscoveryResult(fetched=1, per_source={"searchfirm:acmesearch": 1})

    import blastjob.discovery.pipeline as dp

    monkeypatch.setattr(dp, "run_discovery", fake_discovery)


def test_main_sweep_search_firms_prints_and_skips_discord_and_daily_runs(tmp_path, monkeypatch):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    conn = dbc.init_db(cfg)
    _stub_search_firm_discovery(monkeypatch, conn)
    conn.close()

    monkeypatch.setattr(daily.cfg_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(daily.applog, "setup", lambda: None)

    def boom_sender():
        raise AssertionError("Discord sender must not be constructed for a sweep")

    monkeypatch.setattr(daily, "_discord_sender", boom_sender)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = daily.main(["--sweep-search-firms", "--no-score"])
    assert rc == 0
    out = buf.getvalue()
    assert "searchfirm" in out
    assert "Chief Operations Officer" in out

    conn2 = dbc.init_db(cfg)
    assert daily.last_run(conn2) is None
    conn2.close()


def test_sweep_search_firms_respects_no_score(db, monkeypatch):
    cfg, conn, _ = db
    _stub_search_firm_discovery(monkeypatch, conn)

    called = {"matching": False}

    async def fake_matching(cfg, conn, tracker, **kw):
        called["matching"] = True
        raise AssertionError("run_matching must not be called when score=False")

    import blastjob.matching.pipeline as mp

    monkeypatch.setattr(mp, "run_matching", fake_matching)

    import asyncio as _asyncio

    result = _asyncio.run(daily.sweep_search_firms(cfg, conn, score=False))
    assert not called["matching"]
    assert result["per_source"] == {"searchfirm:acmesearch": 1}
    assert any(j["title"] == "Chief Operations Officer" for j in result["jobs"])


# ---------- nightly backup (Y2) ----------


@pytest.mark.asyncio
async def test_run_daily_sets_backup_path_to_existing_file(db, monkeypatch):
    cfg, conn, _ = db
    _stub_pipeline(monkeypatch)

    async def ok(text):
        pass

    res = await daily.run_daily(cfg, conn, send=ok, today=dt.date(2026, 9, 8))
    assert res.backup_path
    backup_file = Path(res.backup_path)
    assert backup_file.is_file()
    assert backup_file.parent == Path(cfg.paths.data_dir) / "backups"
    assert "backup" not in res.errors


@pytest.mark.asyncio
async def test_run_daily_records_backup_error_but_continues(db, monkeypatch):
    cfg, conn, _ = db
    _stub_pipeline(monkeypatch)

    def boom_backup(conn, dest_dir, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(daily.dbc, "backup_db", boom_backup)

    sent: list[str] = []

    async def ok(text):
        sent.append(text)

    res = await daily.run_daily(cfg, conn, send=ok, today=dt.date(2026, 9, 8))
    assert res.errors.get("backup") == "RuntimeError: disk full"
    assert not res.backup_path
    # discovery/matching/digest still proceed despite the backup failure
    assert res.new_jobs == 2
    assert res.sent and len(sent) == 1


def test_main_status_prints_last_run(tmp_path, monkeypatch):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    conn = dbc.init_db(cfg)
    res = daily.DailyResult(
        started_at="2026-09-05T07:33:00",
        duration_s=125.0,
        fetched=10,
        per_source={"adzuna": 10},
        scored=5,
        scoring_batches=2,
        scoring_s=60.0,
    )
    daily.record_run(conn, res)
    conn.close()

    monkeypatch.setattr(daily.cfg_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(daily.applog, "setup", lambda: None)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = daily.main(["--status"])
    assert rc == 0
    assert "fetched" in buf.getvalue()
    # backup_path isn't a daily_runs column (no migration added for it), so
    # --status can only note that history doesn't carry it — see report.
    assert "backup" in buf.getvalue()


def test_format_status_notes_a_stale_successful_run():
    row_dict = {
        "started_at": "2026-09-05T07:33:00",
        "finished_at": "2026-09-05T07:35:05",
        "duration_s": 125.0,
        "fetched": 10,
        "per_source": json.dumps({"adzuna": 10}),
        "scored": 5,
        "scoring_batches": 2,
        "scoring_s": 60.0,
        "new_jobs": 3,
        "followups": 1,
        "sent": 1,
        "errors": "{}",
    }
    text = daily._format_status(row_dict, today=dt.date(2026, 9, 8))
    assert "3 days ago" in text
    assert "LAST RUN FAILED" not in text


def test_format_status_leads_with_failure_even_when_sent(monkeypatch):
    row_dict = {
        "started_at": "2026-09-08T07:33:00",
        "finished_at": "2026-09-08T07:35:05",
        "duration_s": 125.0,
        "fetched": 0,
        "per_source": "{}",
        "scored": 0,
        "scoring_batches": 0,
        "scoring_s": 0.0,
        "new_jobs": 0,
        "followups": 0,
        "sent": 1,  # a digest naming the failure was still sent
        "errors": json.dumps({"discovery": "RuntimeError: boom"}),
    }
    text = daily._format_status(row_dict, today=dt.date(2026, 9, 8))
    assert text.startswith("LAST RUN FAILED")
    assert "discovery" in text and "boom" in text
    assert "days ago" not in text  # same day as `today`


def test_format_status_handles_malformed_started_at():
    row_dict = {
        "started_at": "",
        "finished_at": "",
        "duration_s": 0.0,
        "fetched": 0,
        "per_source": "{}",
        "scored": 0,
        "scoring_batches": 0,
        "scoring_s": 0.0,
        "new_jobs": 0,
        "followups": 0,
        "sent": 0,
        "errors": "{}",
    }
    text = daily._format_status(row_dict)  # must not raise
    assert "unknown date" in text


def test_main_status_reports_pause_without_showing_a_stale_run(tmp_path, monkeypatch):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    conn = dbc.init_db(cfg)
    old_res = daily.DailyResult(
        started_at="2026-09-01T07:00:00", duration_s=10.0, fetched=1, scored=0
    )
    daily.record_run(conn, old_res)
    conn.close()
    Path(tmp_path, daily.PAUSE_FILE).touch()

    monkeypatch.setattr(daily.cfg_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(daily.applog, "setup", lambda: None)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = daily.main(["--status"])
    assert rc == 0
    out = buf.getvalue()
    assert "paused" in out
    assert "2026-09-01" not in out  # the stale row must not be shown as current


# ---------- replies pass (surface-only reply watching) ----------


def _reply_match(job_id=1):
    from blastjob.discovery.replies import ReplyMatch

    return ReplyMatch(
        message_id="<reply1@lakeshorecu>",
        job_id=job_id,
        company="Lakeshore Credit Union",
        title="Generic VP Role",
        job_url="https://cockpit/lakeshore",
        sender="Sam Lee <sam.lee@example.org>",
        subject="RE: Generic VP Role",
        when="2026-09-15",
        snippet="Full transparency - I have no feedback for anyone.",
        matched_by="app_notes",
    )


def _no_real_secrets(monkeypatch, cfg):
    """`_replies_pass` calls `load_secrets_env()`, which reads the user's real
    ~/.claude/secrets.env -- so without this a test asserting "unconfigured"
    passes or fails depending on the machine it runs on."""
    monkeypatch.setattr(daily.secrets_mod, "load_secrets_env", lambda *a, **k: None)
    for var in (cfg.discovery.mail_user_env, cfg.discovery.mail_password_env):
        monkeypatch.delenv(var, raising=False)


def test_replies_pass_is_a_noop_without_mailbox_credentials(db, monkeypatch):
    """No credentials for the personal mailbox: the pass must not even call
    the reply scanner, and must not report an error for it either."""
    cfg, conn, _ = db
    _no_real_secrets(monkeypatch, cfg)

    async def should_not_run(cfg, conn, **kw):
        raise AssertionError("must not scan without credentials")

    import blastjob.discovery.replies as rp

    monkeypatch.setattr(rp, "scan_replies", should_not_run)
    res = daily.DailyResult()
    import asyncio as _asyncio

    _asyncio.run(daily._replies_pass(cfg, conn, res))
    assert res.replies == [] and "replies" not in res.errors


def test_replies_pass_does_not_record_on_a_dry_run(db, monkeypatch):
    """The wiring, asserted end to end: `--dry-run` must reach scan_replies as
    record=False. The last defect in this module shipped green precisely
    because nothing asserted how the pieces were connected."""
    cfg, conn, _ = db
    monkeypatch.setattr(daily.secrets_mod, "load_secrets_env", lambda *a, **k: None)
    monkeypatch.setenv(cfg.discovery.mail_user_env, "alex@example.com")
    monkeypatch.setenv(cfg.discovery.mail_password_env, "app-pw")

    seen: dict = {}

    async def capture(cfg, conn, **kw):
        seen.update(kw)
        return []

    import asyncio as _asyncio

    import blastjob.discovery.replies as rp

    monkeypatch.setattr(rp, "scan_replies", capture)

    _asyncio.run(daily._replies_pass(cfg, conn, daily.DailyResult(), dry_run=True))
    assert seen["record"] is False

    _asyncio.run(daily._replies_pass(cfg, conn, daily.DailyResult(), dry_run=False))
    assert seen["record"] is True


def test_replies_pass_runs_even_when_the_job_alert_feed_is_off(db, monkeypatch):
    """The decoupling, asserted: `mail_alerts_enabled` governs the LinkedIn/
    Indeed feed on a different account. Switching that feed off must not
    silently stop watching for a recruiter's answer."""
    cfg, conn, _ = db
    cfg.discovery.mail_alerts_enabled = False
    monkeypatch.setattr(daily.secrets_mod, "load_secrets_env", lambda *a, **k: None)
    monkeypatch.setenv(cfg.discovery.mail_user_env, "alex@example.com")
    monkeypatch.setenv(cfg.discovery.mail_password_env, "app-pw")

    ran: list[bool] = []

    async def did_run(cfg, conn, **kw):
        ran.append(True)
        return []

    import blastjob.discovery.replies as rp

    monkeypatch.setattr(rp, "scan_replies", did_run)
    res = daily.DailyResult()
    import asyncio as _asyncio

    _asyncio.run(daily._replies_pass(cfg, conn, res))
    assert ran == [True]


def _fake_mail_credentials(monkeypatch, cfg):
    """`_replies_pass` gates on whether the personal mailbox has credentials, so
    a test that wants the scan to RUN must supply fake ones. Without this the
    test silently depends on the user's real ~/.claude/secrets.env holding live
    IMAP credentials -- it passes on his Mac and fails everywhere else, and it
    was one of four tests broken when the secrets-isolation fixture was briefly
    made suite-wide."""
    monkeypatch.setattr(daily.secrets_mod, "load_secrets_env", lambda *a, **k: None)
    monkeypatch.setenv(cfg.discovery.mail_user_env, "alex@example.com")
    monkeypatch.setenv(cfg.discovery.mail_password_env, "app-pw")


@pytest.mark.asyncio
async def test_run_daily_surfaces_replies_in_the_digest(db, monkeypatch):
    cfg, conn, ids = db
    cfg.discovery.mail_alerts_enabled = True
    _stub_pipeline(monkeypatch)
    _fake_mail_credentials(monkeypatch, cfg)

    import blastjob.discovery.replies as rp

    async def fake_scan(cfg, conn, **kw):
        return [_reply_match(job_id=ids["a"])]

    monkeypatch.setattr(rp, "scan_replies", fake_scan)
    sent: list[str] = []

    async def ok(text):
        sent.append(text)

    res = await daily.run_daily(cfg, conn, send=ok, today=dt.date(2026, 9, 8))
    assert len(res.replies) == 1
    assert sent  # a match alone (plus the fixture's pending jobs) still sends
    text = sent[0]
    assert "**Replies**" in text
    assert "Lakeshore Credit Union" in text
    assert "Sam Lee" in text
    assert "I have no feedback for anyone" in text
    assert f"cockpit <{daily.COCKPIT_BASE}/jobs/{ids['a']}>" in text
    # surface-only: never a draft, a suggested response, or a "reply now" nudge
    for banned in ("draft", "suggested repl", "reply now"):
        assert banned not in text.lower()


def test_build_digest_replies_section_has_no_draft_affordance():
    text = daily.build_digest([], {}, [], replies=[_reply_match()])
    assert "**Replies**" in text
    assert "Lakeshore Credit Union" in text
    assert "cockpit <http://127.0.0.1:8765/jobs/1>" in text
    for banned in ("draft", "suggested repl", "reply now", "send reply"):
        assert banned not in text.lower()


def test_build_digest_empty_with_no_replies_either():
    assert daily.build_digest([], {}, [], replies=[]) == ""
    assert daily.build_digest([], {}, [], replies=None) == ""


@pytest.mark.asyncio
async def test_run_daily_reply_scan_failure_is_isolated(db, monkeypatch):
    """An IMAP failure inside the reply scan records an error and the daily
    run still completes and still sends the rest of the digest -- same
    contract as `_closure_pass`."""
    cfg, conn, _ids = db
    cfg.discovery.mail_alerts_enabled = True
    _stub_pipeline(monkeypatch)
    _fake_mail_credentials(monkeypatch, cfg)

    import blastjob.discovery.replies as rp

    async def boom(cfg, conn, **kw):
        raise OSError("IMAP connection refused")

    monkeypatch.setattr(rp, "scan_replies", boom)
    sent: list[str] = []

    async def ok(text):
        sent.append(text)

    res = await daily.run_daily(cfg, conn, send=ok, today=dt.date(2026, 9, 8))
    assert res.errors.get("replies", "").startswith("OSError")
    assert res.replies == []
    assert sent  # the run still completes and still sends
    row = daily.last_run(conn)
    assert "replies" in json.loads(row["errors"])


# ---------- discord ----------


def test_credentials_missing_raises():
    with pytest.raises(discord.DiscordConfigError):
        discord.credentials({})
    assert discord.credentials({"DISCORD_BOT_TOKEN": "t", "DISCORD_CHANNEL_ID": "c"}) == ("t", "c")


def test_chunk_message_splits_on_lines_under_limit():
    lines = [f"line {i} " + "x" * 40 for i in range(100)]
    chunks = discord.chunk_message("\n".join(lines), limit=500)
    assert all(len(c) <= 500 for c in chunks)
    assert "\n".join(chunks).split("\n") == lines  # nothing lost or reordered
    assert discord.chunk_message("", limit=10) == []


@pytest.mark.asyncio
async def test_send_message_posts_each_chunk_with_bot_auth():
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append({"auth": req.headers.get("authorization"), "body": req.content})
        return httpx.Response(200, json={"id": str(len(seen))})

    text = "\n".join("y" * 100 for _ in range(30))  # 3000 chars -> 2 chunks at 1900
    ids = await discord.send_message(
        text, token="tok", channel_id="chan", transport=httpx.MockTransport(handler)
    )
    assert ids == ["1", "2"]
    assert all(s["auth"] == "Bot tok" for s in seen)
    assert b'"allowed_mentions"' in seen[0]["body"]


@pytest.mark.asyncio
async def test_send_message_raises_on_error_status():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Missing Access"})

    with pytest.raises(httpx.HTTPStatusError):
        await discord.send_message(
            "hi",
            token="t",
            channel_id="c",
            max_retries=1,
            transport=httpx.MockTransport(handler),
        )


def test_run_footer_shows_cost_known_and_unknown():
    base = {"duration_s": 60, "fetched": 3, "scored": 2, "scoring_batches": 2, "scoring_s": 30}
    known = daily.DailyResult(**base, scoring_cost=CallCost(cost_usd=0.1234))
    unknown = daily.DailyResult(**base, scoring_cost=CallCost(cost_usd=0.1, known=False))
    no_batches = daily.DailyResult(duration_s=60, fetched=3, scoring_cost=CallCost(cost_usd=0.1))

    assert "cost $0.1234" in daily.format_run_footer(known)
    assert "cost unknown" in daily.format_run_footer(unknown)
    assert "cost" not in daily.format_run_footer(no_batches)

"""Tests for the job-discovery persistence layer.

Guards the correctness-critical invariants from the Codex review:
- identity is (source, external_id) — no destructive collisions (#5)
- CAS build claim has exactly one winner (#3)
- expired build leases are recovered, never permanently stuck (#3)
- discovery upserts never clobber user review/build state (#4)
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile

import pytest

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    yield c
    c.close()


def test_migrations_create_all_tables(conn):
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jobs", "companies", "job_clusters", "build_runs", "schema_migrations"} <= tables


def test_upsert_same_identity_refreshes_not_duplicates(conn):
    a = dbc.upsert_job(conn, source="greenhouse", external_id="req-1", title="VP Ops", raw_jd="v1")
    b = dbc.upsert_job(conn, source="greenhouse", external_id="req-1", title="VP Ops", raw_jd="v2")
    assert a == b
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 1
    assert conn.execute("SELECT raw_jd FROM jobs WHERE id=?", (a,)).fetchone()["raw_jd"] == "v2"


def test_distinct_external_ids_are_distinct_rows(conn):
    """Two different reqs at the same company must not collide (Codex #5)."""
    a = dbc.upsert_job(conn, source="greenhouse", external_id="r1", company="Acme", title="Dir Ops")
    b = dbc.upsert_job(conn, source="greenhouse", external_id="r2", company="Acme", title="Dir Ops")
    assert a != b
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 2


def test_upsert_refresh_preserves_user_state(conn):
    """A rediscovery must not overwrite review_state / build_state (Codex #4)."""
    jid = dbc.upsert_job(conn, source="lever", external_id="x", title="VP")
    conn.execute("UPDATE jobs SET review_state='kept', build_state='tailored' WHERE id=?", (jid,))
    conn.commit()
    dbc.upsert_job(conn, source="lever", external_id="x", title="VP", raw_jd="refreshed")
    row = conn.execute(
        "SELECT review_state, build_state, raw_jd FROM jobs WHERE id=?", (jid,)
    ).fetchone()
    assert row["review_state"] == "kept"
    assert row["build_state"] == "tailored"
    assert row["raw_jd"] == "refreshed"


def test_cas_claim_has_single_winner(conn):
    jid = dbc.upsert_job(conn, source="s", external_id="e", title="t")
    assert dbc.claim_for_build(conn, jid, "run-A") is True
    assert dbc.claim_for_build(conn, jid, "run-B") is False


def test_recover_stale_build_unsticks_job(conn):
    jid = dbc.upsert_job(conn, source="s", external_id="e", title="t")
    dbc.claim_for_build(conn, jid, "run-A")
    conn.execute(
        "UPDATE build_runs SET lease_expires_at='2000-01-01T00:00:00' WHERE run_id='run-A'"
    )
    conn.commit()
    assert dbc.recover_stale_builds(conn) == 1
    bs = conn.execute("SELECT build_state FROM jobs WHERE id=?", (jid,)).fetchone()["build_state"]
    assert bs == "failed"
    # a failed job is re-claimable
    assert dbc.claim_for_build(conn, jid, "run-C") is True


def test_finish_build_marks_tailored(conn):
    jid = dbc.upsert_job(conn, source="s", external_id="e", title="t")
    dbc.claim_for_build(conn, jid, "run-A")
    dbc.finish_build(conn, "run-A", artifact_dir="/tmp/x", ok=True)
    bs = conn.execute("SELECT build_state FROM jobs WHERE id=?", (jid,)).fetchone()["build_state"]
    assert bs == "tailored"


# ---------- backup_db (Y2) ----------


def test_backup_db_produces_openable_copy_with_same_tables_and_row_count(conn, tmp_path):
    dbc.upsert_job(conn, source="s", external_id="e1", title="t1")
    dbc.upsert_job(conn, source="s", external_id="e2", title="t2")

    dest_dir = tmp_path / "backups"
    target = dbc.backup_db(conn, dest_dir)

    assert target.exists()
    assert target.parent == dest_dir

    table_sql = "SELECT name FROM sqlite_master WHERE type='table'"
    src_tables = {r["name"] for r in conn.execute(table_sql)}
    src_count = conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]

    backup_conn = sqlite3.connect(target)
    backup_conn.row_factory = sqlite3.Row
    try:
        dest_tables = {r["name"] for r in backup_conn.execute(table_sql)}
        dest_count = backup_conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
    finally:
        backup_conn.close()

    assert dest_tables == src_tables
    assert dest_count == src_count == 2


def test_backup_db_prunes_to_keep_newest_and_ignores_unrelated_paths(conn, tmp_path):
    dest_dir = tmp_path / "backups"
    dest_dir.mkdir()
    (dest_dir / "foo.db").write_text("not a backup")
    (dest_dir / "20200101-000000").mkdir()

    t1 = dbc.backup_db(conn, dest_dir, keep=2, now=dt.datetime(2026, 1, 1, 0, 0, 0))
    t2 = dbc.backup_db(conn, dest_dir, keep=2, now=dt.datetime(2026, 1, 2, 0, 0, 0))
    t3 = dbc.backup_db(conn, dest_dir, keep=2, now=dt.datetime(2026, 1, 3, 0, 0, 0))

    remaining = {p.name for p in dest_dir.glob("blastjob-*.db")}
    assert remaining == {t2.name, t3.name}
    assert not t1.exists()
    assert t2.exists() and t3.exists()

    # unrelated file and directory left untouched
    assert (dest_dir / "foo.db").exists()
    assert (dest_dir / "20200101-000000").is_dir()


def test_backup_db_creates_dest_dir(conn, tmp_path):
    dest_dir = tmp_path / "nested" / "backups"
    assert not dest_dir.exists()
    target = dbc.backup_db(conn, dest_dir)
    assert dest_dir.exists()
    assert target.exists()


# ----- a migration is recorded only when it fully applied --------------------


def test_a_half_applied_multi_statement_migration_is_not_recorded_as_done(monkeypatch, tmp_path):
    """`executescript` aborts at the failing statement, but the version used to
    be recorded regardless whenever the failure was a duplicate column. A
    migration that added two columns, half-applied by an earlier crash, would
    then be marked done with its second column never created — and every query
    naming that column fails forever.

    The duplicate-column tolerance exists for a real race (the cockpit and the
    daily job both migrating a fresh DB), so it stays; what changes is that a
    tolerated failure must leave the rest of the script applied.
    """
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    c = dbc.init_db(cfg)
    c.execute("CREATE TABLE half (a TEXT)")
    c.commit()

    # Statement 1 duplicates an existing column (the tolerated race); statement 2
    # is the one that must still land.
    monkeypatch.setattr(
        dbc,
        "MIGRATIONS",
        [(9_001, "ALTER TABLE half ADD COLUMN a TEXT; ALTER TABLE half ADD COLUMN b TEXT;")],
    )
    dbc.migrate(c)

    cols = {r["name"] for r in c.execute("PRAGMA table_info(half)")}
    recorded = {r["version"] for r in c.execute("SELECT version FROM schema_migrations")}
    assert "b" in cols, "the statements after a tolerated duplicate must still apply"
    assert 9_001 in recorded
    c.close()


def test_init_db_migrates_once_per_database_not_once_per_request(tmp_path, monkeypatch):
    """`web/app.py`'s `conn()` calls `init_db` on EVERY request, and most full
    pages call it twice (once more for the nav counts). Each call re-read the
    config, re-ran the migration loop, and re-issued an `ALTER TABLE
    build_runs ADD COLUMN owner_token` that raises and is caught every single
    time. The schema cannot change under a running process, so it is migrated
    once per database file and skipped thereafter.
    """
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)

    calls: list[int] = []
    real_migrate = dbc.migrate

    def counting_migrate(conn):
        calls.append(1)
        return real_migrate(conn)

    monkeypatch.setattr(dbc, "migrate", counting_migrate)
    dbc.reset_migration_memo()

    for _ in range(5):
        c = dbc.init_db(cfg)
        c.close()

    assert len(calls) == 1, f"migrate ran {len(calls)} times for one database"

    # A different database still gets migrated: the memo is per path, not global.
    other = BlastJobConfig()
    other.paths.data_dir = str(tmp_path / "second")
    dbc.init_db(other).close()
    assert len(calls) == 2

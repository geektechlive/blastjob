"""Migrations 59 and 60: the dream-list columns on jobs (#90)."""

from __future__ import annotations

import sqlite3

import pytest

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _forget(conn: sqlite3.Connection, version: int) -> None:
    conn.execute("DELETE FROM schema_migrations WHERE version>=?", (version,))
    conn.commit()


def test_dream_columns_exist_with_defaults(conn):
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(jobs)")}
    assert "dream_far" in cols
    assert "dream_notified_at" in cols
    conn.execute(
        "INSERT INTO jobs (source, external_id, first_seen, last_seen) "
        "VALUES ('s', 'x', '2026-10-01', '2026-10-01')"
    )
    row = conn.execute("SELECT dream_far, dream_notified_at FROM jobs").fetchone()
    assert row["dream_far"] == 0
    assert row["dream_notified_at"] == ""


def test_migrations_apply_to_a_database_at_version_58(tmp_path):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    c = dbc.init_db(cfg)
    c.execute(
        "INSERT INTO jobs (source, external_id, first_seen, last_seen, company) "
        "VALUES ('s', 'old', '2026-09-01', '2026-09-01', 'Acme')"
    )
    c.commit()
    # Rebuild the jobs table as it was at version 58 (no dream columns), then migrate.
    c.execute("ALTER TABLE jobs DROP COLUMN dream_far")
    c.execute("ALTER TABLE jobs DROP COLUMN dream_notified_at")
    _forget(c, 59)
    assert "dream_far" not in {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
    dbc.migrate(c)
    row = c.execute(
        "SELECT dream_far, dream_notified_at FROM jobs WHERE external_id='old'"
    ).fetchone()
    assert (row["dream_far"], row["dream_notified_at"]) == (0, "")
    versions = {r["version"] for r in c.execute("SELECT version FROM schema_migrations")}
    assert {59, 60} <= versions
    c.close()

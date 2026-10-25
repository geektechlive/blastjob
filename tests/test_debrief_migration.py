"""Migration 61: the interview_debriefs table (#84)."""

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


def test_the_table_exists_with_no_transcript_or_filename_column(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(interview_debriefs)")}
    assert {"job_id", "ingest_id", "owner_label", "longest_json", "numbers_json"} <= cols
    assert not cols & {"source_name", "filename", "transcript", "text", "body"}


def test_ingest_id_is_unique(conn):
    sql = (
        "INSERT INTO interview_debriefs (job_id, ingest_id, created_at, owner_label) "
        "VALUES (1, 'abc', '2026-10-25', 'Jordan Example')"
    )
    conn.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)


def test_the_migration_applies_to_a_database_at_version_60(tmp_path):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    c = dbc.init_db(cfg)
    c.execute("DROP TABLE interview_debriefs")
    c.execute("DELETE FROM schema_migrations WHERE version>=61")
    c.commit()
    dbc.migrate(c)
    versions = {r["version"] for r in c.execute("SELECT version FROM schema_migrations")}
    assert 61 in versions
    c.execute("SELECT * FROM interview_debriefs").fetchall()
    c.close()

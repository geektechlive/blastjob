"""Migration 54: company exclusion keys are re-keyed to company_key() (#35)."""

from __future__ import annotations

import logging
import sqlite3

import pytest

from blastjob.core.company import company_key
from blastjob.db import client as dbc
from blastjob.matching.pipeline import auto_archive_rejected_companies
from blastjob.models.config import BlastJobConfig
from tests.geo_fixture import apply_region
from tests.profile_fixture import apply_profile


@pytest.fixture
def cfg(tmp_path) -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = str(tmp_path)
    apply_region(c)
    apply_profile(c)
    (tmp_path / "work_history.md").write_text("# History\n", encoding="utf-8")
    return c


@pytest.fixture
def conn(cfg) -> sqlite3.Connection:
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _rerun_migration(conn: sqlite3.Connection, version: int) -> None:
    # migrate() resumes after the highest recorded version, so forget it and every
    # later one; a later ADD COLUMN that already exists is tolerated.
    conn.execute("DELETE FROM schema_migrations WHERE version>=?", (version,))
    conn.commit()
    dbc.migrate(conn)


def _exclude(conn, key: str, company: str, reason: str = "") -> None:
    conn.execute(
        "INSERT INTO company_exclusions (company_key, company, reason, created_at) "
        "VALUES (?,?,?,?)",
        (key, company, reason, "2026-01-01"),
    )
    conn.commit()


def test_lowercase_keyed_exclusion_matches_after_migration_54(conn):
    name = "Acme Corp, Inc."
    _exclude(conn, name.lower(), name, "migrated from archive_reason")
    assert name.lower() != company_key(name)
    conn.execute(
        "INSERT INTO jobs (source, external_id, company, title, review_state, first_seen, "
        "last_seen) VALUES ('s', 'x1', ?, 'CIO', 'new', '2026-10-01', '2026-10-01')",
        (name,),
    )
    conn.commit()
    assert auto_archive_rejected_companies(conn) == 0  # lapsed: the stored key is wrong

    _rerun_migration(conn, 54)

    keys = [r["company_key"] for r in conn.execute("SELECT company_key FROM company_exclusions")]
    assert keys == [company_key(name)]
    assert auto_archive_rejected_companies(conn) == 1


def test_migration_54_collapsing_keys_keeps_both_rows_and_logs(conn, caplog):
    _exclude(conn, "acme corp, inc.", "Acme Corp, Inc.", "old")
    _exclude(conn, "acme", "Acme", "already right")

    with caplog.at_level(logging.WARNING):
        _rerun_migration(conn, 54)  # must not raise

    rows = {r["company_key"]: r["reason"] for r in conn.execute("SELECT * FROM company_exclusions")}
    assert rows["acme"] == "already right"
    assert len(rows) == 2  # the colliding row keeps its old key, nothing is deleted
    assert any("company_exclusions" in m for m in caplog.messages)

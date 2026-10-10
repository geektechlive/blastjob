"""`blastjob-daily --duplicate-applications`: two applications to what looks like
the same role (same company key + normalized title), clustered or not. Read-only."""

from __future__ import annotations

import argparse
import tempfile

import pytest

from blastjob import daily_tools
from blastjob.core import duplicate_applications as dup
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig


@pytest.fixture
def cfg() -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(conn, ext: str, company: str, title: str, **cols) -> int:
    jid = dbc.upsert_job(conn, source="adzuna", external_id=ext, company=company, title=title)
    cols.setdefault("review_state", "kept")
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), jid))
    conn.commit()
    return jid


def test_pairs_two_applications_to_the_same_role_across_name_variants(conn):
    a = _job(
        conn,
        "1",
        "Northwind Services Inc",
        "Vice President of Technology",
        application_state="applied",
        applied_at="2026-09-13",
    )
    b = _job(conn, "2", "Northwind Services", "VP Technology", application_state="rejected")
    _job(conn, "3", "Northwind Services", "VP Technology", application_state="drafted")
    _job(conn, "4", "Northwind Services", "VP Technology")
    _job(
        conn,
        "5",
        "Northwind Services",
        "VP Technology",
        review_state="archived",
        application_state="applied",
    )
    _job(conn, "6", "Northwind Services", "Director of IT", application_state="applied")

    pairs = dup.duplicate_applications(conn)

    assert [(int(x["id"]), int(y["id"])) for x, y in pairs] == [(a, b)]


def test_report_prints_pairs_and_changes_nothing(cfg, conn):
    _job(conn, "1", "Acme", "CIO", application_state="applied", applied_at="2026-09-01")
    _job(conn, "2", "Acme", "CIO", application_state="screen")
    before = [tuple(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id")]
    ap = argparse.ArgumentParser()
    daily_tools.add_arguments(ap)

    text = daily_tools.dispatch(ap.parse_args(["--duplicate-applications"]), cfg, conn)

    assert text is not None and "1 pair" in text and "Acme" in text
    assert [tuple(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id")] == before


def test_report_says_so_when_there_are_none(conn):
    assert dup.format_pairs([]) == "no duplicate applications"

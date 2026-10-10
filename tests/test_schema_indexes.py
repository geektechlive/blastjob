"""Migration 41: the kept-stage expression index.

`web/stages.py`'s SHORTLIST/APPLIED/CLOSED predicates filter on
`review_state='kept' AND COALESCE(application_state,'') IN (...)`.
`application_state` carries no NOT NULL constraint, so a plain index on the
column cannot serve a query built around the COALESCE expression -- this
locks in that SQLite actually uses the expression index once it exists,
rather than falling back to a full scan of the kept rows.
"""

from __future__ import annotations

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


def test_kept_appstate_index_exists(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='jobs'"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert "idx_jobs_kept_appstate" in names


@pytest.mark.parametrize(
    "in_list",
    [
        "'','drafted'",
        "'applied','screen','interview','offer'",
        "'rejected','ghosted','withdrawn','posting_closed','filled'",
    ],
)
def test_kept_stage_queries_use_the_expression_index(conn, in_list):
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM jobs WHERE review_state='kept' "
        f"AND COALESCE(application_state,'') IN ({in_list})"
    ).fetchall()
    detail = " ".join(row["detail"] for row in plan)
    assert "idx_jobs_kept_appstate" in detail

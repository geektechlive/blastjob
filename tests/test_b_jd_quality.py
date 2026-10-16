"""Group B: what a rediscovery refresh may and may not overwrite (B2, B3, B7).

JD quality classes: stub (empty) < snippet (< MIN_FULL_JD_CHARS) < full < pasted.
A refresh may replace a stored JD with one of the same or a higher class, never a
lower one; full replaces full (a legitimate revision).
"""

from __future__ import annotations

import tempfile

import pytest

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig

SNIPPET = "Short teaser for the role. " * 5
FULL_A = "Full description, version A. " * 40
FULL_B = "Full description, revised B. " * 40


@pytest.fixture
def conn():
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(c)
    yield conn
    conn.close()


def _row(conn, job_id):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def _seed(conn, jd, **extra):
    return dbc.upsert_job(
        conn, source="adzuna", external_id="a1", company="Acme", title="VP IT", raw_jd=jd, **extra
    )


def _score(conn, job_id, state="surfaced"):
    conn.execute(
        "UPDATE jobs SET alignment_score=88, rationale='r', match_strengths='s', "
        "match_gaps='g', review_state=? WHERE id=?",
        (state, job_id),
    )
    conn.commit()


def _refresh(conn, jd, **kw):
    return dbc.upsert_job(
        conn, source="adzuna", external_id="a1", company="Acme", title="VP IT", raw_jd=jd, **kw
    )


# ---- B2 ----------------------------------------------------------------------


def test_snippet_never_replaces_full_jd(conn):
    jid = _seed(conn, FULL_A)
    _refresh(conn, SNIPPET)
    assert _row(conn, jid)["raw_jd"] == FULL_A


def test_full_replaces_snippet(conn):
    jid = _seed(conn, SNIPPET)
    _refresh(conn, FULL_A)
    assert _row(conn, jid)["raw_jd"] == FULL_A


def test_full_replaces_full_even_when_shorter(conn):
    longer = FULL_A * 2
    jid = _seed(conn, longer)
    _refresh(conn, FULL_B)
    assert _row(conn, jid)["raw_jd"] == FULL_B


def test_shorter_snippet_replaces_snippet(conn):
    jid = _seed(conn, SNIPPET)
    _refresh(conn, "tiny")
    assert _row(conn, jid)["raw_jd"] == "tiny"


def test_pasted_jd_survives_full_refresh(conn):
    jid = _seed(conn, SNIPPET)
    dbc.set_pasted_jd(conn, jid, "pasted text")
    _refresh(conn, FULL_A)
    assert _row(conn, jid)["raw_jd"] == "pasted text"


# ---- B7 ----------------------------------------------------------------------


@pytest.mark.parametrize("state", ["new", "surfaced", "queued"])
def test_changed_jd_clears_undecided_score(conn, state):
    jid = _seed(conn, SNIPPET)
    _score(conn, jid, state)
    _refresh(conn, FULL_A)
    row = _row(conn, jid)
    assert row["alignment_score"] is None
    assert (row["rationale"], row["match_strengths"], row["match_gaps"]) == ("", "", "")


def test_unchanged_jd_keeps_score(conn):
    jid = _seed(conn, FULL_A)
    _score(conn, jid)
    _refresh(conn, FULL_A + "  \n")  # whitespace only is no change
    assert _row(conn, jid)["alignment_score"] == 88


def test_dropped_lower_class_jd_keeps_score(conn):
    jid = _seed(conn, FULL_A)
    _score(conn, jid)
    _refresh(conn, SNIPPET)
    assert _row(conn, jid)["alignment_score"] == 88


def test_kept_row_keeps_score_when_jd_changes(conn):
    jid = _seed(conn, SNIPPET)
    _score(conn, jid, "kept")
    _refresh(conn, FULL_A)
    row = _row(conn, jid)
    assert row["raw_jd"] == FULL_A
    assert row["alignment_score"] == 88


def test_presence_stub_without_jd_keeps_score(conn):
    jid = _seed(conn, FULL_A)
    _score(conn, jid)
    dbc.upsert_job(conn, source="adzuna", external_id="a1", company="Acme", title="VP IT")
    row = _row(conn, jid)
    assert row["raw_jd"] == FULL_A and row["alignment_score"] == 88


# ---- B3 ----------------------------------------------------------------------


def test_empty_company_never_overwrites_repaired_company(conn):
    jid = _seed(conn, FULL_A)
    dbc.upsert_job(conn, source="adzuna", external_id="a1", company="", title="VP IT")
    assert _row(conn, jid)["company"] == "Acme"


def test_nonempty_company_still_refreshes(conn):
    jid = _seed(conn, FULL_A)
    dbc.upsert_job(conn, source="adzuna", external_id="a1", company="Acme Corp", title="VP IT")
    assert _row(conn, jid)["company"] == "Acme Corp"


def test_whole_card_refresh_does_not_touch_title(conn):
    jid = _seed(conn, FULL_A)
    dbc.upsert_job(
        conn,
        source="adzuna",
        external_id="a1",
        company="Acme",
        title="VP IT Acme Chicago, IL Easy Apply",
        refresh_title=False,
    )
    assert _row(conn, jid)["title"] == "VP IT"


def test_whole_card_first_sight_still_inserts_title(conn):
    jid = dbc.upsert_job(
        conn,
        source="adzuna",
        external_id="w1",
        company="",
        title="VP IT Acme Chicago",
        refresh_title=False,
    )
    assert _row(conn, jid)["title"] == "VP IT Acme Chicago"

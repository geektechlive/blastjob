"""Employer-JD provenance in the DB layer (issue #17, unit U1)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from blastjob.db import client as dbc

NOW = "2026-10-16T08:00:00"
SNIPPET = "Short Adzuna snippet for the role. " * 5
AGG_FULL = "Aggregator full description text. " * 40
EMPLOYER = "Employer site description with the real duties. " * 30
EMPLOYER_V2 = "Employer site revised description, new duties. " * 30
SRC = "employer:greenhouse"


@pytest.fixture
def db(tmp_path: Path):
    conn = dbc.init_db_at(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _job(conn, raw_jd=SNIPPET, review_state="surfaced", ext="1", **extra) -> int:
    return dbc.upsert_job(
        conn,
        source="adzuna",
        external_id=ext,
        company="Acme",
        title="VP IT",
        raw_jd=raw_jd,
        review_state=review_state,
        **extra,
    )


def _row(conn, job_id):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def _adopt(conn, job_id, text=EMPLOYER, ref="g1", src=SRC):
    snap = dbc.jd_snapshot(conn, job_id)
    assert snap is not None
    return dbc.set_employer_jd(conn, snap, text=text, jd_source=src, jd_source_ref=ref, now=NOW)


def test_migration_adds_the_eight_columns(db):
    cols = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
    assert {
        "jd_source",
        "jd_source_ref",
        "jd_fetched_at",
        "jd_prior",
        "jd_lookup_at",
        "jd_lookup_status",
        "jd_lookup_failures",
        "score_stale",
    } <= cols


def test_jd_class_orders_employer_between_full_and_pasted():
    assert dbc.JD_FULL < dbc.JD_EMPLOYER < dbc.JD_PASTED
    assert dbc.jd_class(EMPLOYER, None, SRC) == dbc.JD_EMPLOYER
    assert dbc.jd_class(EMPLOYER, "2026-01-01", SRC) == dbc.JD_PASTED
    assert dbc.jd_class(EMPLOYER) == dbc.JD_FULL


def test_aggregator_full_refresh_never_replaces_employer_jd_and_lands_in_prior(db):
    jid = _job(db)
    assert _adopt(db, jid)
    db.commit()
    _job(db, raw_jd=AGG_FULL)
    row = _row(db, jid)
    assert row["raw_jd"] == EMPLOYER.strip()
    assert row["jd_prior"] == AGG_FULL
    assert row["jd_source"] == SRC


def test_snippet_refresh_never_replaces_employer_jd(db):
    jid = _job(db)
    _adopt(db, jid)
    db.commit()
    _job(db, raw_jd="tiny snippet")
    row = _row(db, jid)
    assert row["raw_jd"] == EMPLOYER.strip()
    assert row["jd_prior"] == "tiny snippet"


def test_blank_refresh_does_not_wipe_jd_prior(db):
    jid = _job(db)
    _adopt(db, jid)
    db.commit()
    _job(db, raw_jd="")
    assert _row(db, jid)["jd_prior"] == SNIPPET


def test_paste_beats_employer_text_and_clears_provenance(db):
    jid = _job(db)
    _adopt(db, jid)
    db.commit()
    assert dbc.set_pasted_jd(db, jid, "My pasted full description. " * 30)
    row = _row(db, jid)
    assert row["raw_jd"].startswith("My pasted")
    assert (row["jd_source"], row["jd_source_ref"], row["jd_fetched_at"], row["jd_prior"]) == (
        "",
        "",
        "",
        "",
    )


def test_paste_of_identical_text_also_clears_provenance(db):
    jid = _job(db)
    _adopt(db, jid)
    db.commit()
    assert dbc.set_pasted_jd(db, jid, EMPLOYER)
    row = _row(db, jid)
    assert row["jd_source"] == "" and row["jd_pasted_at"]


def test_set_employer_jd_loses_to_concurrent_paste(db):
    jid = _job(db)
    snap = dbc.jd_snapshot(db, jid)
    dbc.set_pasted_jd(db, jid, "Pasted while we were fetching. " * 30)
    ok = dbc.set_employer_jd(db, snap, text=EMPLOYER, jd_source=SRC, jd_source_ref="g1", now=NOW)
    assert ok is False
    assert _row(db, jid)["raw_jd"].startswith("Pasted")


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE jobs SET company='Other Co' WHERE id=?",
        "UPDATE jobs SET title='Director IT' WHERE id=?",
        "UPDATE jobs SET review_state='archived' WHERE id=?",
        "UPDATE jobs SET source_state='stale' WHERE id=?",
        "UPDATE jobs SET employer_link_source='workday' WHERE id=?",
        "UPDATE jobs SET employer_link_external_id='zzz' WHERE id=?",
        "UPDATE jobs SET raw_jd='changed underneath' WHERE id=?",
    ],
)
def test_set_employer_jd_loses_race_on_each_snapshot_field(db, sql):
    jid = _job(db)
    snap = dbc.jd_snapshot(db, jid)
    db.execute(sql, (jid,))
    before = dict(_row(db, jid))
    ok = dbc.set_employer_jd(db, snap, text=EMPLOYER, jd_source=SRC, jd_source_ref="g1", now=NOW)
    assert ok is False
    assert dict(_row(db, jid)) == before


def test_set_employer_jd_refuses_when_other_source_already_adopted(db):
    jid = _job(db)
    _adopt(db, jid)
    snap = dbc.jd_snapshot(db, jid)
    ok = dbc.set_employer_jd(
        db, snap, text=EMPLOYER_V2, jd_source="employer:lever", jd_source_ref="x", now=NOW
    )
    assert ok is False


def test_short_text_after_normalization_is_refused(db):
    jid = _job(db)
    snap = dbc.jd_snapshot(db, jid)
    html = "<p>" + ("x" * 700) + "</p>"
    assert dbc.normalize_employer_jd("<div>tiny</div>") is None
    # tags stripped: 700 chars of padding inside markup still passes, markup alone does not
    assert dbc.normalize_employer_jd(html) == "x" * 700
    markup_only = "<p>" + "<b></b>" * 200 + "short</p>"
    assert dbc.normalize_employer_jd(markup_only) is None
    assert not dbc.set_employer_jd(
        db, snap, text="short", jd_source=SRC, jd_source_ref="g1", now=NOW
    )
    assert _row(db, jid)["jd_source"] == ""


def test_html_is_normalized_on_adoption(db):
    jid = _job(db)
    assert _adopt(db, jid, text="<p>" + "Real duties here. " * 60 + "</p>")
    raw = _row(db, jid)["raw_jd"]
    assert "<p>" not in raw and raw.startswith("Real duties")


def test_text_over_cap_is_truncated():
    out = dbc.normalize_employer_jd("a" * (dbc.MAX_PASTED_JD_CHARS + 5000))
    assert out is not None and len(out) == dbc.MAX_PASTED_JD_CHARS


def test_undecided_score_cleared_and_not_stale(db):
    jid = _job(db)
    db.execute("UPDATE jobs SET alignment_score=88, rationale='r' WHERE id=?", (jid,))
    assert _adopt(db, jid)
    row = _row(db, jid)
    assert row["alignment_score"] is None and row["rationale"] == ""
    assert row["score_stale"] == 0
    assert row["jd_source"] == SRC and row["jd_source_ref"] == "g1"
    assert row["jd_fetched_at"] == NOW and row["jd_prior"] == SNIPPET


def test_kept_score_kept_and_flagged_stale(db):
    jid = _job(db, review_state="kept")
    db.execute("UPDATE jobs SET alignment_score=88 WHERE id=?", (jid,))
    assert _adopt(db, jid)
    row = _row(db, jid)
    assert row["alignment_score"] == 88 and row["score_stale"] == 1


def test_revision_keeps_original_jd_prior(db):
    jid = _job(db)
    _adopt(db, jid)
    assert _adopt(db, jid, text=EMPLOYER_V2)
    row = _row(db, jid)
    assert row["raw_jd"] == EMPLOYER_V2.strip()
    assert row["jd_prior"] == SNIPPET


def test_revert_restores_prior_and_clears_provenance(db):
    jid = _job(db)
    _adopt(db, jid)
    db.execute("UPDATE jobs SET alignment_score=70 WHERE id=?", (jid,))
    assert dbc.revert_employer_jd(db, jid)
    row = _row(db, jid)
    assert row["raw_jd"] == SNIPPET
    assert (row["jd_source"], row["jd_source_ref"], row["jd_fetched_at"], row["jd_prior"]) == (
        "",
        "",
        "",
        "",
    )
    assert row["alignment_score"] is None


def test_revert_on_kept_row_flags_stale(db):
    jid = _job(db, review_state="kept")
    _adopt(db, jid)
    db.execute("UPDATE jobs SET score_stale=0 WHERE id=?", (jid,))
    assert dbc.revert_employer_jd(db, jid)
    assert _row(db, jid)["score_stale"] == 1


def test_revert_honours_ref(db):
    jid = _job(db)
    _adopt(db, jid)
    assert not dbc.revert_employer_jd(db, jid, ref=(SRC, "other"))
    assert _row(db, jid)["raw_jd"] == EMPLOYER.strip()
    assert dbc.revert_employer_jd(db, jid, ref=(SRC, "g1"))
    assert _row(db, jid)["raw_jd"] == SNIPPET


def test_revert_never_touches_a_paste_or_plain_row(db):
    jid = _job(db)
    assert not dbc.revert_employer_jd(db, jid)
    dbc.set_pasted_jd(db, jid, "Pasted text. " * 60)
    assert not dbc.revert_employer_jd(db, jid)
    assert _row(db, jid)["raw_jd"].startswith("Pasted")


def test_functions_never_commit(tmp_path: Path):
    path = tmp_path / "c.db"
    a = dbc.init_db_at(path)
    jid = _job(a)
    a.row_factory = sqlite3.Row
    snap = dbc.jd_snapshot(a, jid)
    assert dbc.set_employer_jd(a, snap, text=EMPLOYER, jd_source=SRC, jd_source_ref="g", now=NOW)
    dbc.record_jd_lookup(a, jid, "found", NOW)
    assert a.in_transaction
    b = sqlite3.connect(path)
    assert b.execute("SELECT jd_source FROM jobs WHERE id=?", (jid,)).fetchone()[0] == ""
    b.close()
    a.commit()
    assert dbc.revert_employer_jd(a, jid)
    assert a.in_transaction
    b = sqlite3.connect(path)
    assert b.execute("SELECT jd_source FROM jobs WHERE id=?", (jid,)).fetchone()[0] == SRC
    b.close()
    a.rollback()
    a.close()


def test_snapshot_of_missing_job_is_none(db):
    assert dbc.jd_snapshot(db, 9999) is None


def test_lookup_backoff_schedule(db):
    jid = _job(db)
    row0 = _row(db, jid)
    assert dbc.jd_lookup_due(row0, NOW)  # never looked up

    dbc.record_jd_lookup(db, jid, "found", NOW)
    assert not dbc.jd_lookup_due(_row(db, jid), "2030-01-01T00:00:00")

    dbc.record_jd_lookup(db, jid, "no_match", NOW)
    row = _row(db, jid)
    assert row["jd_lookup_failures"] == 0
    assert not dbc.jd_lookup_due(row, "2026-10-29T07:59:00")
    assert dbc.jd_lookup_due(row, "2026-10-30T08:00:00")

    expected_days = [1, 2, 4, 8, 14, 14]
    for failures, days in enumerate(expected_days, start=1):
        dbc.record_jd_lookup(db, jid, "transient", NOW)
        row = _row(db, jid)
        assert row["jd_lookup_failures"] == failures
        assert row["jd_lookup_status"] == "transient"
        just_before = f"2026-10-{16 + days - 1:02d}T23:59:00" if days < 15 else ""
        assert not dbc.jd_lookup_due(row, just_before)
        assert dbc.jd_lookup_due(row, f"2026-10-{16 + days:02d}T08:00:00")

    dbc.record_jd_lookup(db, jid, "found", NOW)
    assert _row(db, jid)["jd_lookup_failures"] == 0

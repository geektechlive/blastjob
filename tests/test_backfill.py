"""`blastjob-daily --backfill-artifacts[-report]` (`core/backfill.py`).

Report mode writes nothing. Apply mode registers each build's own files and the
as-sent documents, reconstructs historical application events, is idempotent,
and lists missing files in their own section instead of stopping.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from blastjob.core import artifacts, backfill, events
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig


@pytest.fixture
def cfg() -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(conn, ext: str, title: str, **cols) -> int:
    jid = dbc.upsert_job(conn, source="s1", external_id=ext, company="Acme", title=title)
    cols = {"review_state": "kept", **cols}
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), jid))
    conn.commit()
    return jid


def _role_files(folder: Path, slug: str, role: str, resume: str) -> None:
    (folder / f"{slug}.metadata.json").write_text(
        json.dumps({"company": "Acme", "role": role}), encoding="utf-8"
    )
    (folder / f"{slug}.fit_score.md").write_text("x", encoding="utf-8")
    (folder / resume).write_text("x", encoding="utf-8")


@pytest.fixture
def world(conn, cfg, tmp_path):
    folder = Path(cfg.paths.output_dir) / "acme-2026-09-01"
    folder.mkdir()
    a = _job(conn, "a", "VP IT", application_state="applied", applied_at="2026-09-02")
    b = _job(
        conn,
        "b",
        "Director of Data",
        application_state="rejected",
        applied_at="2026-09-03",
        stage_changed_at="2026-09-25T10:00:00",
    )
    _role_files(folder, "vp-it", "VP IT", "Alex_Rivera_Resume_-_Acme_VP_IT.md")
    _role_files(
        folder,
        "director-of-data",
        "Director of Data",
        "Alex_Rivera_Resume_-_Acme_Director_of_Data.md",
    )
    for jid, run in ((a, "run-a"), (b, "run-b")):
        dbc.claim_for_build(conn, jid, run)
        dbc.finish_build(conn, run, artifact_dir=str(folder), ok=True)
    gone = _job(conn, "c", "CIO")
    dbc.claim_for_build(conn, gone, "run-c")
    dbc.finish_build(conn, "run-c", artifact_dir=str(tmp_path / "moved-away"), ok=True)
    sent = tmp_path / "sent-resume.docx"
    sent.write_bytes(b"docx")
    conn.execute(
        "INSERT INTO packet_sends (job_id, resume_path, resume_sha256, cover_letter_path, sent_at) "
        "VALUES (?, ?, 'h', ?, '2026-09-02')",
        (a, str(sent), str(tmp_path / "deleted-cover.docx")),
    )
    conn.commit()
    return {"a": a, "b": b, "gone": gone, "folder": folder}


def test_report_mode_writes_nothing(conn, cfg, world):
    before = conn.total_changes
    out = backfill.run(conn, apply=False)
    assert conn.total_changes == before
    assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM application_events").fetchone()[0] == 0
    assert "would register" in out.lower()
    assert "missing" in out.lower()


def test_apply_registers_each_runs_own_files_and_the_sent_documents(conn, world):
    backfill.run(conn, apply=True)
    a_names = {Path(r["path"]).name for r in artifacts.artifacts_for(conn, world["a"])}
    assert a_names == {
        "vp-it.metadata.json",
        "vp-it.fit_score.md",
        "Alex_Rivera_Resume_-_Acme_VP_IT.md",
        "sent-resume.docx",
    }
    b_names = {Path(r["path"]).name for r in artifacts.artifacts_for(conn, world["b"])}
    assert "Alex_Rivera_Resume_-_Acme_Director_of_Data.md" in b_names
    assert not b_names & a_names


def test_apply_reports_missing_files_separately_and_continues(conn, world):
    out = backfill.run(conn, apply=True)
    missing = out.lower().split("missing", 1)[1]
    assert "moved-away" in missing
    assert "deleted-cover.docx" in missing
    assert artifacts.artifacts_for(conn, world["b"])  # later runs still registered


def test_apply_reconstructs_events_and_is_idempotent(conn, world):
    backfill.run(conn, apply=True)
    backfill.run(conn, apply=True)
    a = [(e["kind"], e["at"], e["source"]) for e in events.events_for(conn, world["a"])]
    assert a == [("applied", "2026-09-02", "backfill")]
    b = [(e["kind"], e["at"], e["source"]) for e in events.events_for(conn, world["b"])]
    assert b == [
        ("rejected", "2026-09-25T10:00:00", "backfill"),
        ("applied", "2026-09-03", "backfill"),
    ]
    assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 7


def test_events_backfill_skips_drafted_and_falls_back_on_dates(conn):
    drafted = _job(conn, "d", "VP", application_state="drafted")
    closed = _job(
        conn,
        "e",
        "CTO",
        application_state="posting_closed",
        posting_closed_at="2026-09-10T07:30:00",
    )
    undated = _job(conn, "f", "CISO", application_state="ghosted")
    out = backfill.run(conn, apply=True)
    assert events.events_for(conn, drafted) == []
    assert [e["at"] for e in events.events_for(conn, closed)] == ["2026-09-10T07:30:00"]
    assert events.events_for(conn, undated) == []
    assert f"#{undated}" in out


def test_existing_state_events_are_not_duplicated(conn):
    jid = _job(conn, "g", "VP", application_state="screen", stage_changed_at="2026-10-01T09:00:00")
    events.add_event(conn, jid, "screen", "2026-10-01T09:00:00", source="state")
    backfill.run(conn, apply=True)
    assert [e["kind"] for e in events.events_for(conn, jid)] == ["screen"]


def test_a_run_whose_role_no_longer_matches_is_listed_not_silently_skipped(conn, world):
    """A title edited after the build (e.g. the LinkedIn title repair) leaves
    the run's files unattributable by naming; the report must say so."""
    conn.execute("UPDATE jobs SET title='Vice President of IT' WHERE id=?", (world["a"],))
    conn.commit()
    out = backfill.run(conn, apply=False)
    assert "no files matched" in out.lower()
    unmatched = out.lower().split("no files matched", 1)[1]
    assert f"job #{world['a']}" in unmatched
    assert f"job #{world['b']}" not in unmatched

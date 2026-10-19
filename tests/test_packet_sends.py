"""As-sent attribution (core/packet_sends.py): immutable send snapshots plus
an append-only outcome log.

The packet_sends / packet_outcomes tables are reserved as migrations 39/40 in
db/schema.py (delivered as an integration patch, not owned by this module),
so these tests create them directly on a temp DB's connection -- same SQL as
the patch -- rather than depending on schema.py being updated yet.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from blastjob.core import packet_sends as ps
from blastjob.core.packet_report import Artifact, Finding, Severity
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig

# Mirrors the migration 39/40 SQL in this workstream's INTEGRATION PATCH.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS packet_sends (
    id                        INTEGER PRIMARY KEY,
    job_id                    INTEGER NOT NULL REFERENCES jobs(id),
    artifact_dir              TEXT DEFAULT '',
    resume_path               TEXT DEFAULT '',
    resume_sha256             TEXT NOT NULL,
    cover_letter_path         TEXT DEFAULT '',
    cover_letter_sha256       TEXT DEFAULT '',
    jd_sha256                 TEXT DEFAULT '',
    prompt_version            TEXT DEFAULT '',
    model                     TEXT DEFAULT '',
    groundedness               INTEGER,
    jd_phrasing                INTEGER,
    cover_letter_groundedness  INTEGER,
    fatal_findings_json        TEXT DEFAULT '[]',
    sent_at                    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_packet_sends_job ON packet_sends(job_id);

CREATE TABLE IF NOT EXISTS packet_outcomes (
    id          INTEGER PRIMARY KEY,
    send_id     INTEGER NOT NULL REFERENCES packet_sends(id),
    outcome     TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    note        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_packet_outcomes_send ON packet_outcomes(send_id);
"""


@pytest.fixture
def cfg():
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    return c


@pytest.fixture
def conn(cfg) -> sqlite3.Connection:
    conn = dbc.init_db(cfg)
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    yield conn
    conn.close()


def _job(conn: sqlite3.Connection, *, ext: str = "r1", raw_jd: str = "job description text") -> int:
    job_id = dbc.upsert_job(
        conn, source="workday:acme", external_id=ext, company="Acme", title="VP of Ops"
    )
    conn.execute("UPDATE jobs SET raw_jd=? WHERE id=?", (raw_jd, job_id))
    conn.commit()
    return job_id


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_record_send_hashes_resume_and_jd(conn, tmp_path):
    job_id = _job(conn, raw_jd="tailored posting text")
    resume = _write(tmp_path, "resume.md", "Alex Rivera resume v1")

    send_id = ps.record_send(conn, job_id, str(tmp_path), resume)

    row = conn.execute("SELECT * FROM packet_sends WHERE id=?", (send_id,)).fetchone()
    assert row["job_id"] == job_id
    assert row["resume_sha256"] == ps._sha256_file(resume)
    assert row["jd_sha256"] == ps._sha256_bytes(b"tailored posting text")
    assert row["cover_letter_sha256"] == ""
    assert row["sent_at"]


def test_record_send_stores_scores_and_fatal_findings(conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "content")
    finding = Finding(
        severity=Severity.FATAL,
        code="empty_recent_role",
        message="Contoso Health Analytics 2017-2020 has no bullets",
        artifact=Artifact.RESUME,
        source="sent packet 2026-09-10",
    )

    send_id = ps.record_send(
        conn,
        job_id,
        str(tmp_path),
        resume,
        prompt_version="resume-v3",
        model="claude-cli",
        groundedness=92,
        jd_phrasing=61,
        fatal_findings=[finding],
    )

    row = conn.execute("SELECT * FROM packet_sends WHERE id=?", (send_id,)).fetchone()
    assert row["prompt_version"] == "resume-v3"
    assert row["model"] == "claude-cli"
    assert row["groundedness"] == 92
    assert row["jd_phrasing"] == 61
    stored_findings = json.loads(row["fatal_findings_json"])
    assert stored_findings == [
        {
            "severity": "fatal",
            "code": "empty_recent_role",
            "message": "Contoso Health Analytics 2017-2020 has no bullets",
            "artifact": "resume",
            "line": None,
            "evidence": "",
            "source": "sent packet 2026-09-10",
        }
    ]


def test_marking_applied_twice_is_one_row(conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "content")

    first = ps.record_send(conn, job_id, str(tmp_path), resume)
    second = ps.record_send(conn, job_id, str(tmp_path), resume)

    assert first == second
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM packet_sends WHERE job_id=?", (job_id,)
    ).fetchone()
    assert count["n"] == 1


def test_rebuild_after_send_does_not_change_recorded_hash(conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "original content")
    send_id = ps.record_send(conn, job_id, str(tmp_path), resume)
    original_hash = conn.execute(
        "SELECT resume_sha256 FROM packet_sends WHERE id=?", (send_id,)
    ).fetchone()["resume_sha256"]

    # Simulate a same-day rebuild overwriting the file on disk.
    resume.write_text("rebuilt content, totally different", encoding="utf-8")

    stored_hash = conn.execute(
        "SELECT resume_sha256 FROM packet_sends WHERE id=?", (send_id,)
    ).fetchone()["resume_sha256"]
    assert stored_hash == original_hash
    assert stored_hash != ps._sha256_file(resume)


def test_a_new_send_after_a_rebuild_creates_a_second_row(conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "v1 content")
    first = ps.record_send(conn, job_id, str(tmp_path), resume)

    resume.write_text("v2 content, rebuilt", encoding="utf-8")
    second = ps.record_send(conn, job_id, str(tmp_path), resume)

    assert first != second
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM packet_sends WHERE job_id=?", (job_id,)
    ).fetchone()
    assert count["n"] == 2


def test_a_new_attempt_after_a_terminal_outcome_creates_a_second_row(conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "same unchanged file")
    first = ps.record_send(conn, job_id, str(tmp_path), resume)
    ps.record_outcome(conn, first, "rejected")

    # Reapplying later with the exact same, unrebuilt file is a genuinely new
    # attempt because the prior one already resolved.
    second = ps.record_send(conn, job_id, str(tmp_path), resume)

    assert first != second
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM packet_sends WHERE job_id=?", (job_id,)
    ).fetchone()
    assert count["n"] == 2


def test_outcomes_are_appended_in_order(conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "content")
    send_id = ps.record_send(conn, job_id, str(tmp_path), resume)

    ps.record_outcome(conn, send_id, "screen", note="recruiter call")
    ps.record_outcome(conn, send_id, "interview", note="panel round 1")

    rows = conn.execute(
        "SELECT outcome, note FROM packet_outcomes WHERE send_id=? ORDER BY id", (send_id,)
    ).fetchall()
    assert [r["outcome"] for r in rows] == ["screen", "interview"]
    assert rows[1]["note"] == "panel round 1"


def test_record_outcome_rejects_unknown_outcome(conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "content")
    send_id = ps.record_send(conn, job_id, str(tmp_path), resume)

    with pytest.raises(ValueError):
        ps.record_outcome(conn, send_id, "not_a_real_outcome")


def test_outcome_report_shows_furthest_stage_reached(conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "content")
    send_id = ps.record_send(conn, job_id, str(tmp_path), resume)
    ps.record_outcome(conn, send_id, "screen")
    ps.record_outcome(conn, send_id, "interview")
    ps.record_outcome(conn, send_id, "rejected")

    report = ps.outcome_report(conn)
    assert len(report) == 1
    entry = report[0]
    assert entry["furthest_stage"] == "interview"
    assert entry["latest_outcome"] == "rejected"
    assert [o["outcome"] for o in entry["outcomes"]] == ["screen", "interview", "rejected"]


def test_outcome_report_handles_send_with_no_outcome_yet(conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "content")
    ps.record_send(conn, job_id, str(tmp_path), resume, now=datetime(2026, 9, 16, 9, 0, 0))

    report = ps.outcome_report(conn)
    assert len(report) == 1
    entry = report[0]
    assert entry["outcomes"] == []
    assert entry["furthest_stage"] == "applied"
    assert entry["latest_outcome"] is None


def test_record_send_raises_on_missing_resume_file(conn, tmp_path):
    job_id = _job(conn)
    missing = tmp_path / "does_not_exist.md"

    with pytest.raises(FileNotFoundError):
        ps.record_send(conn, job_id, str(tmp_path), missing)


def test_latest_send_id_returns_none_for_job_with_no_send(conn):
    job_id = _job(conn)
    assert ps.latest_send_id(conn, job_id) is None


def test_latest_send_id_picks_the_newest_send(conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "v1")
    first = ps.record_send(conn, job_id, str(tmp_path), resume, now=datetime(2026, 1, 1))
    ps.record_outcome(conn, first, "rejected")
    resume.write_text("v2", encoding="utf-8")
    second = ps.record_send(conn, job_id, str(tmp_path), resume, now=datetime(2026, 2, 1))

    assert ps.latest_send_id(conn, job_id) == second


def test_record_outcome_for_job_is_a_noop_when_job_has_no_send(conn):
    job_id = _job(conn)
    # No exception, no row written -- exactly the "silent no-op" contract
    # stage_moves relies on for jobs that moved before packet_sends existed.
    assert ps.record_outcome_for_job(conn, job_id, "screen") is None
    count = conn.execute("SELECT COUNT(*) AS n FROM packet_outcomes").fetchone()
    assert count["n"] == 0


def test_record_outcome_for_job_appends_to_the_latest_send(conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "content")
    send_id = ps.record_send(conn, job_id, str(tmp_path), resume)

    result_id = ps.record_outcome_for_job(conn, job_id, "screen", note="recruiter reached out")

    row = conn.execute("SELECT * FROM packet_outcomes WHERE id=?", (result_id,)).fetchone()
    assert row["send_id"] == send_id
    assert row["outcome"] == "screen"
    assert row["note"] == "recruiter reached out"


def test_record_outcome_for_job_still_raises_on_an_invalid_outcome(conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "content")
    ps.record_send(conn, job_id, str(tmp_path), resume)

    # A genuine bug (a typo'd outcome name) must not be swallowed at this
    # layer -- only the callers (stage_moves, etc.) wrap this best-effort.
    with pytest.raises(ValueError):
        ps.record_outcome_for_job(conn, job_id, "not_a_real_outcome")


def test_reopen_then_reapply_with_unchanged_files_coalesces_to_the_same_send(conn, tmp_path):
    """The idempotency decision documented in record_send's docstring: a
    reopen appends a fresh 'applied' outcome (un-terminalizing the send), so
    reapplying afterwards with byte-identical files is recognized as the
    same attempt, not a new one."""
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "same unchanged file")
    first = ps.record_send(conn, job_id, str(tmp_path), resume)
    ps.record_outcome(conn, first, "rejected")

    # Simulates what web.stage_moves.reopen does: record a fresh 'applied'
    # outcome on the existing send when the user reopens a closed job.
    ps.record_outcome_for_job(conn, job_id, "applied")

    second = ps.record_send(conn, job_id, str(tmp_path), resume)

    assert second == first
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM packet_sends WHERE job_id=?", (job_id,)
    ).fetchone()
    assert count["n"] == 1


def test_stage_durations_reports_days_at_each_stage(conn):
    events = [
        {"stage": "applied", "at": "2026-09-01T09:00:00"},
        {"stage": "screen", "at": "2026-09-05T09:00:00"},
        {"stage": "interview", "at": "2026-09-10T09:00:00"},
    ]
    durations = ps.stage_durations(events, now=datetime(2026, 9, 13, 9, 0, 0))

    assert durations == [
        {"stage": "applied", "entered_at": "2026-09-01T09:00:00", "days": 4.0},
        {"stage": "screen", "entered_at": "2026-09-05T09:00:00", "days": 5.0},
        {"stage": "interview", "entered_at": "2026-09-10T09:00:00", "days": 3.0},
    ]


def test_stage_durations_of_empty_events_is_empty(conn):
    assert ps.stage_durations([]) == []


def test_job_event_log_is_ordered_oldest_first(conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "content")
    send_id = ps.record_send(conn, job_id, str(tmp_path), resume, now=datetime(2026, 9, 1, 9, 0))
    ps.record_outcome(conn, send_id, "screen", note="recruiter call")
    ps.record_outcome(conn, send_id, "interview", note="panel round 1")

    log = ps.job_event_log(conn, job_id)

    assert [e["stage"] for e in log] == ["applied", "screen", "interview"]
    assert all(e["send_id"] == send_id for e in log)
    assert log[1]["note"] == "recruiter call"


def test_job_event_log_spans_multiple_sends(conn, tmp_path):
    # record_outcome always stamps real "now", so this leaves send/outcome
    # calls in natural call order (no backdated `now=`) to keep the resulting
    # timestamps monotonically increasing in the order asserted below.
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "v1")
    first = ps.record_send(conn, job_id, str(tmp_path), resume)
    ps.record_outcome(conn, first, "rejected", note="")

    resume.write_text("v2", encoding="utf-8")
    second = ps.record_send(conn, job_id, str(tmp_path), resume)
    ps.record_outcome(conn, second, "screen", note="")

    log = ps.job_event_log(conn, job_id)

    assert [e["send_id"] for e in log] == [first, first, second, second]
    assert [e["stage"] for e in log] == ["applied", "rejected", "applied", "screen"]


def test_job_event_log_empty_for_job_with_no_send(conn):
    job_id = _job(conn)
    assert ps.job_event_log(conn, job_id) == []


def test_no_ranking_or_comparison_function_exists_in_public_api(conn):
    """HARD CONSTRAINT (module docstring, and docs/truth-guarantees.md):
    packet_sends does descriptive bookkeeping only. If a future
    contributor adds a rank_packets / success_rate / win_rate / compare_sends
    style function, this test should fail the build rather than rely on
    someone re-reading the docstring. Revisit docs/truth-guarantees.md
    before changing this."""
    forbidden_substrings = ("rank", "compare", "success_rate", "win_rate", "best_packet")
    public_names = [name for name in dir(ps) if not name.startswith("_")]
    offenders = [
        name for name in public_names if any(term in name.lower() for term in forbidden_substrings)
    ]
    assert offenders == []


def test_fatal_findings_from_report_converts_dicts_to_real_enum_members():
    report = {
        "findings": [
            {
                "severity": "fatal",
                "code": "banned_characters",
                "message": "Contains illegal characters",
                "artifact": "resume",
                "line": 4,
                "evidence": "�",
                "source": "Workday rejection 2026-09-10",
            }
        ]
    }
    out = ps.fatal_findings_from_report(report)
    assert len(out) == 1
    f = out[0]
    assert isinstance(f, Finding)
    # StrEnum IDENTITY, not just equality -- PacketReport.fatal() checks `is`.
    assert f.severity is Severity.FATAL
    assert f.artifact is Artifact.RESUME
    assert f.code == "banned_characters"
    assert f.line == 4
    assert f.evidence == "�"


def test_fatal_findings_from_report_excludes_non_fatal_severity():
    report = {
        "findings": [
            {"severity": "major", "code": "x", "message": "m", "artifact": "resume"},
            {"severity": "minor", "code": "y", "message": "m", "artifact": "cover_letter"},
        ]
    }
    assert ps.fatal_findings_from_report(report) == ()


def test_fatal_findings_from_report_skips_garbage_entries_without_raising():
    report = {
        "findings": [
            "not a dict",
            {"severity": "not-a-real-severity", "code": "x", "message": "m"},
            {"severity": "fatal", "code": "x"},  # missing message
            {"artifact": "resume"},  # missing severity entirely
            {
                "severity": "fatal",
                "code": "empty_recent_role",
                "message": "no bullets",
                "artifact": "not-a-real-artifact",
            },
        ]
    }
    assert ps.fatal_findings_from_report(report) == ()


def test_fatal_findings_from_report_handles_missing_or_empty_findings_key():
    assert ps.fatal_findings_from_report({}) == ()
    assert ps.fatal_findings_from_report({"findings": []}) == ()
    assert ps.fatal_findings_from_report({"findings": None}) == ()


def test_fatal_findings_from_report_defaults_optional_fields():
    report = {
        "findings": [
            {"severity": "fatal", "code": "x", "message": "m"},
        ]
    }
    out = ps.fatal_findings_from_report(report)
    assert len(out) == 1
    assert out[0].artifact is Artifact.RESUME
    assert out[0].line is None
    assert out[0].evidence == ""
    assert out[0].source == ""


def test_record_send_stores_converted_fatal_findings(conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "resume content")
    report = {
        "findings": [
            {
                "severity": "fatal",
                "code": "banned_characters",
                "message": "Contains illegal characters",
                "artifact": "resume",
            }
        ]
    }
    send_id = ps.record_send(
        conn, job_id, str(tmp_path), resume, fatal_findings=ps.fatal_findings_from_report(report)
    )
    row = conn.execute(
        "SELECT fatal_findings_json FROM packet_sends WHERE id=?", (send_id,)
    ).fetchone()
    stored = json.loads(row["fatal_findings_json"])
    assert stored != []
    assert stored[0]["code"] == "banned_characters"
    assert stored[0]["severity"] == "fatal"


def test_cover_letter_hash_recorded_and_included_in_dedupe_key(conn, tmp_path):
    job_id = _job(conn)
    resume = _write(tmp_path, "resume.md", "resume content")
    cover_v1 = _write(tmp_path, "cover_v1.md", "cover v1")

    first = ps.record_send(conn, job_id, str(tmp_path), resume, cover_letter_path=cover_v1)

    cover_v2 = _write(tmp_path, "cover_v2.md", "cover v2, different letter")
    second = ps.record_send(conn, job_id, str(tmp_path), resume, cover_letter_path=cover_v2)

    assert first != second
    row = conn.execute(
        "SELECT cover_letter_sha256 FROM packet_sends WHERE id=?", (first,)
    ).fetchone()
    assert row["cover_letter_sha256"] == ps._sha256_file(cover_v1)

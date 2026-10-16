"""Group A, item A1: quarantine is per stem, fail-closed, and the download
resolver refuses files a QUARANTINED report names."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from blastjob.core.quarantine import (
    QuarantineIncompleteError,
    held_names,
    is_quarantined,
    quarantine,
    quarantined_files,
    release,
    targeted_files,
)
from blastjob.exporters import ats, markdown
from blastjob.models.config import BlastJobConfig, PathsConfig
from blastjob.web import downloads

ROLE_A = "Alex_Rivera_Resume_-_Acme_VP_IT"
ROLE_B = "Alex_Rivera_Resume_-_Acme_CIO"
LETTER_B = "Alex_Rivera_Cover_Letter_-_Acme_CIO"


def _write_role(folder: Path, stem: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{stem}.md").write_text(f"# {stem}\n", encoding="utf-8")
    (folder / f"{stem}.docx").write_bytes(b"docx")
    (folder / f"{stem}_ATS.ats.txt").write_text("ats", encoding="utf-8")


def test_second_failing_role_in_a_shared_folder_is_held_too(tmp_path):
    _write_role(tmp_path, ROLE_A)
    _write_role(tmp_path, ROLE_B)

    quarantine(tmp_path, stems=[ROLE_A, f"{ROLE_A}_ATS"], reasons=["a is bad"])
    quarantine(tmp_path, stems=[ROLE_B, f"{ROLE_B}_ATS"], reasons=["b is bad"])

    assert targeted_files(tmp_path, [ROLE_A, ROLE_B, f"{ROLE_A}_ATS", f"{ROLE_B}_ATS"]) == []
    held = {p.name for p in quarantined_files(tmp_path)}
    assert f"{ROLE_B}.md" in held and f"{ROLE_A}.md" in held


def test_why_records_every_stem_separately(tmp_path):
    _write_role(tmp_path, ROLE_A)
    _write_role(tmp_path, ROLE_B)

    quarantine(tmp_path, stems=[ROLE_A], reasons=["reason for a"])
    quarantine(tmp_path, stems=[ROLE_B], reasons=["reason for b"])

    why = (tmp_path / "quarantine" / "WHY.txt").read_text(encoding="utf-8")
    assert "reason for a" in why and "reason for b" in why
    assert ROLE_A in why and ROLE_B in why


def test_requarantining_the_same_stem_adds_no_second_entry(tmp_path):
    _write_role(tmp_path, ROLE_A)
    quarantine(tmp_path, stems=[ROLE_A], reasons=["once"])
    before = (tmp_path / "quarantine" / "WHY.txt").read_text(encoding="utf-8")

    quarantine(tmp_path, stems=[ROLE_A], reasons=["again"])

    assert (tmp_path / "quarantine" / "WHY.txt").read_text(encoding="utf-8") == before


def test_release_with_stems_frees_only_that_role(tmp_path):
    _write_role(tmp_path, ROLE_A)
    _write_role(tmp_path, ROLE_B)
    quarantine(tmp_path, stems=[ROLE_A, f"{ROLE_A}_ATS", ROLE_B, f"{ROLE_B}_ATS"], reasons=["x"])

    moved = release(tmp_path, reason="a is fine", stems=[ROLE_A])

    assert {p.name for p in moved} == {
        f"{ROLE_A}.md",
        f"{ROLE_A}.docx",
        f"{ROLE_A}_ATS.ats.txt",
    }
    assert is_quarantined(tmp_path)
    assert (tmp_path / "quarantine" / f"{ROLE_B}.md").exists()
    assert not (tmp_path / f"{ROLE_B}.md").exists()


def test_requarantine_of_a_held_role_replaces_the_held_copy_and_leaves_none_public(tmp_path):
    _write_role(tmp_path, ROLE_A)
    quarantine(tmp_path, stems=[ROLE_A], reasons=["first"])
    (tmp_path / f"{ROLE_A}.md").write_text("NEWER", encoding="utf-8")  # same-day re-tailor

    quarantine(tmp_path, stems=[ROLE_A], reasons=["second"])

    assert not (tmp_path / f"{ROLE_A}.md").exists()
    assert (tmp_path / "quarantine" / f"{ROLE_A}.md").read_text(encoding="utf-8") == "NEWER"
    # The superseded copy is parked, not lost, and release() never returns it.
    parked = list((tmp_path / "quarantine" / "superseded").iterdir())
    assert len(parked) == 1
    assert parked[0].read_text(encoding="utf-8").startswith("# ")
    release(tmp_path, reason="ok")
    assert (tmp_path / f"{ROLE_A}.md").read_text(encoding="utf-8") == "NEWER"


def test_a_file_that_cannot_move_raises_instead_of_staying_public(tmp_path, monkeypatch):
    _write_role(tmp_path, ROLE_A)
    real_replace = __import__("os").replace

    def flaky(src, dst):
        if str(src).endswith(".docx"):
            raise PermissionError("locked")
        return real_replace(src, dst)

    monkeypatch.setattr("blastjob.core.quarantine.os.replace", flaky)

    with pytest.raises(QuarantineIncompleteError) as err:
        quarantine(tmp_path, stems=[ROLE_A, f"{ROLE_A}_ATS"], reasons=["x"])

    assert err.value.leftovers == [f"{ROLE_A}.docx"]
    # The movable files still went.
    assert (tmp_path / "quarantine" / f"{ROLE_A}.md").exists()


def test_real_exporter_outputs_are_all_covered(tmp_path):
    markdown.write("# r", tmp_path, stem=ROLE_A)
    ats.write("# r", tmp_path, stem=f"{ROLE_A}_ATS")

    quarantine(tmp_path, stems=[ROLE_A, f"{ROLE_A}_ATS"], reasons=["x"])

    assert [p.name for p in tmp_path.iterdir() if p.is_file()] == []


# --- held_names ------------------------------------------------------------------


def _report(folder: Path, slug: str, **fields) -> None:
    (folder / f"{slug}.packet_report.json").write_text(json.dumps(fields), encoding="utf-8")


def test_held_names_reads_named_files_of_a_quarantined_report(tmp_path):
    _report(tmp_path, "cio", state="quarantined", quarantined_files=["a.md", "b.docx"])
    _report(tmp_path, "vp", state="ready", quarantined_files=["z.md"])

    assert held_names(tmp_path) == {"a.md", "b.docx"}


def test_held_names_ignores_an_acknowledged_override(tmp_path):
    _report(tmp_path, "cio", state="acknowledged_override", quarantined_files=["a.md"])

    assert held_names(tmp_path) == frozenset()


def test_held_names_malformed_report_names_nothing(tmp_path):
    (tmp_path / "cio.packet_report.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "vp.packet_report.json").write_text('["a list"]', encoding="utf-8")
    _report(tmp_path, "ok", state="quarantined", quarantined_files="not-a-list")

    assert held_names(tmp_path) == frozenset()


def test_held_names_derives_names_for_a_report_from_before_files_were_recorded(tmp_path):
    _write_role(tmp_path, ROLE_B)
    (tmp_path / f"{LETTER_B}.md").write_text("letter", encoding="utf-8")
    (tmp_path / "cio.metadata.json").write_text(
        json.dumps({"company": "Acme", "role": "CIO"}), encoding="utf-8"
    )
    _report(tmp_path, "cio", state="quarantined", quarantined_artifacts=["cover_letter"])

    assert held_names(tmp_path) == {f"{LETTER_B}.md"}


def test_held_names_reads_a_bare_legacy_report(tmp_path):
    (tmp_path / "packet_report.json").write_text(
        json.dumps({"state": "quarantined", "quarantined_files": ["resume.md"]}),
        encoding="utf-8",
    )

    assert held_names(tmp_path) == {"resume.md"}


# --- download resolver -----------------------------------------------------------


def _conn_with_packet(folder: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE build_runs (job_id, status, artifact_dir, id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO build_runs VALUES (1, 'done', ?, 1)", (str(folder),))
    return conn


def test_download_refuses_a_file_a_quarantined_report_names(tmp_path):
    out = tmp_path / "out"
    folder = out / "acme-2026-10-16"
    _write_role(folder, ROLE_B)
    _report(folder, "cio", state="quarantined", quarantined_files=[f"{ROLE_B}.md"])
    cfg = BlastJobConfig(paths=PathsConfig(output_dir=str(out), data_dir=str(tmp_path / "d")))
    conn = _conn_with_packet(folder)

    assert downloads.resolve_download(conn, cfg, 1, f"{ROLE_B}.md") is None


def test_download_still_serves_files_of_a_role_that_passed(tmp_path):
    out = tmp_path / "out"
    folder = out / "acme-2026-10-16"
    _write_role(folder, ROLE_A)
    _write_role(folder, ROLE_B)
    _report(folder, "cio", state="quarantined", quarantined_files=[f"{ROLE_B}.md"])
    _report(folder, "vp", state="ready")
    cfg = BlastJobConfig(paths=PathsConfig(output_dir=str(out), data_dir=str(tmp_path / "d")))
    conn = _conn_with_packet(folder)

    served = downloads.resolve_download(conn, cfg, 1, f"{ROLE_A}.md")

    assert served == folder / f"{ROLE_A}.md"


def test_why_timestamp_is_recorded(tmp_path):
    _write_role(tmp_path, ROLE_A)
    quarantine(tmp_path, stems=[ROLE_A], reasons=["r"], now=datetime(2026, 10, 16, 9, 0, 0))

    assert "2026-10-16T09:00:00" in (tmp_path / "quarantine" / "WHY.txt").read_text()

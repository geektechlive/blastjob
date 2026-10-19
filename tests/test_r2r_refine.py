"""Round 2 (R2, R3, R4, R11): refine's publish/hold ordering and what it carries
forward from the packet's previous report."""

from __future__ import annotations

import json
import os

import pytest

from blastjob.core import quarantine as qz
from blastjob.core.cover_letter_score import CoverLetterScore
from blastjob.core.refine_files import publish
from tests.test_a_refine import BAD, GOOD, VP, _patch, _refine, _seed, _stem

LETTER = _stem("Cover_Letter", "VP IT")


def _report(run_dir):
    return json.loads((run_dir / "vp-it.packet_report.json").read_text())


def _write_old_report(run_dir, **fields):
    old = {"state": "ready", "findings": [], "checks_run": [], **fields}
    path = run_dir / "vp-it.packet_report.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    os.utime(path, (4_000_000_000, 4_000_000_000))  # newer than any letter: unchanged


def _letter(run_dir, text="Dear team,\n\nHello.\n"):
    path = run_dir / f"{LETTER}.md"
    path.write_text(text, encoding="utf-8")
    os.utime(path, (1_000_000_000, 1_000_000_000))
    return path


# ---- R2 -------------------------------------------------------------------


async def test_a_sidecar_write_failure_does_not_stop_the_hold(tmp_path, monkeypatch):
    run_dir, cfg = _seed(tmp_path)
    _patch(monkeypatch, BAD)

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr("blastjob.core.refine._write_fit_score", boom)

    result = await _refine(run_dir, cfg)

    assert result.held
    assert not (run_dir / f"{VP}.md").exists()
    assert (run_dir / "quarantine" / f"{VP}.md").read_text() == BAD


async def test_a_metadata_write_failure_does_not_stop_the_hold(tmp_path, monkeypatch):
    run_dir, cfg = _seed(tmp_path)
    _patch(monkeypatch, BAD)

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr("blastjob.core.refine._append_version_to_metadata", boom)

    result = await _refine(run_dir, cfg)

    assert result.held
    assert not (run_dir / f"{VP}.md").exists()


async def test_a_publish_that_fails_after_the_rotation_loses_neither_resume(tmp_path, monkeypatch):
    run_dir, cfg = _seed(tmp_path)
    _patch(monkeypatch, GOOD)
    real = os.replace

    def flaky(src, dst):
        # The rotation succeeds; placing the new markdown fails.
        if ".refine" in str(src) and str(dst).endswith(f"{VP}.md"):
            raise PermissionError("locked")
        return real(src, dst)

    monkeypatch.setattr("blastjob.core.refine_files.os.replace", flaky)

    with pytest.raises(PermissionError):
        await _refine(run_dir, cfg)

    # The original is back in place, and the revision survives under .refine/.
    assert (run_dir / f"{VP}.md").read_text() == "ORIGINAL VP IT\n"
    kept = [p for p in (run_dir / ".refine").rglob("*.md") if p.read_text() == GOOD]
    assert kept


def test_publish_restores_the_rotated_original_when_placing_fails(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    stage = tmp_path / "stage"
    stage.mkdir()
    (run_dir / "S.md").write_text("old")
    (stage / "S.md").write_text("new")
    real = os.replace

    def flaky(src, dst):
        if str(src).endswith("stage/S.md"):
            raise PermissionError("locked")
        return real(src, dst)

    monkeypatch.setattr("blastjob.core.refine_files.os.replace", flaky)

    with pytest.raises(PermissionError):
        publish(stage, run_dir, "S", prior_n=1, source_in_quarantine=False)

    assert (run_dir / "S.md").read_text() == "old"
    assert (stage / "S.md").read_text() == "new"


# ---- R3 -------------------------------------------------------------------


async def test_a_letter_the_old_report_listed_as_unscored_is_rescored(tmp_path, monkeypatch):
    run_dir, cfg = _seed(tmp_path)
    _letter(run_dir)
    _write_old_report(run_dir, checks_skipped=["cover_letter_claims"])
    _patch(monkeypatch, GOOD)
    calls = []

    async def fake_score(letter_md, **_kw):
        calls.append(letter_md)
        return CoverLetterScore(overall_score=91, scored=True)

    monkeypatch.setattr("blastjob.core.refine.score_cover_letter", fake_score)

    await _refine(run_dir, cfg)

    assert len(calls) == 1
    report = _report(run_dir)
    assert "cover_letter_claims" not in report["checks_skipped"]
    assert report["cover_letter_groundedness"] == 91


async def test_a_missing_letter_generation_stays_recorded_as_skipped(tmp_path, monkeypatch):
    run_dir, cfg = _seed(tmp_path)
    _write_old_report(run_dir, checks_skipped=["cover_letter_generation"])
    _patch(monkeypatch, GOOD)

    await _refine(run_dir, cfg)

    assert "cover_letter_generation" in _report(run_dir)["checks_skipped"]


async def test_uncited_research_note_survives_a_refine(tmp_path, monkeypatch):
    """Refine never re-researches, so research with no cited source still has none."""
    run_dir, cfg = _seed(tmp_path)
    _write_old_report(run_dir, checks_skipped=["company_research_sources"])
    _patch(monkeypatch, GOOD)

    await _refine(run_dir, cfg)

    assert "company_research_sources" in _report(run_dir)["checks_skipped"]


# ---- R4 -------------------------------------------------------------------


async def test_a_verification_crash_never_lifts_the_hold_on_a_held_source(tmp_path, monkeypatch):
    run_dir, cfg = _seed(tmp_path)
    qz.quarantine(run_dir, stems=[VP], reasons=["bad"])
    _write_old_report(
        run_dir,
        state="quarantined",
        quarantined_artifacts=["resume"],
        quarantined_files=[f"{VP}.md", f"{VP}.docx"],
    )
    _patch(monkeypatch, GOOD)

    def crash(*_a, **_kw):
        raise RuntimeError("gate bug")

    monkeypatch.setattr("blastjob.core.packet_gate.gate", crash)

    result = await _refine(run_dir, cfg)

    assert result.held
    assert not (run_dir / f"{VP}.md").exists()
    assert (run_dir / "quarantine" / f"{VP}.md").read_text() == GOOD
    report = _report(run_dir)
    assert f"{VP}.md" in report["quarantined_files"]
    assert report["sendable"] is False
    assert f"{VP}.md" in qz.held_names(run_dir)


async def test_a_verification_crash_on_a_public_source_keeps_it_public_like_a_build(
    tmp_path, monkeypatch
):
    run_dir, cfg = _seed(tmp_path)
    _patch(monkeypatch, GOOD)

    def crash(*_a, **_kw):
        raise RuntimeError("gate bug")

    monkeypatch.setattr("blastjob.core.packet_gate.gate", crash)

    result = await _refine(run_dir, cfg)

    assert not result.held
    assert _report(run_dir)["state"] == "verification_failed"
    assert (run_dir / f"{VP}.md").read_text() == GOOD


# ---- R11 ------------------------------------------------------------------


async def test_a_released_letter_with_unchanged_findings_stays_released(tmp_path, monkeypatch):
    run_dir, cfg = _seed(tmp_path)
    letter = _letter(run_dir)
    fatal = {
        "severity": "fatal",
        "code": "cover_letter_claim_refuted",
        "message": "letter claim",
        "artifact": "cover_letter",
        "line": None,
        "evidence": "",
        "source": "x",
    }
    override = {"reason": "I checked it", "at": "2026-10-16T09:00:00"}
    _write_old_report(
        run_dir, state="acknowledged_override", findings=[fatal], override=override, sendable=True
    )
    _patch(monkeypatch, GOOD)

    result = await _refine(run_dir, cfg)

    assert letter.is_file()  # not re-held
    assert not (run_dir / "quarantine" / letter.name).exists()
    report = _report(run_dir)
    assert report["state"] == "acknowledged_override" and report["sendable"] is True
    assert report["override"] == override
    assert result.state == "acknowledged_override"


async def test_a_released_letter_is_held_again_when_it_changed(tmp_path, monkeypatch):
    run_dir, cfg = _seed(tmp_path)
    letter = _letter(run_dir)
    fatal = {
        "severity": "fatal",
        "code": "cover_letter_claim_refuted",
        "message": "letter claim",
        "artifact": "cover_letter",
        "line": None,
        "evidence": "",
        "source": "x",
    }
    _write_old_report(
        run_dir,
        state="acknowledged_override",
        findings=[fatal],
        override={"reason": "r", "at": "t"},
    )
    os.utime(letter, (4_100_000_000, 4_100_000_000))  # edited after the report
    _patch(monkeypatch, GOOD)

    async def refuted(letter_md, **_kw):
        from blastjob.core.packet_report import Artifact, Finding, Severity

        return CoverLetterScore(
            overall_score=10,
            scored=True,
            findings=(
                Finding(
                    severity=Severity.FATAL,
                    code="cover_letter_claim_refuted",
                    message="new bad claim",
                    artifact=Artifact.COVER_LETTER,
                ),
            ),
        )

    monkeypatch.setattr("blastjob.core.refine.score_cover_letter", refuted)

    await _refine(run_dir, cfg)

    assert not letter.exists()
    assert (run_dir / "quarantine" / letter.name).exists()
    assert "override" not in _report(run_dir)

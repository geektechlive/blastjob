"""Group A, A1 + A4 through the real `run_build`: a failing role is held even when
another role in the shared folder already is, a hold that cannot complete fails
the build, and a scorer that did not run is recorded."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from blastjob.core import quarantine as qz
from blastjob.core.build import PacketNotQuarantinedError, run_build
from blastjob.llm.cost import CostTracker
from blastjob.models.config import BlastJobConfig

# The history states no money figure, so any "$..." in a resume is a FATAL
# unverified_total and the gate quarantines the resume.
HISTORY = "---\nname: Jane Doe\n---\n\n## Experience\n\n- Ran IT.\n"
BAD_RESUME = "# Jane Doe\n\n- Saved $9,999,999 a year.\n"
GOOD_RESUME = "# Jane Doe\n\n- Ran IT.\n"
FIT_JSON = (
    '{"overall_score": 80, "jd_alignment_score": 80, "groundedness_score": 90, '
    '"claims": [], "summary": "ok"}'
)


class _Usage:
    input_tokens = 10
    output_tokens = 5
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _Final:
    usage = _Usage()


class _Stream:
    def __init__(self, text: str) -> None:
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    @property
    def text_stream(self):
        async def _iter():
            yield self._text

        return _iter()

    async def get_final_message(self):
        return _Final()


def _cfg(tmp_path: Path) -> BlastJobConfig:
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.paths.output_dir = str(tmp_path / "out")
    Path(cfg.paths.data_dir).mkdir(parents=True, exist_ok=True)
    (Path(cfg.paths.data_dir) / "work_history.md").write_text(HISTORY, encoding="utf-8")
    return cfg


def _patch(monkeypatch, resume: str, fit: str = FIT_JSON) -> None:
    """Resume stream from build, fit-score stream from scoring; coverage is empty."""

    async def build_stream(*_a, **_kw):
        return _Stream(resume)

    async def score_stream(*_a, **_kw):
        return _Stream(fit)

    async def coverage_stream(*_a, **_kw):
        return _Stream("[]")

    monkeypatch.setattr("blastjob.core.build.make_stream", build_stream)
    monkeypatch.setattr("blastjob.core.scoring.make_stream", score_stream)
    monkeypatch.setattr("blastjob.core.coverage.make_stream", coverage_stream)


async def _build(cfg: BlastJobConfig, role: str) -> Path:
    out_dir, _ = await run_build(
        company="Acme",
        role=role,
        job_description="Lead IT.",
        formats={"md", "ats"},
        use_ats=False,
        app_config=cfg,
        cost_tracker=CostTracker(),
        confidential=True,
    )
    return out_dir


def _public_documents(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.is_file() and "_Resume_-_" in p.name)


def _report(folder: Path, slug: str) -> dict:
    return json.loads((folder / f"{slug}.packet_report.json").read_text(encoding="utf-8"))


async def test_second_failing_role_in_the_same_folder_is_held_not_public(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, BAD_RESUME)

    folder = await _build(cfg, "VP IT")
    await _build(cfg, "CIO")

    assert _public_documents(folder) == []
    held = {p.name for p in qz.quarantined_files(folder)}
    assert any("_VP_IT" in n for n in held) and any("_CIO" in n for n in held)
    assert _report(folder, "cio")["quarantined_files"]


async def test_report_names_the_files_it_holds(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, BAD_RESUME)

    folder = await _build(cfg, "VP IT")

    report = _report(folder, "vp-it")
    assert report["state"] == "quarantined"
    assert sorted(report["quarantined_files"]) == sorted(
        p.name for p in qz.quarantined_files(folder)
    )
    assert any(n.endswith(".ats.txt") for n in report["quarantined_files"])


async def test_a_hold_that_cannot_complete_fails_the_build(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, BAD_RESUME)

    real_replace = os.replace

    def refuse(src, dst):
        if str(src).endswith("_ATS.ats.txt"):  # only the held documents; sidecars must write
            raise PermissionError("locked")
        return real_replace(src, dst)

    monkeypatch.setattr("blastjob.core.quarantine.os.replace", refuse)

    with pytest.raises(PacketNotQuarantinedError) as err:
        await _build(cfg, "VP IT")

    folder = err.value.out_dir
    assert "FAILED-TO-QUARANTINE" in str(err.value)
    report = _report(folder, "vp-it")
    assert report["quarantine_failed"] is True
    # The metadata is still written (keeps the role slug claimed) and the report
    # names the files, so the download resolver refuses them even though they
    # are still in the public folder.
    assert (folder / "vp-it.metadata.json").exists()
    assert qz.held_names(folder) == set(report["quarantined_files"]) != set()


async def test_a_clean_packet_is_not_held(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, GOOD_RESUME)

    folder = await _build(cfg, "VP IT")

    assert _report(folder, "vp-it")["state"] == "ready"
    assert qz.quarantined_files(folder) == []
    assert _public_documents(folder) != []


async def test_a_scorer_that_did_not_run_is_recorded_and_shown(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, GOOD_RESUME, fit="not json at all")

    folder = await _build(cfg, "VP IT")

    report = _report(folder, "vp-it")
    assert "resume_groundedness" in report["checks_skipped"]
    assert report["state"] == "ready"  # not blocking: groundedness is uncalibrated
    assert report["sendable"] is True
    assert any("not checked" in r.lower() for r in report["reasons"])


async def test_cli_error_in_the_fit_scorer_is_skipped_not_a_crash_and_not_silent(
    tmp_path, monkeypatch
):
    from blastjob.llm.providers import CLIError

    cfg = _cfg(tmp_path)
    _patch(monkeypatch, GOOD_RESUME)

    async def failing(*_a, **_kw):
        raise CLIError("claude -p exited 1")

    monkeypatch.setattr("blastjob.core.scoring.make_stream", failing)

    folder = await _build(cfg, "VP IT")

    report = _report(folder, "vp-it")
    assert "resume_groundedness" in report["checks_skipped"]
    assert any("not checked" in r.lower() for r in report["reasons"])
    assert _public_documents(folder) != []  # the complete packet is not lost


async def test_cli_error_generating_the_resume_fails_the_build(tmp_path, monkeypatch):
    from blastjob.llm.providers import CLIError

    cfg = _cfg(tmp_path)

    async def failing(*_a, **_kw):
        raise CLIError("claude -p timed out")

    monkeypatch.setattr("blastjob.core.build.make_stream", failing)

    with pytest.raises(CLIError):
        await _build(cfg, "VP IT")


async def test_cover_letter_generation_failure_is_recorded_not_silent(tmp_path, monkeypatch):
    from blastjob.llm.providers import CLIError

    cfg = _cfg(tmp_path)
    calls = {"n": 0}

    async def build_stream(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Stream(GOOD_RESUME)
        raise CLIError("claude -p exited 1")

    async def score_stream(*_a, **_kw):
        return _Stream(FIT_JSON)

    async def coverage_stream(*_a, **_kw):
        return _Stream("[]")

    monkeypatch.setattr("blastjob.core.build.make_stream", build_stream)
    monkeypatch.setattr("blastjob.core.scoring.make_stream", score_stream)
    monkeypatch.setattr("blastjob.core.coverage.make_stream", coverage_stream)

    out_dir, _ = await run_build(
        company="Acme",
        role="VP IT",
        job_description="Lead IT.",
        formats={"md"},
        use_ats=False,
        app_config=cfg,
        cost_tracker=CostTracker(),
        confidential=True,
        include_cover_letter=True,
    )

    assert "cover_letter_generation" in _report(out_dir, "vp-it")["checks_skipped"]

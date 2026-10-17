"""Group A, A3: refine targets the job's own role, re-verifies, gates and
quarantines, holds a per-resume lease, and writes through staging."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from blastjob.core import quarantine as qz
from blastjob.core.packet_verify import PacketNotQuarantinedError
from blastjob.core.paths import document_token, person_slug
from blastjob.core.refine import run_refine_detailed
from blastjob.core.refine_files import RefineBusyError, lease
from blastjob.llm.cost import CostTracker
from blastjob.models.config import BlastJobConfig, PathsConfig

HISTORY = "---\nname: Jane Doe\n---\n\n## Experience\n\n- Ran IT.\n"
GOOD = "# Jane Doe\n\n- Ran IT well.\n"
BAD = "# Jane Doe\n\n- Saved $9,999,999 a year.\n"
PERSON = person_slug("Jane Doe")


def _stem(kind: str, role: str, company: str = "Acme") -> str:
    return f"{PERSON}_{kind}_-_{document_token(company, role)}"


VP, CIO = _stem("Resume", "VP IT"), _stem("Resume", "CIO")


class _Usage:
    input_tokens = 1
    output_tokens = 1
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _Final:
    usage = _Usage()


class _Stream:
    def __init__(self, text):
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    @property
    def text_stream(self):
        async def it():
            yield self._text

        return it()

    async def get_final_message(self):
        return _Final()


def _patch(monkeypatch, revised: str, fit: str = "{}"):
    async def refine_stream(*_a, **_kw):
        return _Stream(revised)

    async def score_stream(*_a, **_kw):
        return _Stream(fit)

    monkeypatch.setattr("blastjob.core.refine.make_stream", refine_stream)
    monkeypatch.setattr("blastjob.core.scoring.make_stream", score_stream)


def _seed(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "work_history.md").write_text(HISTORY, encoding="utf-8")
    run_dir = tmp_path / "out" / "acme-2026-10-16"
    run_dir.mkdir(parents=True)
    for slug, role, stem in (("vp-it", "VP IT", VP), ("cio", "CIO", CIO)):
        (run_dir / f"{stem}.md").write_text(f"ORIGINAL {role}\n", encoding="utf-8")
        (run_dir / f"{stem}.docx").write_bytes(b"docx")
        (run_dir / f"{slug}.job_description.md").write_text("# JD\n\n**Co**\n\nbody", "utf-8")
        (run_dir / f"{slug}.metadata.json").write_text(
            json.dumps({"company": "Acme", "role": role, "formats": ["md"]}), encoding="utf-8"
        )
    cfg = BlastJobConfig(paths=PathsConfig(data_dir=str(data), output_dir=str(tmp_path / "out")))
    return run_dir, cfg


async def _refine(run_dir, cfg, role="VP IT"):
    return await run_refine_detailed(
        run_dir, "tighten", cfg, CostTracker(), company="Acme", role=role
    )


def _top_files(run_dir: Path) -> list[str]:
    return sorted(p.name for p in run_dir.iterdir() if p.is_file())


async def test_refine_rewrites_only_the_jobs_own_role_even_when_another_is_newer(
    tmp_path, monkeypatch
):
    run_dir, cfg = _seed(tmp_path)
    os.utime(run_dir / f"{CIO}.md", (9_999_999_999, 9_999_999_999))  # CIO is the newest
    _patch(monkeypatch, GOOD)

    result = await _refine(run_dir, cfg, "VP IT")

    assert result.resume_path == run_dir / f"{VP}.md"
    assert (run_dir / f"{VP}.md").read_text() == GOOD
    assert (run_dir / f"{VP}.v1.md").read_text() == "ORIGINAL VP IT\n"
    assert (run_dir / f"{CIO}.md").read_text() == "ORIGINAL CIO\n"
    assert not (run_dir / f"{CIO}.v1.md").exists()
    meta_cio = json.loads((run_dir / "cio.metadata.json").read_text())
    assert "versions" not in meta_cio
    assert len(json.loads((run_dir / "vp-it.metadata.json").read_text())["versions"]) == 1


async def test_refine_writes_a_packet_report_for_the_role(tmp_path, monkeypatch):
    run_dir, cfg = _seed(tmp_path)
    _patch(monkeypatch, GOOD)

    result = await _refine(run_dir, cfg)

    report = json.loads((run_dir / "vp-it.packet_report.json").read_text())
    assert report["state"] == "ready" and result.state == "ready"
    assert report["checks_run"]


async def test_a_refined_resume_that_fails_the_gate_is_quarantined_like_a_build(
    tmp_path, monkeypatch
):
    run_dir, cfg = _seed(tmp_path)
    _patch(monkeypatch, BAD)

    result = await _refine(run_dir, cfg)

    assert result.state == "quarantined" and result.held
    report = json.loads((run_dir / "vp-it.packet_report.json").read_text())
    assert report["state"] == "quarantined"
    # No file of the failed version (or of its stem) is public; the other role is untouched.
    assert [n for n in _top_files(run_dir) if n.startswith(VP + ".") and ".v" not in n] == []
    assert (run_dir / "quarantine" / f"{VP}.md").read_text() == BAD
    assert (run_dir / f"{CIO}.md").exists()
    assert not (run_dir / f"{VP}.docx").exists()  # stale export of the replaced version
    assert qz.held_names(run_dir) == set(report["quarantined_files"])


async def test_refine_can_start_from_a_quarantined_source_and_leaves_nothing_held(
    tmp_path, monkeypatch
):
    run_dir, cfg = _seed(tmp_path)
    qz.quarantine(run_dir, stems=[VP], reasons=["bad"])
    assert not (run_dir / f"{VP}.md").exists()
    _patch(monkeypatch, GOOD)

    result = await _refine(run_dir, cfg)

    assert result.state == "ready"
    assert (run_dir / f"{VP}.md").read_text() == GOOD
    leftovers = [p.name for p in qz.quarantined_files(run_dir) if p.name.startswith(VP)]
    assert leftovers == []
    # The failed source and its archive were parked, not deleted.
    assert any((run_dir / "quarantine" / "superseded").iterdir())


async def test_no_staging_or_temp_files_are_left_in_the_packet_folder(tmp_path, monkeypatch):
    run_dir, cfg = _seed(tmp_path)
    _patch(monkeypatch, GOOD)

    await _refine(run_dir, cfg)

    assert [n for n in _top_files(run_dir) if n.startswith(".") or n.endswith(".tmp")] == []
    assert list((run_dir / ".refine").iterdir()) == []  # lock released, stage removed


async def test_a_second_refine_of_the_same_resume_is_refused_while_the_first_holds_the_lease(
    tmp_path, monkeypatch
):
    run_dir, cfg = _seed(tmp_path)
    _patch(monkeypatch, GOOD)

    with lease(run_dir, VP):
        with pytest.raises(RefineBusyError):
            await _refine(run_dir, cfg)
        # A different role's refine is not blocked.
        await _refine(run_dir, cfg, "CIO")

    await _refine(run_dir, cfg)  # released: allowed again


async def test_a_stale_lease_from_a_crashed_run_is_taken_over(tmp_path):
    run_dir, _cfg = _seed(tmp_path)
    lock = run_dir / ".refine" / f"{VP}.lock"
    lock.parent.mkdir()
    lock.write_text("123")
    os.utime(lock, (1, 1))

    with lease(run_dir, VP):
        pass


async def test_a_hold_that_cannot_complete_fails_the_refine(tmp_path, monkeypatch):
    run_dir, cfg = _seed(tmp_path)
    _patch(monkeypatch, BAD)
    real = os.replace

    def flaky(src, dst):
        if "quarantine" in str(dst) and str(src).endswith(".md"):
            raise PermissionError("locked")
        return real(src, dst)

    monkeypatch.setattr("blastjob.core.quarantine.os.replace", flaky)

    with pytest.raises(PacketNotQuarantinedError):
        await _refine(run_dir, cfg)

    report = json.loads((run_dir / "vp-it.packet_report.json").read_text())
    assert report["quarantine_failed"] is True
    assert f"{VP}.md" in qz.held_names(run_dir)  # still refused by name


async def test_a_scorer_failure_is_recorded_as_skipped(tmp_path, monkeypatch):
    from blastjob.llm.providers import CLIError

    run_dir, cfg = _seed(tmp_path)
    _patch(monkeypatch, GOOD)

    async def failing(*_a, **_kw):
        raise CLIError("claude -p exited 1")

    monkeypatch.setattr("blastjob.core.scoring.make_stream", failing)

    result = await _refine(run_dir, cfg)

    report = json.loads((run_dir / "vp-it.packet_report.json").read_text())
    assert "resume_groundedness" in report["checks_skipped"]
    assert result.fit_score is None
    assert (run_dir / f"{VP}.md").read_text() == GOOD  # the revision still lands


async def test_an_unchanged_cover_letter_is_not_rescored_and_its_findings_carry_forward(
    tmp_path, monkeypatch
):
    run_dir, cfg = _seed(tmp_path)
    letter = _stem("Cover_Letter", "VP IT")
    (run_dir / f"{letter}.md").write_text("Dear team,\n\nHello.\n", encoding="utf-8")
    finding = {
        "severity": "major",
        "code": "unsupported_claim",
        "message": "letter claim",
        "artifact": "cover_letter",
        "line": None,
        "evidence": "",
        "source": "x",
    }
    old = {
        "state": "ready",
        "findings": [finding],
        "cover_letter_groundedness": 77,
        "checks_run": [],
    }
    (run_dir / "vp-it.packet_report.json").write_text(json.dumps(old), encoding="utf-8")
    _patch(monkeypatch, GOOD)

    async def boom(*_a, **_kw):
        raise AssertionError("an unchanged letter must not be re-scored")

    monkeypatch.setattr("blastjob.core.refine.score_cover_letter", boom)

    await _refine(run_dir, cfg)

    report = json.loads((run_dir / "vp-it.packet_report.json").read_text())
    assert [f["message"] for f in report["findings"] if f["artifact"] == "cover_letter"] == [
        "letter claim"
    ]
    assert report["cover_letter_groundedness"] == 77
    assert (run_dir / f"{letter}.md").read_text() == "Dear team,\n\nHello.\n"


async def test_the_web_wrapper_passes_the_jobs_own_company_and_role(tmp_path, monkeypatch):
    from blastjob.web import refine as web_refine
    from blastjob.web.runtime import Run

    run_dir, cfg = _seed(tmp_path)
    seen: dict = {}

    async def fake_refine(run_dir_, feedback, cfg_, tracker, **kw):
        seen.update(kw)
        return run_dir_ / f"{VP}.md", None

    monkeypatch.setattr("blastjob.core.refine.run_refine", fake_refine)
    monkeypatch.setattr(web_refine, "_job_identity", lambda *_a: ("Acme", "VP IT", "tailored"))
    # The wrapper claims the job in the DB (R1), so the job must exist.
    from blastjob.db import client as dbc

    conn = dbc.init_db(cfg)
    jid = dbc.upsert_job(conn, source="s", external_id="e", title="VP IT", company="Acme")
    conn.close()

    work = web_refine.make_refine_work(str(run_dir), "tighten", lambda: cfg, job_id=jid)
    await work(Run(id="r1", name="refine"))

    assert (seen["company"], seen["role"]) == ("Acme", "VP IT")


async def test_the_web_wrapper_refuses_while_a_tailor_is_running(tmp_path, monkeypatch):
    from blastjob.web import refine as web_refine
    from blastjob.web.runtime import Run

    run_dir, cfg = _seed(tmp_path)
    monkeypatch.setattr(web_refine, "_job_identity", lambda *_a: ("Acme", "VP IT", "tailoring"))

    async def must_not_run(*_a, **_kw):
        raise AssertionError("refine must not start during a tailor")

    monkeypatch.setattr("blastjob.core.refine.run_refine", must_not_run)

    work = web_refine.make_refine_work(str(run_dir), "tighten", lambda: cfg, job_id=1)
    assert await work(Run(id="r1", name="refine")) is None


async def test_a_missing_resume_for_the_role_is_an_error_not_another_roles_file(tmp_path):
    run_dir, cfg = _seed(tmp_path)

    with pytest.raises(FileNotFoundError):
        await _refine(run_dir, cfg, "Chief Nothing Officer")

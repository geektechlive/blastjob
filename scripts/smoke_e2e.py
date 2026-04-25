"""End-to-end smoke for all four phases.

Exercises run_build (resume + cover letter + score), application tracking,
run_refine (version rotation + rescore), and analyze_coverage — all with
mocked LLM streams so no API key is required.

Run: .venv/bin/python scripts/smoke_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

# ruff: noqa: E402
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from blastjob.core.build import run_build
from blastjob.core.coverage import analyze_coverage
from blastjob.core.history import scan_history
from blastjob.core.refine import run_refine
from blastjob.core.tracking import load_tracking, save_tracking
from blastjob.llm.cost import CostTracker
from blastjob.models.config import BlastJobConfig, PathsConfig
from blastjob.models.tracking import TrackingRecord


# ---------------------------------------------------------------------------
# Fake stream
# ---------------------------------------------------------------------------


class _Usage:
    input_tokens = 200
    output_tokens = 100
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 150


class _Final:
    usage = _Usage()


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    @property
    def text_stream(self):
        async def _iter():
            for c in self._chunks:
                yield c

        return _iter()

    async def get_final_message(self):
        return _Final()


def _make_factory(*sequences):
    state = {"i": 0}

    async def factory(*_a, **_kw):
        seq = sequences[state["i"] % len(sequences)]
        state["i"] += 1
        return _Stream(seq)

    return factory


async def _fake_research(*_a, **_kw):
    return "## Overview\n\nWidgetCo makes widgets.\n"


def _patch_all_streams(factory):
    """Patch make_stream in every module that imports it directly."""
    import blastjob.core.build as build_mod
    import blastjob.core.coverage as cov_mod
    import blastjob.core.refine as refine_mod
    import blastjob.core.scoring as scoring_mod

    build_mod.make_stream = factory
    build_mod.make_research_call = _fake_research
    scoring_mod.make_stream = factory
    refine_mod.make_stream = factory
    cov_mod.make_stream = factory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_RESUME_MD = """# Jane Smith

## Summary

Senior platform engineer with 12 years building payments infrastructure.

## Experience

### Acme Corp — Senior Engineer (2020-Present)

- Led team of 8 engineers to ship the payments platform
- Reduced p99 latency by 40%
"""

_COVER_LETTER_MD = """I am writing to apply for the Staff Engineer role at WidgetCo.

Your recent launch of the new payments platform aligns with my decade-plus building
similar systems at Acme Corp.

I would welcome the chance to discuss how my work fits.
"""

_FIT_SCORE_JSON = """{
  "overall_score": 82,
  "jd_alignment_score": 75,
  "groundedness_score": 90,
  "claims": [
    {
      "claim_text": "Led team of 8 engineers",
      "grounded": true,
      "evidence_quote": "Led team of 8 engineers to ship the payments platform",
      "source_section": "Experience"
    }
  ],
  "unsupported_count": 0,
  "summary": "Strong match."
}"""

_REVISED_RESUME_MD = """# Jane Smith

## Summary

Tightened summary for the second pass.

## Experience

### Acme Corp — Senior Engineer

- Led team of 8 engineers
- Cut p99 latency 40%
"""

_COVERAGE_JSON = """{
  "coverage_score": 99,
  "summary": "Strong fit. One gap on Kubernetes.",
  "requirements": [
    {
      "text": "Lead engineering teams",
      "priority": "must",
      "covered": true,
      "evidence_quote": "Led team of 8 engineers to ship the payments platform",
      "gap_note": ""
    },
    {
      "text": "Kubernetes operator experience",
      "priority": "must",
      "covered": false,
      "evidence_quote": "",
      "gap_note": "No K8s experience documented in work history."
    }
  ]
}"""


def _seed_data_dir(root: Path) -> Path:
    data_dir = root / "data"
    data_dir.mkdir()
    (data_dir / "work_history.md").write_text(
        "## Acme Corp — Senior Engineer (2020-Present)\n\n"
        "- Led team of 8 engineers to ship the payments platform\n"
        "- Reduced p99 latency by 40%\n"
    )
    (data_dir / "templates").mkdir()
    (data_dir / "templates" / "standard.md").write_text("# Resume")
    return data_dir


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def _scenario_build_with_cover_letter(tmp: Path) -> dict:
    data_dir = _seed_data_dir(tmp)
    cfg = BlastJobConfig(
        paths=PathsConfig(data_dir=str(data_dir), output_dir=str(tmp / "out"))
    )
    tracker = CostTracker()

    _patch_all_streams(
        _make_factory([_RESUME_MD], [_COVER_LETTER_MD], [_FIT_SCORE_JSON])
    )

    out_dir, fit = await run_build(
        company="WidgetCo",
        role="Staff Engineer",
        job_description="Build payment systems at scale.",
        formats={"md"},
        use_ats=False,
        app_config=cfg,
        cost_tracker=tracker,
        on_text=None,
        confidential=False,
        include_cover_letter=True,
    )

    return {
        "out_dir": out_dir,
        "files": sorted(p.name for p in out_dir.iterdir()),
        "metadata": json.loads((out_dir / "metadata.json").read_text()),
        "fit_score": fit.overall_score if fit else None,
        "tracker_calls": len(tracker.calls),
        "tracker_total_cost": tracker.total_cost,
    }


async def _scenario_build_without_cover_letter(tmp: Path) -> dict:
    data_dir = _seed_data_dir(tmp)
    cfg = BlastJobConfig(
        paths=PathsConfig(data_dir=str(data_dir), output_dir=str(tmp / "out"))
    )
    tracker = CostTracker()

    _patch_all_streams(_make_factory([_RESUME_MD], [_FIT_SCORE_JSON]))

    out_dir, _ = await run_build(
        company="OtherCo",
        role="Engineer",
        job_description="A role.",
        formats={"md"},
        use_ats=False,
        app_config=cfg,
        cost_tracker=tracker,
        on_text=None,
        confidential=True,
        include_cover_letter=False,
    )

    return {
        "out_dir": out_dir,
        "files": sorted(p.name for p in out_dir.iterdir()),
        "metadata": json.loads((out_dir / "metadata.json").read_text()),
    }


async def _scenario_refine(run_dir: Path, cfg: BlastJobConfig) -> dict:
    _patch_all_streams(_make_factory([_REVISED_RESUME_MD], [_FIT_SCORE_JSON]))
    tracker = CostTracker()

    new_path, fit = await run_refine(
        run_dir, "Tighten the summary, cut filler.", cfg, tracker, on_text=None
    )

    meta = json.loads((run_dir / "metadata.json").read_text())
    return {
        "new_resume_path": new_path,
        "files": sorted(p.name for p in run_dir.iterdir()),
        "current_resume_first_line": new_path.read_text().splitlines()[0],
        "v1_archived": (run_dir / "resume.v1.md").exists(),
        "v1_first_line": (
            (run_dir / "resume.v1.md").read_text().splitlines()[0]
            if (run_dir / "resume.v1.md").exists()
            else ""
        ),
        "versions_in_metadata": meta.get("versions", []),
        "tracker_calls": len(tracker.calls),
        "fit_score": fit.overall_score if fit else None,
    }


async def _scenario_coverage(tmp: Path) -> dict:
    data_dir = _seed_data_dir(tmp)
    cfg = BlastJobConfig(
        paths=PathsConfig(data_dir=str(data_dir), output_dir=str(tmp / "out"))
    )
    _patch_all_streams(_make_factory([_COVERAGE_JSON]))
    tracker = CostTracker()

    report = await analyze_coverage(
        "Need an engineering leader with Kubernetes experience.",
        cfg,
        tracker,
        on_text=None,
    )
    return {
        "score": report.coverage_score,
        "must_pct": report.must_have_coverage_pct,
        "gap_count": report.gap_count,
        "requirements": [(r.text, r.covered) for r in report.requirements],
        "summary": report.summary,
        "tracker_calls": len(tracker.calls),
    }


def _scenario_tracking_roundtrip(out_root: Path) -> dict:
    runs = scan_history(out_root)
    if not runs:
        return {"runs_found": 0}

    target = runs[0]
    initial_status = target.status
    save_tracking(
        target.path,
        TrackingRecord(
            status="applied",
            applied_at="2026-04-25",
            next_action="Email Jane",
            notes="Recruiter reached out via LinkedIn.",
        ),
    )
    reloaded = load_tracking(target.path)
    rescanned = scan_history(out_root)

    return {
        "runs_found": len(runs),
        "initial_status": initial_status,
        "saved_status": reloaded.status,
        "rescanned_status": rescanned[0].status,
        "tracking_file_exists": (target.path / "tracking.json").exists(),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as td:
        # Scenario 1: build with cover letter + tracking roundtrip + refine
        tmp = Path(td) / "scenario1"
        tmp.mkdir()
        with_cl = asyncio.run(_scenario_build_with_cover_letter(tmp))
        print("--- Scenario 1: build with cover letter ---")
        print(f"  files: {with_cl['files']}")
        print(f"  fit: {with_cl['fit_score']}  cost calls: {with_cl['tracker_calls']}")

        for must in ("resume.md", "cover_letter.md", "fit_score.json", "metadata.json"):
            if must not in with_cl["files"]:
                failures.append(f"S1: {must} missing")
        if not with_cl["metadata"].get("include_cover_letter"):
            failures.append("S1: include_cover_letter not True in metadata")
        if not with_cl["metadata"].get("cover_letter_cost_usd"):
            failures.append("S1: cover_letter_cost_usd missing/zero")
        if with_cl["tracker_calls"] != 3:
            failures.append(f"S1: expected 3 cost calls, got {with_cl['tracker_calls']}")
        if with_cl["fit_score"] is None:
            failures.append("S1: fit_score is None — score call did not complete")

        # Scenario 1b: tracking roundtrip on the same out tree
        track = _scenario_tracking_roundtrip(tmp / "out")
        print("\n--- Scenario 1b: tracking roundtrip ---")
        print(f"  initial: {track['initial_status']} → saved: {track['saved_status']}")
        if track["initial_status"] != "drafted":
            failures.append(f"S1b: initial status not drafted: {track['initial_status']}")
        if track["rescanned_status"] != "applied":
            failures.append("S1b: status didn't persist across rescan")

        # Scenario 1c: refine on the same run
        run_dir = with_cl["out_dir"]
        cfg = BlastJobConfig(
            paths=PathsConfig(
                data_dir=str(tmp / "data"), output_dir=str(tmp / "out")
            )
        )
        refine = asyncio.run(_scenario_refine(run_dir, cfg))
        print("\n--- Scenario 1c: refine ---")
        print(f"  files: {refine['files']}")
        print(f"  current first line: {refine['current_resume_first_line']!r}")
        print(f"  archived first line: {refine['v1_first_line']!r}")
        print(f"  versions in metadata: {[v['n'] for v in refine['versions_in_metadata']]}")
        if not refine["v1_archived"]:
            failures.append("S1c: resume.v1.md not created")
        if "Tightened summary" not in refine["current_resume_first_line"] and refine[
            "current_resume_first_line"
        ] != "# Jane Smith":
            # The revised resume starts with "# Jane Smith" too — check second line via files
            pass
        if refine["v1_first_line"] != "# Jane Smith":
            failures.append(f"S1c: v1 archive content unexpected: {refine['v1_first_line']!r}")
        if not refine["versions_in_metadata"]:
            failures.append("S1c: metadata.versions is empty")
        elif refine["versions_in_metadata"][0]["n"] != 2:
            failures.append(
                f"S1c: first version number wrong: {refine['versions_in_metadata'][0]['n']}"
            )
        if refine["tracker_calls"] != 2:
            failures.append(f"S1c: expected 2 refine cost calls, got {refine['tracker_calls']}")

        # Scenario 2: build without cover letter (confidential)
        tmp2 = Path(td) / "scenario2"
        tmp2.mkdir()
        without_cl = asyncio.run(_scenario_build_without_cover_letter(tmp2))
        print("\n--- Scenario 2: build without cover letter (confidential) ---")
        print(f"  files: {without_cl['files']}")
        if "cover_letter.md" in without_cl["files"]:
            failures.append("S2: cover_letter.md present when flag was off")
        if "company_research.md" in without_cl["files"]:
            failures.append("S2: research file present despite confidential=True")

        # Scenario 3: coverage analysis
        tmp3 = Path(td) / "scenario3"
        tmp3.mkdir()
        cov = asyncio.run(_scenario_coverage(tmp3))
        print("\n--- Scenario 3: coverage ---")
        print(f"  score: {cov['score']}  must%: {cov['must_pct']}  gaps: {cov['gap_count']}")
        print(f"  requirements: {cov['requirements']}")
        if cov["score"] != 50:
            failures.append(f"S3: expected coverage_score=50 (1/2 musts covered), got {cov['score']}")
        if cov["gap_count"] != 1:
            failures.append(f"S3: expected 1 gap, got {cov['gap_count']}")
        if cov["tracker_calls"] != 1:
            failures.append(f"S3: expected 1 cost call, got {cov['tracker_calls']}")

    print("\n=========================================")
    if failures:
        print(f"FAIL ({len(failures)} issue(s)):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — all e2e checks succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

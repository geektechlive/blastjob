"""End-to-end smoke test for Phase 1 (tracking) + Phase 2 (cover letter).

Mocks the LLM stream so we can exercise the real run_build pipeline + file writes
+ tracking flow without an API key.

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
from blastjob.core.history import scan_history
from blastjob.core.tracking import load_tracking, save_tracking
from blastjob.llm.cost import CostTracker
from blastjob.models.config import BlastJobConfig, PathsConfig
from blastjob.models.tracking import TrackingRecord


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


def _make_stream_factory():
    """Return a make_stream replacement that cycles through resume → cover → score."""
    call_count = {"n": 0}
    sequences = [
        [_RESUME_MD],
        [_COVER_LETTER_MD],
        [_FIT_SCORE_JSON],
    ]

    async def factory(*_a, **_kw):
        seq = sequences[call_count["n"] % len(sequences)]
        call_count["n"] += 1
        return _Stream(seq)

    return factory


async def _fake_research(*_a, **_kw):
    return "## Overview\n\nWidgetCo makes widgets.\n"


async def _scenario_with_cover_letter(tmp: Path) -> dict:
    data_dir = tmp / "data"
    data_dir.mkdir()
    (data_dir / "work_history.md").write_text(
        "## Acme Corp — Senior Engineer (2020-Present)\n\n"
        "- Led team of 8 engineers to ship the payments platform\n"
    )
    (data_dir / "templates").mkdir()
    (data_dir / "templates" / "standard.md").write_text("# {{NAME}}\n\n## Experience")

    cfg = BlastJobConfig(
        paths=PathsConfig(data_dir=str(data_dir), output_dir=str(tmp / "out"))
    )
    tracker = CostTracker()

    import blastjob.core.build as build_mod

    build_mod.make_stream = _make_stream_factory()
    build_mod.make_research_call = _fake_research

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


async def _scenario_without_cover_letter(tmp: Path) -> dict:
    data_dir = tmp / "data"
    data_dir.mkdir()
    (data_dir / "work_history.md").write_text("## Acme — Engineer\n\n- Did things\n")
    (data_dir / "templates").mkdir()
    (data_dir / "templates" / "standard.md").write_text("# {{NAME}}")

    cfg = BlastJobConfig(
        paths=PathsConfig(data_dir=str(data_dir), output_dir=str(tmp / "out"))
    )
    tracker = CostTracker()

    import blastjob.core.build as build_mod

    build_mod.make_stream = _make_stream_factory()
    build_mod.make_research_call = _fake_research

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
            next_action_due="2026-04-28",
            notes="Recruiter reached out via LinkedIn.",
        ),
    )

    reloaded = load_tracking(target.path)
    rescanned = scan_history(out_root)

    return {
        "runs_found": len(runs),
        "initial_status": initial_status,
        "saved_status": reloaded.status,
        "saved_next_action": reloaded.next_action,
        "rescanned_status": rescanned[0].status,
        "rescanned_applied_at": rescanned[0].applied_at,
        "tracking_file_exists": (target.path / "tracking.json").exists(),
    }


def main() -> int:
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "scenario1"
        tmp.mkdir()
        with_cl = asyncio.run(_scenario_with_cover_letter(tmp))

        print("--- Scenario 1: build with cover letter ---")
        print(f"  files: {with_cl['files']}")
        print(f"  fit_score: {with_cl['fit_score']}")
        print(f"  cost calls: {with_cl['tracker_calls']}")
        print(f"  total cost: ${with_cl['tracker_total_cost']:.6f}")

        if "resume.md" not in with_cl["files"]:
            failures.append("S1: resume.md missing")
        if "cover_letter.md" not in with_cl["files"]:
            failures.append("S1: cover_letter.md missing")
        if "metadata.json" not in with_cl["files"]:
            failures.append("S1: metadata.json missing")
        if "fit_score.json" not in with_cl["files"]:
            failures.append("S1: fit_score.json missing")
        if with_cl["metadata"].get("include_cover_letter") is not True:
            failures.append("S1: metadata.include_cover_letter not True")
        if not with_cl["metadata"].get("cover_letter_cost_usd"):
            failures.append("S1: metadata.cover_letter_cost_usd missing/zero")
        if with_cl["tracker_calls"] != 3:
            failures.append(
                f"S1: expected 3 cost calls (resume + cover + score), got {with_cl['tracker_calls']}"
            )

        # Tracking roundtrip on the same output tree
        track = _scenario_tracking_roundtrip(tmp / "out")
        print("\n--- Scenario 1b: tracking roundtrip ---")
        print(f"  initial status: {track['initial_status']}")
        print(f"  after save: {track['saved_status']} (next: {track['saved_next_action']!r})")
        print(f"  after rescan: {track['rescanned_status']}, applied {track['rescanned_applied_at']}")

        if track["initial_status"] != "drafted":
            failures.append(f"S1b: expected initial drafted, got {track['initial_status']}")
        if track["saved_status"] != "applied":
            failures.append(f"S1b: save not persisted, got {track['saved_status']}")
        if track["rescanned_status"] != "applied":
            failures.append(f"S1b: rescan didn't pick up new status")
        if track["rescanned_applied_at"] != "2026-04-25":
            failures.append(f"S1b: applied_at not preserved")
        if not track["tracking_file_exists"]:
            failures.append("S1b: tracking.json not on disk")

        tmp2 = Path(td) / "scenario2"
        tmp2.mkdir()
        without_cl = asyncio.run(_scenario_without_cover_letter(tmp2))

        print("\n--- Scenario 2: build without cover letter (confidential) ---")
        print(f"  files: {without_cl['files']}")

        if "cover_letter.md" in without_cl["files"]:
            failures.append("S2: cover_letter.md present when flag was off")
        if without_cl["metadata"].get("include_cover_letter") is not False:
            failures.append("S2: metadata.include_cover_letter not False")
        if without_cl["metadata"].get("cover_letter_cost_usd") is not None:
            failures.append("S2: metadata.cover_letter_cost_usd should be null")
        if "company_research.md" in without_cl["files"]:
            failures.append("S2: company_research.md present despite confidential=True")

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

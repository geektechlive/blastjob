"""Resume refinement pipeline — single-shot revision with version rotation."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from blastjob import config as cfg_mod
from blastjob.core.scoring import render_score_md, score_resume
from blastjob.exporters import ats, docx, pdf
from blastjob.llm import caching, prompts
from blastjob.llm.cost import CostTracker, cost_from_usage
from blastjob.llm.providers import make_stream
from blastjob.models.config import BlastJobConfig
from blastjob.models.fit_score import FitScore

_VERSION_FILE_RE = re.compile(r"^resume\.v(\d+)\.md$")


def _highest_archived_version(run_dir: Path) -> int:
    """Highest N across resume.v{N}.md files, or 0 if none exist."""
    highest = 0
    for p in run_dir.iterdir():
        m = _VERSION_FILE_RE.match(p.name)
        if m:
            highest = max(highest, int(m.group(1)))
    return highest


def _next_version_number(run_dir: Path) -> int:
    """Version number for the new resume.md after rotating the current one.

    Original unsuffixed resume.md is implicitly v1. After it's archived to
    resume.v1.md the new resume.md is v2; archived to v2.md it's v3, etc.
    """
    return _highest_archived_version(run_dir) + 2


def _read_jd_body(jd_path: Path) -> str:
    """Strip the 'Company / Role' header that run_build prepends to job_description.md."""
    if not jd_path.exists():
        return ""
    content = jd_path.read_text(encoding="utf-8")
    parts = content.split("\n\n", 2)
    return parts[2] if len(parts) >= 3 else content


async def run_refine(
    run_dir: Path,
    feedback: str,
    app_config: BlastJobConfig,
    cost_tracker: CostTracker,
    on_text: Callable[[str], None] | None = None,
) -> tuple[Path, FitScore | None]:
    """Generate a revised resume in `run_dir`, rotating the previous version.

    Returns (new resume.md path, updated fit score). Raises FileNotFoundError if
    `resume.md` or work_history.md is missing.
    """
    resume_path = run_dir / "resume.md"
    if not resume_path.exists():
        raise FileNotFoundError(f"No resume.md in {run_dir}")

    data_path = cfg_mod.data_dir(app_config)
    work_history_path = data_path / "work_history.md"
    if not work_history_path.exists():
        raise FileNotFoundError("No work history found.")

    if not feedback.strip():
        raise ValueError("Feedback is empty — provide a revision instruction.")

    work_history_md = work_history_path.read_text(encoding="utf-8")
    current_resume_md = resume_path.read_text(encoding="utf-8")
    jd = _read_jd_body(run_dir / "job_description.md")

    if on_text:
        on_text("\nRefining resume...\n\n")

    system, user_content = caching.build_refine_messages(
        prompts.REFINE_SYSTEM, work_history_md, current_resume_md, feedback, jd
    )

    revised_md = ""
    stream = await make_stream(
        app_config, system, [{"role": "user", "content": user_content}], max_tokens=4096
    )
    async with stream:
        async for text in stream.text_stream:
            revised_md += text
            if on_text:
                on_text(text)
        final = await stream.get_final_message()

    if not revised_md.strip():
        raise ValueError("Refinement returned empty content — nothing to write.")

    call_cost = cost_from_usage(final.usage, app_config.pricing)
    cost_tracker.record(call_cost)

    # Rotate: archive current resume.md as resume.v{prior_n}.md, then write new
    prior_n = _highest_archived_version(run_dir) + 1
    archived = run_dir / f"resume.v{prior_n}.md"
    resume_path.rename(archived)
    resume_path.write_text(revised_md, encoding="utf-8")

    # Re-export the new version into whatever formats the original run produced
    formats = _formats_from_metadata(run_dir)
    if "pdf" in formats:
        try:
            pdf.write(revised_md, run_dir)
        except Exception as e:
            if on_text:
                on_text(f"\nPDF export failed: {e}\n")
    if "docx" in formats:
        try:
            docx.write(revised_md, run_dir)
        except Exception as e:
            if on_text:
                on_text(f"\nDOCX export failed: {e}\n")
    if "ats" in formats:
        ats.write(revised_md, run_dir)

    # Re-score the revision
    fit_score = await score_resume(
        revised_md, work_history_md, jd, app_config, cost_tracker, on_text
    )
    if fit_score is not None:
        (run_dir / "fit_score.json").write_text(
            fit_score.model_dump_json(indent=2), encoding="utf-8"
        )
        (run_dir / "fit_score.md").write_text(render_score_md(fit_score), encoding="utf-8")

    new_version_n = prior_n + 1  # the version we just wrote to resume.md
    _append_version_to_metadata(
        run_dir,
        version_n=new_version_n,
        feedback=feedback,
        cost=call_cost,
        fit_score=fit_score.overall_score if fit_score else None,
    )

    if on_text:
        on_text(f"\n\nRevision v{new_version_n} saved.\n")
    return resume_path, fit_score


def _formats_from_metadata(run_dir: Path) -> set[str]:
    meta_path = run_dir / "metadata.json"
    if not meta_path.exists():
        return set()
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return set(data.get("formats", []))


def _append_version_to_metadata(
    run_dir: Path,
    version_n: int,
    feedback: str,
    cost,
    fit_score: int | None,
) -> None:
    meta_path = run_dir / "metadata.json"
    if not meta_path.exists():
        return
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    versions = data.get("versions", [])
    versions.append(
        {
            "n": version_n,
            "timestamp": datetime.now().isoformat(),
            "feedback": feedback,
            "cost_usd": round(cost.cost_usd, 6),
            "input_tokens": cost.input_tokens,
            "output_tokens": cost.output_tokens,
            "fit_score": fit_score,
        }
    )
    data["versions"] = versions
    meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

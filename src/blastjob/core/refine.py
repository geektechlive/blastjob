"""Resume refinement pipeline — single-shot revision with version rotation.

Handles both filename conventions found on disk:
  - legacy: bare `resume.md` / `job_description.md` / `metadata.json` /
    `fit_score.json` / `fit_score.md` (one build per run_dir; still produced
    by the legacy TUI build path).
  - current (core/build.py, the web cockpit's only build path): the resume is
    `{Person}_Resume_-_{Co}_{Role}.md` and the sidecars are role-slug-prefixed
    (`{role_slug}.job_description.md`, `.metadata.json`, `.fit_score.json`,
    `.fit_score.md`) since one flat {company-slug}-{date}/ folder can hold the
    sidecars for more than one role tailored to that company on the same day.
"""

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

_METADATA_SUFFIX = "metadata.json"
# Archived rotations are named "{stem}.v{N}.md" regardless of convention.
_VERSION_SUFFIX_RE = re.compile(r"\.v\d+\.md$")


def _highest_archived_version(run_dir: Path, stem: str = "resume") -> int:
    """Highest N across `{stem}.v{N}.md` files in run_dir, or 0 if none exist."""
    pattern = re.compile(rf"^{re.escape(stem)}\.v(\d+)\.md$")
    highest = 0
    for p in run_dir.iterdir():
        m = pattern.match(p.name)
        if m:
            highest = max(highest, int(m.group(1)))
    return highest


def _next_version_number(run_dir: Path, stem: str = "resume") -> int:
    """Version number for the new resume after rotating the current one.

    Kept for backward compatibility: tests/test_refine.py (not owned by this
    change) imports and exercises this directly. run_refine itself computes
    the same value inline (prior_n + 1, where prior_n = _highest_archived_version + 1)
    rather than calling this, since it also needs the intermediate prior_n to
    name the archived file.
    """
    return _highest_archived_version(run_dir, stem) + 2


def _find_resume_path(run_dir: Path) -> Path:
    """Locate the resume to refine.

    Legacy bare `resume.md` wins if present. Otherwise, find the newest
    current-convention resume file — build.py's stem always contains
    "_Resume_-_" — skipping ATS exports (`..._ATS.md`), cover letters (a
    different "_Cover_Letter_-_" stem), and already-archived `.v{N}.md`
    rotations. "Newest" picks the most recently built role when a folder
    holds more than one.
    """
    legacy = run_dir / "resume.md"
    if legacy.exists():
        return legacy
    candidates = [
        p
        for p in run_dir.glob("*_Resume_-_*.md")
        if not p.name.endswith("_ATS.md") and not _VERSION_SUFFIX_RE.search(p.name)
    ]
    if not candidates:
        raise FileNotFoundError(f"No resume found in {run_dir}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _find_metadata_path(run_dir: Path) -> Path | None:
    """Locate metadata.json: legacy bare name, else the newest role-prefixed one."""
    legacy = run_dir / _METADATA_SUFFIX
    if legacy.exists():
        return legacy
    candidates = list(run_dir.glob(f"*.{_METADATA_SUFFIX}"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _sidecar_prefix(metadata_path: Path | None) -> str:
    """The role-slug prefix shared by job_description/fit_score/metadata sidecars.

    '' for the legacy bare convention, '{role_slug}.' for the current one.
    """
    if metadata_path is None or metadata_path.name == _METADATA_SUFFIX:
        return ""
    return metadata_path.name[: -len(_METADATA_SUFFIX)]


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

    Returns (new resume path, updated fit score). Raises FileNotFoundError if
    no resume or work_history.md is found.
    """
    resume_path = _find_resume_path(run_dir)
    resume_stem = resume_path.name[: -len(".md")]

    metadata_path = _find_metadata_path(run_dir)
    prefix = _sidecar_prefix(metadata_path)

    data_path = cfg_mod.data_dir(app_config)
    work_history_path = data_path / "work_history.md"
    if not work_history_path.exists():
        raise FileNotFoundError("No work history found.")

    if not feedback.strip():
        raise ValueError("Feedback is empty — provide a revision instruction.")

    work_history_md = work_history_path.read_text(encoding="utf-8")
    current_resume_md = resume_path.read_text(encoding="utf-8")
    jd = _read_jd_body(run_dir / f"{prefix}job_description.md")

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

    # Rotate: archive current resume as {resume_stem}.v{prior_n}.md, then write new
    prior_n = _highest_archived_version(run_dir, resume_stem) + 1
    archived = run_dir / f"{resume_stem}.v{prior_n}.md"
    resume_path.rename(archived)
    resume_path.write_text(revised_md, encoding="utf-8")

    # Re-export the new version into whatever formats the original run produced
    formats = _formats_from_metadata(metadata_path)
    if "pdf" in formats:
        try:
            pdf.write(revised_md, run_dir, stem=resume_stem)
        except Exception as e:
            if on_text:
                on_text(f"\nPDF export failed: {e}\n")
    if "docx" in formats:
        try:
            docx.write(revised_md, run_dir, stem=resume_stem)
        except Exception as e:
            if on_text:
                on_text(f"\nDOCX export failed: {e}\n")
    if "ats" in formats:
        ats.write(revised_md, run_dir, stem=f"{resume_stem}_ATS")

    # Re-score the revision
    fit_score = await score_resume(
        revised_md, work_history_md, jd, app_config, cost_tracker, on_text
    )
    if fit_score is not None:
        (run_dir / f"{prefix}fit_score.json").write_text(
            fit_score.model_dump_json(indent=2), encoding="utf-8"
        )
        (run_dir / f"{prefix}fit_score.md").write_text(render_score_md(fit_score), encoding="utf-8")

    new_version_n = prior_n + 1  # the version we just wrote to resume_path
    _append_version_to_metadata(
        metadata_path,
        version_n=new_version_n,
        feedback=feedback,
        cost=call_cost,
        fit_score=fit_score.overall_score if fit_score else None,
    )

    if on_text:
        on_text(f"\n\nRevision v{new_version_n} saved.\n")
    return resume_path, fit_score


def _formats_from_metadata(metadata_path: Path | None) -> set[str]:
    if metadata_path is None or not metadata_path.exists():
        return set()
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return set(data.get("formats", []))


def _append_version_to_metadata(
    metadata_path: Path | None,
    version_n: int,
    feedback: str,
    cost,
    fit_score: int | None,
) -> None:
    if metadata_path is None or not metadata_path.exists():
        return
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
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
    metadata_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

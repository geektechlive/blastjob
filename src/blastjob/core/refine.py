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

import dataclasses
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from blastjob import config as cfg_mod
from blastjob.core import packet_verify, refine_files
from blastjob.core.cover_letter_score import (
    render_cover_letter_score_md,
    score_cover_letter,
)
from blastjob.core.packet_report import (
    Artifact,
    CoverageGap,
    Finding,
    GateVerdict,
    PacketState,
    Severity,
)
from blastjob.core.paths import document_tokens_regex, resolve_role_slug
from blastjob.core.research_facts import distill_research
from blastjob.core.scoring import render_score_md, score_resume
from blastjob.exporters import ats, docx, markdown, pdf
from blastjob.llm import caching, prompts
from blastjob.llm.cost import CostTracker, cost_from_usage
from blastjob.llm.providers import make_stream
from blastjob.models.config import BlastJobConfig
from blastjob.models.fit_score import FitScore

log = logging.getLogger(__name__)

_METADATA_SUFFIX = "metadata.json"
# Archived rotations are named "{stem}.v{N}.md" regardless of convention.
_VERSION_SUFFIX_RE = re.compile(r"\.v\d+\.md$")
_QUARANTINE_DIR = "quarantine"


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


@dataclass(frozen=True)
class Target:
    """The one resume a refinement rewrites, and the sidecars that belong to it."""

    resume_path: Path
    stem: str
    slug: str  # role slug; "" for a legacy per-role folder
    prefix: str  # sidecar prefix: "{slug}." or ""
    metadata_path: Path | None
    in_quarantine: bool

    @property
    def cover_stem(self) -> str:
        if "_Resume_-_" in self.stem:
            return self.stem.replace("_Resume_-_", "_Cover_Letter_-_", 1)
        return "cover_letter"


@dataclass(frozen=True)
class RefineResult:
    """What a refinement did, for the caller to show."""

    resume_path: Path
    fit_score: FitScore | None
    state: str  # packet state after the gate: ready / quarantined / verification_failed
    reasons: tuple[str, ...] = ()
    held: bool = False


def _find_in(folder: Path, pattern: re.Pattern[str]) -> Path | None:
    if not folder.is_dir():
        return None
    found = [p for p in folder.glob("*.md") if pattern.match(p.name)]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def locate(run_dir: Path, company: str | None, role: str | None) -> Target:
    """Find the resume to refine.

    With a company and role (the job's own), the file is picked by the role's
    naming rules and its `metadata.json`, in the packet folder or, failing that,
    in `quarantine/`: a held resume is a legitimate thing to revise. Never "the
    newest resume in the folder", which in a shared company-day folder can be
    another role's. Without them (legacy callers), the old newest-wins search.
    """
    legacy = (run_dir / "resume.md").exists() or (run_dir / _METADATA_SUFFIX).exists()
    if company is None or role is None or legacy:
        path = _find_resume_path(run_dir)
        meta = _find_metadata_path(run_dir)
        prefix = _sidecar_prefix(meta)
        return Target(path, path.name[: -len(".md")], prefix.rstrip("."), prefix, meta, False)

    slug = resolve_role_slug(run_dir, role)
    meta_path = run_dir / f"{slug}.{_METADATA_SUFFIX}"
    built_company, built_role = company, role
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        built_company = str(data.get("company") or company)
        built_role = str(data.get("role") or role)
    except (OSError, ValueError):
        pass
    tokens = document_tokens_regex(built_company, built_role)
    pattern = re.compile(rf"^.+_Resume_-_{tokens}\.md$")
    for folder, held in ((run_dir, False), (run_dir / _QUARANTINE_DIR, True)):
        found = _find_in(folder, pattern)
        if found is not None:
            return Target(
                found,
                found.name[: -len(".md")],
                slug,
                f"{slug}.",
                meta_path if meta_path.exists() else None,
                held,
            )
    raise FileNotFoundError(f"No resume found for {built_role} at {built_company} in {run_dir}")


def _read_text(*candidates: Path) -> str:
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return ""


def _read_json_object(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _carried_findings(old_report: dict | None) -> list[Finding] | None:
    """The cover letter's findings from the packet's previous report, or None
    when there is no readable report to carry them from."""
    if old_report is None or not isinstance(old_report.get("findings"), list):
        return None
    out: list[Finding] = []
    for raw in old_report["findings"]:
        if not isinstance(raw, dict) or raw.get("artifact") != Artifact.COVER_LETTER.value:
            continue
        try:
            out.append(
                Finding(
                    severity=Severity(str(raw.get("severity"))),
                    code=str(raw.get("code") or ""),
                    message=str(raw.get("message") or ""),
                    artifact=Artifact.COVER_LETTER,
                    line=raw.get("line") if isinstance(raw.get("line"), int) else None,
                    evidence=str(raw.get("evidence") or ""),
                    source=str(raw.get("source") or ""),
                )
            )
        except ValueError:
            continue
    return out


def _old_gaps(old_report: dict | None) -> list[CoverageGap]:
    """Coverage gaps depend on the JD and the resume text; refine does not recompute
    them, so the previous report's are carried rather than silently dropped."""
    gaps = (old_report or {}).get("coverage_gaps")
    if not isinstance(gaps, list):
        return []
    return [
        CoverageGap(
            requirement=str(g.get("requirement") or ""),
            history_quote=str(g.get("history_quote") or ""),
            priority=str(g.get("priority") or "must"),
        )
        for g in gaps
        if isinstance(g, dict)
    ]


def _letter_changed(letter_path: Path | None, report_path: Path) -> bool:
    """True when the letter is newer than the report that last verified it (or
    there is no report). Refine never edits a letter, so normally False."""
    if letter_path is None or not report_path.is_file():
        return letter_path is not None
    return letter_path.stat().st_mtime > report_path.stat().st_mtime


async def _rescore_letter(
    letter_md: str,
    *,
    run_dir: Path,
    target: Target,
    work_history_md: str,
    app_config: BlastJobConfig,
    cost_tracker: CostTracker,
    on_text: Callable[[str], None] | None,
):
    """Score a letter that changed (or was never verified), and store the score."""
    research_md = _read_text(run_dir / f"{target.prefix}company_research.md")
    facts = (
        await distill_research(research_md, company="", cfg=app_config, cost_tracker=cost_tracker)
        if research_md
        else []
    )
    score = await score_cover_letter(
        letter_md,
        work_history_md=work_history_md,
        research_md=research_md,
        cfg=app_config,
        cost_tracker=cost_tracker,
        facts=facts,
        on_text=on_text,
    )
    packet_verify.write_text_atomic(
        run_dir / f"{target.prefix}cover_letter_score.json", score.model_dump_json(indent=2)
    )
    packet_verify.write_text_atomic(
        run_dir / f"{target.prefix}cover_letter_score.md", render_cover_letter_score_md(score)
    )
    return score, facts


def _export_to_stage(stage: Path, revised_md: str, stem: str, formats: set[str], on_text) -> None:
    """Write the revised resume into every format the original run produced."""
    markdown.write(revised_md, stage, stem=stem)
    for fmt, writer, out_stem in (
        ("pdf", pdf.write, stem),
        ("docx", docx.write, stem),
        ("ats", ats.write, f"{stem}_ATS"),
    ):
        if fmt not in formats:
            continue
        try:
            writer(revised_md, stage, stem=out_stem)
        except Exception as e:  # noqa: BLE001 — an export failure never loses the revision
            if on_text:
                on_text(f"\n{fmt.upper()} export failed: {e}\n")


async def run_refine(
    run_dir: Path,
    feedback: str,
    app_config: BlastJobConfig,
    cost_tracker: CostTracker,
    on_text: Callable[[str], None] | None = None,
    *,
    company: str | None = None,
    role: str | None = None,
) -> tuple[Path, FitScore | None]:
    """Generate a revised resume in `run_dir`, rotating the previous version.

    Returns (new resume path, updated fit score). See `run_refine_detailed`.
    """
    result = await run_refine_detailed(
        run_dir, feedback, app_config, cost_tracker, on_text, company=company, role=role
    )
    return result.resume_path, result.fit_score


async def run_refine_detailed(
    run_dir: Path,
    feedback: str,
    app_config: BlastJobConfig,
    cost_tracker: CostTracker,
    on_text: Callable[[str], None] | None = None,
    *,
    company: str | None = None,
    role: str | None = None,
) -> RefineResult:
    """Revise one role's resume and RE-VERIFY it exactly as a fresh build would.

    Raises FileNotFoundError if no resume (for this role) or work_history.md is
    found, `RefineBusyError` while another refinement of the same resume runs,
    and `PacketNotQuarantinedError` when a failing revision could not be moved out
    of the public folder.

    A revision is not trusted because the user asked for it: the deterministic
    checks, the gate and quarantine run again, the resume is re-scored (one paid
    call), and the cover letter is re-scored only if it changed. A revision that
    fails the gate is held like a build's, never left downloadable.
    """
    target = locate(run_dir, company, role)

    work_history_path = cfg_mod.data_dir(app_config) / "work_history.md"
    if not work_history_path.exists():
        raise FileNotFoundError("No work history found.")
    if not feedback.strip():
        raise ValueError("Feedback is empty — provide a revision instruction.")

    with refine_files.lease(run_dir, target.stem):
        return await _refine_locked(
            run_dir, target, feedback, app_config, cost_tracker, on_text, work_history_path
        )


async def _refine_locked(
    run_dir: Path,
    target: Target,
    feedback: str,
    app_config: BlastJobConfig,
    cost_tracker: CostTracker,
    on_text: Callable[[str], None] | None,
    work_history_path: Path,
) -> RefineResult:
    stem, prefix = target.stem, target.prefix
    work_history_md = work_history_path.read_text(encoding="utf-8")
    current_resume_md = target.resume_path.read_text(encoding="utf-8")
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

    formats = _formats_from_metadata(target.metadata_path)
    stage = refine_files.new_stage(run_dir)
    keep_stage = False  # set when publishing fails: the staged revision is then evidence
    try:
        _export_to_stage(stage, revised_md, stem, formats, on_text)

        # Re-score the revision (a scorer failure is recorded as skipped by verify).
        fit_score = await score_resume(
            revised_md, work_history_md, jd, app_config, cost_tracker, on_text
        )

        report_file = packet_verify.report_path(run_dir, target.slug)
        old_report = _read_json_object(report_file)
        letter_path = _first_existing(
            run_dir / f"{target.cover_stem}.md",
            run_dir / _QUARANTINE_DIR / f"{target.cover_stem}.md",
        )
        letter_md = _read_text(letter_path) if letter_path else ""
        # A letter the previous report left unscored is scored now, not carried:
        # carrying would erase the "not checked" note and read as clean.
        changed = bool(letter_md) and (
            _letter_changed(letter_path, report_file) or _letter_was_unscored(old_report)
        )
        letter_score, facts, carried = None, [], None
        if changed:
            letter_score, facts = await _rescore_letter(
                letter_md,
                run_dir=run_dir,
                target=target,
                work_history_md=work_history_md,
                app_config=app_config,
                cost_tracker=cost_tracker,
                on_text=on_text,
            )
        elif letter_md:
            carried = _carried_findings(old_report)

        report = packet_verify.verify_packet(
            resume_md=revised_md,
            cover_letter_md=letter_md,
            work_history_md=work_history_md,
            job_description=jd,
            out_dir=stage,
            resume_stem=stem,
            fit_score=fit_score,
            cover_letter_score=letter_score,
            coverage_gaps=_old_gaps(old_report),
            company_facts=facts,
            carried_cover_findings=carried,
            carried_checks_skipped=_carried_skips(old_report, letter_md),
        )
        report = dataclasses.replace(
            report,
            artifact_dir=str(run_dir),
            cover_letter_groundedness=_carried_letter_score(report, old_report, carried),
        )

        waived, override = _prior_override(old_report, carried)
        verdict = packet_verify.gate_and_report(
            report,
            run_dir,
            target.slug,
            app_config=app_config,
            resume_stem=stem,
            cover_stem=target.cover_stem,
            will_publish=refine_files.stem_names(stem),
            waived_artifacts=waived,
            override=override,
        )
        # A crash in verification is not a verdict: whatever was held stays held.
        holding = verdict or _kept_hold(old_report, target)

        prior_n = refine_files.highest_version(run_dir, stem) + 1
        try:
            refine_files.publish(
                stage, run_dir, stem, prior_n=prior_n, source_in_quarantine=target.in_quarantine
            )
        except OSError:
            keep_stage = True
            log.error("refine: publish failed; the staged revision is kept in %s", stage)
            raise
        if holding is None or Artifact.RESUME not in holding.quarantined_artifacts:
            refine_files.retire_held(run_dir, stem)
    finally:
        if not keep_stage:
            refine_files.remove_stage(stage)

    # Hold FIRST: nothing below may stand between a failing revision and quarantine.
    hold_failure = packet_verify.hold_documents(
        run_dir, holding, target.slug, resume_stem=stem, cover_stem=target.cover_stem
    )
    _write_sidecars(target, run_dir, fit_score, prior_n, feedback, call_cost)
    if hold_failure:
        raise packet_verify.PacketNotQuarantinedError(hold_failure, run_dir)

    held = holding is not None and Artifact.RESUME in holding.quarantined_artifacts
    path = (run_dir / _QUARANTINE_DIR if held else run_dir) / f"{stem}.md"
    state = verdict.state.value if verdict is not None else "verification_failed"
    if on_text:
        on_text(f"\n\nRevision v{prior_n + 1} saved. Packet check: {state}.\n")
    reasons = tuple(verdict.reasons) if verdict is not None else ()
    return RefineResult(path, fit_score, state, reasons, held)


def _write_sidecars(
    target: Target, run_dir: Path, fit_score: FitScore | None, prior_n: int, feedback: str, cost
) -> None:
    """Fit score + version log. The revision is already published and held as the
    gate decided; a failure here costs bookkeeping, never the hold."""
    try:
        _write_fit_score(run_dir, target.prefix, fit_score)
        _append_version_to_metadata(
            target.metadata_path,
            version_n=prior_n + 1,
            feedback=feedback,
            cost=cost,
            fit_score=fit_score.overall_score if fit_score else None,
        )
    except Exception:  # noqa: BLE001 -- fail-soft by design
        log.warning("refine: could not write the sidecars for %s", target.stem, exc_info=True)


def _letter_was_unscored(old_report: dict | None) -> bool:
    skipped = (old_report or {}).get("checks_skipped")
    return isinstance(skipped, list) and packet_verify.SKIPPED_COVER_LETTER_SCORE in skipped


def _carried_skips(old_report: dict | None, letter_md: str) -> tuple[str, ...]:
    """Skip notes a refine cannot resolve: a letter whose generation failed is still
    missing after the resume is revised."""
    skipped = (old_report or {}).get("checks_skipped")
    if letter_md or not isinstance(skipped, list):
        return ()
    return tuple(n for n in skipped if n == packet_verify.SKIPPED_COVER_LETTER_GENERATION)


def _prior_override(old_report: dict | None, carried) -> tuple[frozenset[Artifact], dict | None]:
    """The user's earlier release of the cover letter, while its findings are the
    very ones the user released (carried, so unchanged). A rescored letter has new
    findings, which the user has not seen, so nothing carries."""
    override = (old_report or {}).get("override")
    if carried is None or not isinstance(override, dict):
        return frozenset(), None
    if (old_report or {}).get("state") != PacketState.ACKNOWLEDGED_OVERRIDE.value:
        return frozenset(), None
    return frozenset({Artifact.COVER_LETTER}), override


def _kept_hold(old_report: dict | None, target: Target) -> GateVerdict | None:
    """The hold to keep when verification itself crashed: what the previous report
    held, plus the resume when it was being revised out of quarantine."""
    held: set[Artifact] = set()
    if (old_report or {}).get("state") == PacketState.QUARANTINED.value:
        for name in (old_report or {}).get("quarantined_artifacts") or []:
            try:
                held.add(Artifact(name))
            except ValueError:
                continue
    if target.in_quarantine:
        held.add(Artifact.RESUME)
    if not held:
        return None
    return GateVerdict(
        state=PacketState.QUARANTINED,
        reasons=("Re-verification crashed, so the earlier hold stays in place.",),
        quarantined_artifacts=frozenset(held),
    )


def _first_existing(*paths: Path) -> Path | None:
    return next((p for p in paths if p.is_file()), None)


def _carried_letter_score(report, old_report: dict | None, carried) -> int | None:
    """The letter's groundedness: freshly scored, or the previous report's figure
    when its findings were carried forward."""
    if carried is None:
        return report.cover_letter_groundedness
    value = (old_report or {}).get("cover_letter_groundedness")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _write_fit_score(run_dir: Path, prefix: str, fit_score: FitScore | None) -> None:
    """Store the new score; with none, park the old one rather than leave a
    score that describes the replaced resume beside the new one."""
    json_path = run_dir / f"{prefix}fit_score.json"
    md_path = run_dir / f"{prefix}fit_score.md"
    if fit_score is None:
        for stale in (json_path, md_path):
            if stale.is_file():
                refine_files.park(stale, run_dir / refine_files.REFINE_DIRNAME / "superseded")
        return
    packet_verify.write_text_atomic(json_path, fit_score.model_dump_json(indent=2))
    packet_verify.write_text_atomic(md_path, render_score_md(fit_score))


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
    packet_verify.write_text_atomic(metadata_path, json.dumps(data, indent=2))

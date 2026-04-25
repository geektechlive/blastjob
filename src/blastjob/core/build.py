from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from blastjob import config as cfg_mod
from blastjob.core.paths import make_output_dir
from blastjob.exporters import ats, docx, markdown, pdf
from blastjob.llm import caching, prompts
from blastjob.llm.cost import CostTracker, cost_from_usage
from blastjob.llm.providers import make_research_call, make_stream
from blastjob.models.config import BlastJobConfig
from blastjob.models.fit_score import FitScore


async def run_build(
    company: str,
    role: str,
    job_description: str,
    formats: set[str],
    use_ats: bool,
    app_config: BlastJobConfig,
    cost_tracker: CostTracker,
    on_text: Callable[[str], None] | None = None,
    confidential: bool = False,
    include_cover_letter: bool = False,
) -> tuple[Path, FitScore | None]:
    data_path = cfg_mod.data_dir(app_config)
    out_root = cfg_mod.output_dir(app_config)

    work_history_path = data_path / "work_history.md"
    if not work_history_path.exists():
        raise FileNotFoundError("No work history found. Please ingest files first.")

    if not formats:
        raise ValueError("No output formats selected. Choose at least one format.")

    work_history_md = work_history_path.read_text(encoding="utf-8")
    template_key = "ats.md" if use_ats else "standard.md"
    template_path = data_path / "templates" / template_key
    template_md = template_path.read_text(encoding="utf-8") if template_path.exists() else ""

    # Step 1: Company research (skipped when company is confidential)
    if confidential:
        if on_text:
            on_text("Skipping company research (confidential posting).\n")
        research_md = ""
    else:
        if on_text:
            on_text(f"Researching {company}...\n")
        research_md = await make_research_call(
            app_config, company, app_config.generation.max_web_searches
        )

    # Step 2: Generate resume
    system_prompt = prompts.RESUME_ATS_SYSTEM if use_ats else prompts.RESUME_SYSTEM
    system, user_content = caching.build_resume_messages(
        system_prompt, work_history_md, template_md, job_description, research_md
    )

    if on_text:
        on_text(f"\nGenerating {'ATS' if use_ats else 'standard'} resume...\n\n")

    resume_md = ""
    stream = await make_stream(
        app_config, system, [{"role": "user", "content": user_content}], max_tokens=4096
    )
    async with stream:
        async for text in stream.text_stream:
            resume_md += text
            if on_text:
                on_text(text)
        final = await stream.get_final_message()
    call_cost = cost_from_usage(final.usage, app_config.pricing)
    cost_tracker.record(call_cost)

    if not resume_md.strip():
        raise ValueError("Resume generation returned empty content — nothing to export.")

    # Step 3: Create output folder
    out_dir = make_output_dir(out_root, company, role)

    # Step 4: Export
    if on_text:
        on_text(f"\n\nExporting to {out_dir}...\n")

    if "md" in formats:
        markdown.write(resume_md, out_dir)
    if "pdf" in formats:
        try:
            pdf.write(resume_md, out_dir)
        except Exception as e:
            if on_text:
                on_text(f"PDF export failed: {e}\n")
    if "docx" in formats:
        try:
            docx.write(resume_md, out_dir)
        except Exception as e:
            if on_text:
                on_text(f"DOCX export failed: {e}\n")
    if "ats" in formats:
        ats.write(resume_md, out_dir)

    # Step 5: Sidecars (job description + research)
    (out_dir / "job_description.md").write_text(
        f"# Job Description\n\n**Company:** {company}\n**Role:** {role}\n\n{job_description}",
        encoding="utf-8",
    )
    if research_md:
        (out_dir / "company_research.md").write_text(research_md, encoding="utf-8")

    # Step 6: Cover letter — fail-soft, optional
    cover_letter_cost = None
    if include_cover_letter:
        cover_letter_cost = await _generate_cover_letter(
            resume_md,
            work_history_md,
            job_description,
            research_md,
            formats,
            out_dir,
            app_config,
            cost_tracker,
            on_text,
        )

    # Step 7: Score — fail-soft so resume delivery is unaffected if scoring errors
    fit_score = await _score_resume(
        resume_md, work_history_md, job_description, app_config, cost_tracker, on_text
    )
    if fit_score is not None:
        (out_dir / "fit_score.json").write_text(
            fit_score.model_dump_json(indent=2), encoding="utf-8"
        )
        (out_dir / "fit_score.md").write_text(_render_score_md(fit_score), encoding="utf-8")

    (out_dir / "metadata.json").write_text(
        json.dumps(
            {
                "timestamp": datetime.now().isoformat(),
                "company": company,
                "role": role,
                "confidential": confidential,
                "provider": app_config.llm.provider,
                "cost_usd": round(call_cost.cost_usd, 6),
                "input_tokens": call_cost.input_tokens,
                "output_tokens": call_cost.output_tokens,
                "cache_creation_tokens": call_cost.cache_creation_tokens,
                "cache_read_tokens": call_cost.cache_read_tokens,
                "cache_hit_ratio": round(call_cost.cache_hit_ratio, 3),
                "formats": list(formats),
                "ats_mode": use_ats,
                "fit_score": fit_score.overall_score if fit_score else None,
                "groundedness_score": fit_score.groundedness_score if fit_score else None,
                "unsupported_claims": fit_score.unsupported_count if fit_score else None,
                "include_cover_letter": include_cover_letter,
                "cover_letter_cost_usd": (
                    round(cover_letter_cost.cost_usd, 6) if cover_letter_cost else None
                ),
                "cover_letter_input_tokens": (
                    cover_letter_cost.input_tokens if cover_letter_cost else None
                ),
                "cover_letter_output_tokens": (
                    cover_letter_cost.output_tokens if cover_letter_cost else None
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    if on_text:
        on_text("Done!\n")

    return out_dir, fit_score


async def _generate_cover_letter(
    resume_md: str,
    work_history_md: str,
    job_description: str,
    research_md: str,
    formats: set[str],
    out_dir: Path,
    app_config: BlastJobConfig,
    cost_tracker: CostTracker,
    on_text: Callable[[str], None] | None = None,
):
    """Generate cover letter alongside the resume. Fail-soft."""
    if on_text:
        on_text("\nGenerating cover letter...\n\n")

    try:
        system, user_content = caching.build_cover_letter_messages(
            prompts.COVER_LETTER_SYSTEM,
            work_history_md,
            resume_md,
            job_description,
            research_md,
        )
        cover_md = ""
        stream = await make_stream(
            app_config, system, [{"role": "user", "content": user_content}], max_tokens=2048
        )
        async with stream:
            async for text in stream.text_stream:
                cover_md += text
                if on_text:
                    on_text(text)
            final = await stream.get_final_message()

        if not cover_md.strip():
            if on_text:
                on_text("\nCover letter returned empty — skipping.\n")
            return None

        call_cost = cost_from_usage(final.usage, app_config.pricing)
        cost_tracker.record(call_cost)

        markdown.write(cover_md, out_dir, stem="cover_letter")
        if "pdf" in formats:
            try:
                pdf.write(cover_md, out_dir, stem="cover_letter")
            except Exception as e:
                if on_text:
                    on_text(f"\nCover letter PDF export failed: {e}\n")
        if "docx" in formats:
            try:
                docx.write(cover_md, out_dir, stem="cover_letter")
            except Exception as e:
                if on_text:
                    on_text(f"\nCover letter DOCX export failed: {e}\n")

        if on_text:
            metric = (
                f" · ${call_cost.cost_usd:.4f}"
                f" · {call_cost.input_tokens + call_cost.output_tokens} tokens"
            )
            on_text(f"\n\nCover letter saved.{metric}\n")
        return call_cost

    except Exception as e:
        if on_text:
            on_text(f"\nCover letter skipped: {e}\n")
        return None


async def _score_resume(
    resume_md: str,
    work_history_md: str,
    job_description: str,
    app_config: BlastJobConfig,
    cost_tracker: CostTracker,
    on_text: Callable[[str], None] | None = None,
) -> FitScore | None:
    system, user_content = caching.build_score_messages(
        prompts.FIT_SCORE_SYSTEM, work_history_md, resume_md, job_description
    )
    try:
        if on_text:
            on_text("\nScoring resume fit and groundedness...\n")

        full_text = ""
        stream = await make_stream(
            app_config,
            system,
            [{"role": "user", "content": user_content}],
            max_tokens=4096,
        )
        async with stream:
            async for chunk in stream.text_stream:
                full_text += chunk
            final = await stream.get_final_message()

        score_cost = cost_from_usage(final.usage, app_config.pricing)
        cost_tracker.record(score_cost)

        score = _parse_score(full_text, work_history_md)

        if on_text:
            on_text(
                f"Fit score: {score.overall_score}/100"
                f"  (JD alignment: {score.jd_alignment_score},"
                f" Groundedness: {score.groundedness_score},"
                f" Unsupported claims: {score.unsupported_count})\n"
            )
        return score

    except Exception as e:
        if on_text:
            on_text(f"Scoring skipped: {e}\n")
        return None


def _parse_score(text: str, work_history_md: str) -> FitScore:
    import re

    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ValueError("No JSON object found in scoring response.")
    data = json.loads(match.group())
    score = FitScore.model_validate(data)

    # Python-side verification: downgrade any claim whose evidence_quote is not an
    # actual substring of work_history_md — prevents Claude from fabricating quotes.
    for claim in score.claims:
        if claim.grounded and claim.evidence_quote:
            if claim.evidence_quote not in work_history_md:
                claim.grounded = False

    score.unsupported_count = sum(1 for c in score.claims if not c.grounded)

    if score.claims:
        grounded_pct = int(100 * sum(1 for c in score.claims if c.grounded) / len(score.claims))
        score.groundedness_score = grounded_pct
        score.overall_score = int(grounded_pct * 0.7 + score.jd_alignment_score * 0.3)

    return score


def _render_score_md(score: FitScore) -> str:
    lines = [
        "# Fit Score Report",
        "",
        f"**Overall:** {score.overall_score}/100",
        f"**JD Alignment:** {score.jd_alignment_score}/100",
        f"**Groundedness:** {score.groundedness_score}/100",
        f"**Unsupported claims:** {score.unsupported_count}",
        "",
        f"> {score.summary}",
        "",
        "## Claim Analysis",
        "",
    ]
    for claim in score.claims:
        status = "✓" if claim.grounded else "✗"
        lines.append(f"**{status} {claim.claim_text}**")
        if claim.grounded and claim.evidence_quote:
            lines.append(f'> Evidence: "{claim.evidence_quote}"')
            if claim.source_section:
                lines.append(f"  *(source: {claim.source_section})*")
        else:
            lines.append("  *No supporting evidence found in work history.*")
        lines.append("")
    return "\n".join(lines)

"""Resume fit scoring + groundedness verification.

Extracted from core/build.py so the refine pipeline can rescore without
pulling in the full build module (which would create a circular import).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from blastjob.llm import caching, prompts
from blastjob.llm.cost import CostTracker, cost_from_usage
from blastjob.llm.providers import make_stream
from blastjob.models.config import BlastJobConfig
from blastjob.models.fit_score import FitScore


async def score_resume(
    resume_md: str,
    work_history_md: str,
    job_description: str,
    app_config: BlastJobConfig,
    cost_tracker: CostTracker,
    on_text: Callable[[str], None] | None = None,
) -> FitScore | None:
    """Run a fit score call. Returns None on any failure (fail-soft)."""
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

        score = parse_score(full_text, work_history_md)

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


def parse_score(text: str, work_history_md: str) -> FitScore:
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


def render_score_md(score: FitScore) -> str:
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

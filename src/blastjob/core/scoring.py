"""Resume fit scoring + groundedness verification.

Extracted from core/build.py so the refine pipeline can rescore without
pulling in the full build module (which would create a circular import).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from blastjob.core import resume_dates
from blastjob.llm import caching, prompts
from blastjob.llm.cost import CostTracker, cost_from_usage
from blastjob.llm.jsonio import find_json_value
from blastjob.llm.providers import make_stream
from blastjob.models.config import BlastJobConfig
from blastjob.models.fit_score import ClaimCheck, FitScore

# Below this groundedness score a packet is badged "needs review" in the cockpit.
# UNCALIBRATED: chosen as a starting point, never validated against a human-reviewed
# claim set. It is a prompt to look, not a measurement. Nothing is blocked by it.
GROUNDEDNESS_FLOOR_DEFAULT = 85


def needs_review(groundedness_score: int, floor: int = GROUNDEDNESS_FLOOR_DEFAULT) -> bool:
    """True when a packet falls below the review floor.

    Compares *groundedness*, not `overall_score` — overall folds in JD alignment,
    and a weak JD match is not a factuality problem.
    """
    return groundedness_score < floor


def verify_claims(claims: Sequence[ClaimCheck], work_history_md: str) -> list[ClaimCheck]:
    """Re-check the model's verdicts in Python. Returns a new list; never mutates.

    The fit score is one model judging another, so a hallucinated `evidence_quote`
    would mark a fabricated claim as grounded and nothing would notice. This is the
    one failure the model cannot self-detect, and it needs no model to catch: a
    quote must actually appear in `work_history.md`.

    This is **model-assisted review, not a guarantee**. It cannot catch a claim
    assembled from several true quotes (an aggregated savings figure), or a
    completion-tense upgrade whose quote is real ("began work to consolidate" cited
    for "consolidated"). Those are prompt-side problems.
    """
    haystack = _normalize_ws(work_history_md)
    return [
        claim
        if not claim.grounded or _quote_supported(claim.evidence_quote, haystack)
        else claim.model_copy(update={"grounded": False})
        for claim in claims
    ]


def _normalize_ws(text: str) -> str:
    """Collapse all runs of whitespace to single spaces. Nothing else is altered."""
    return " ".join(text.split())


def _quote_supported(quote: str, normalized_history: str) -> bool:
    """Whitespace-insensitive substring test. An empty quote supports nothing."""
    normalized = _normalize_ws(quote)
    if not normalized:
        return False
    return normalized in normalized_history


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

        score = parse_score(full_text, work_history_md, resume_md)

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


def parse_score(text: str, work_history_md: str, resume_md: str = "") -> FitScore:
    """Parse the model's score, then verify it in Python.

    `resume_md` is optional only so older callers keep working; pass it. Without
    it the employment-date check cannot run, and dates are the part of a resume
    most easily disproved.
    """
    data = find_json_value(text, "{", dict)
    if data is None:
        raise ValueError("No JSON object found in scoring response.")
    score = FitScore.model_validate(data)

    # Python-side verification: downgrade any claim whose evidence_quote is not an
    # actual substring of work_history_md — prevents Claude from fabricating quotes.
    score.claims = verify_claims(score.claims, work_history_md)

    # Employment dates, checked deterministically rather than left to the model.
    # A generated resume once carried six invented date ranges -- two four-month
    # roles inflated into two-year roles -- and the LLM pass scored it 62/100
    # while flagging thirteen prose claims and not one wrong date. Dates are the
    # single most falsifiable thing on a resume: a recruiter compares them to a
    # LinkedIn profile in seconds.
    if resume_md:
        contradictions, undated = resume_dates.verify(resume_md, work_history_md)
        for finding in contradictions:
            score.claims.append(
                ClaimCheck(claim_text=finding.message, grounded=False, source_section="dates")
            )
        score.undated_employers = undated
        if contradictions:
            score.summary = (
                f"{len(contradictions)} employment date range(s) contradict the work history "
                "— fix these before sending, they are trivially checked against LinkedIn. "
            ) + score.summary

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
    ]
    if score.undated_employers:
        lines += [
            "**Employers with no dates shown:** "
            + ", ".join(score.undated_employers)
            + "  \n*Missing dates are not a false claim, but a resume without them reads "
            "as hiding something.*",
            "",
        ]
    lines += [
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

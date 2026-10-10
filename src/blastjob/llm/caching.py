"""Utilities for building Claude message blocks with prompt caching."""

from typing import Any


def cached_text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


def plain_text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def build_ingest_messages(
    system_prompt: str,
    file_chunks: list[str],
) -> tuple[list[dict], list[dict]]:
    """Return (system_blocks, user_message_content) with cache breakpoints."""
    system = [cached_text(system_prompt)]
    combined = "\n\n".join(file_chunks)
    user_content = [plain_text(combined)]
    return system, user_content


def build_resume_messages(
    system_prompt: str,
    work_history_md: str,
    template_md: str,
    job_description: str,
    company_research: str,
) -> tuple[list[dict], list[dict]]:
    """Return (system_blocks, user_message_content) with 3 cache breakpoints."""
    system = [cached_text(system_prompt)]  # BP 1
    user_content = [
        cached_text(work_history_md),  # BP 2
        cached_text(template_md),  # BP 3
        plain_text(
            f"## Job Description\n\n{job_description}\n\n## Company Research\n\n{company_research}"
        ),
    ]
    return system, user_content


def build_cover_letter_messages(
    system_prompt: str,
    work_history_md: str,
    resume_md: str,
    job_description: str,
    company_research: str,
) -> tuple[list[dict], list[dict]]:
    """Return (system_blocks, user_message_content) for cover letter generation.

    Cache breakpoints: system, work_history. The resume is NOT cached: it is
    generated fresh for this one job, so caching it buys a write at 1.25x that
    can never produce a read. Nor does work_history reuse the resume call's block
    -- Anthropic keys the cache on the request prefix in order, and
    COVER_LETTER_SYSTEM differs from RESUME_SYSTEM, so the prefix differs from
    the first block onward. It is cached here on its own merits (it is stable
    across jobs), not because it is "warm".
    """
    system = [cached_text(system_prompt)]  # BP 1
    user_content = [
        cached_text(work_history_md),  # BP 2
        plain_text(f"## Current Resume\n\n{resume_md}"),
        plain_text(
            f"## Job Description\n\n{job_description}\n\n## Company Research\n\n{company_research}"
        ),
    ]
    return system, user_content


def build_coverage_messages(
    system_prompt: str,
    work_history_md: str,
    job_description: str,
) -> tuple[list[dict], list[dict]]:
    """Return (system_blocks, user_message_content) for JD coverage analysis.

    Cache breakpoints: system, work_history. JD is per-call.
    """
    system = [cached_text(system_prompt)]  # BP 1
    user_content = [
        cached_text(work_history_md),  # BP 2
        plain_text(f"## Job Description\n\n{job_description}"),
    ]
    return system, user_content


def build_refine_messages(
    system_prompt: str,
    work_history_md: str,
    current_resume_md: str,
    feedback: str,
    job_description: str,
) -> tuple[list[dict], list[dict]]:
    """Return (system_blocks, user_message_content) for resume revision.

    Cache breakpoints: system, work_history. The current resume is NOT cached --
    run_refine overwrites it after every revision, so the block is superseded
    before it could ever be read back.
    """
    system = [cached_text(system_prompt)]  # BP 1
    user_content = [
        cached_text(work_history_md),  # BP 2
        plain_text(f"## Current Resume\n\n{current_resume_md}"),
        plain_text(f"## Feedback\n\n{feedback}\n\n## Job Description\n\n{job_description}"),
    ]
    return system, user_content


def build_score_messages(
    system_prompt: str,
    work_history_md: str,
    resume_md: str,
    job_description: str,
) -> tuple[list[dict], list[dict]]:
    """Return (system_blocks, user_message_content) for the fit score call.

    work_history_md is cached because it is stable across jobs -- NOT because it
    reuses the resume call's block. The cache key is the request prefix in order
    (tools, system, messages), and FIT_SCORE_SYSTEM differs from RESUME_SYSTEM,
    so the prefix diverges at the first block and the two never share an entry.
    """
    system = [cached_text(system_prompt)]  # BP 1
    user_content = [
        cached_text(work_history_md),  # BP 2
        plain_text(
            f"## Generated Resume\n\n{resume_md}\n\n## Job Description\n\n{job_description}"
        ),
    ]
    return system, user_content

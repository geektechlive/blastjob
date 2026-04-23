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


def build_score_messages(
    system_prompt: str,
    work_history_md: str,
    resume_md: str,
    job_description: str,
) -> tuple[list[dict], list[dict]]:
    """Return (system_blocks, user_message_content) for the fit score call.

    work_history_md is cached — it reuses the warm cache block written during the
    preceding resume generation call in the same session, so no extra cache write cost.
    """
    system = [cached_text(system_prompt)]  # BP 1
    user_content = [
        cached_text(work_history_md),  # BP 2 — warm from resume gen
        plain_text(
            f"## Generated Resume\n\n{resume_md}\n\n## Job Description\n\n{job_description}"
        ),
    ]
    return system, user_content

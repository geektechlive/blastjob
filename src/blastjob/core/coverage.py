"""JD coverage analyzer — checks how well work_history.md supports a job description."""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from blastjob import config as cfg_mod
from blastjob.llm import caching, prompts
from blastjob.llm.cost import CostTracker, cost_from_usage
from blastjob.llm.providers import make_stream
from blastjob.models.config import BlastJobConfig
from blastjob.models.coverage import CoverageReport


async def analyze_coverage(
    job_description: str,
    app_config: BlastJobConfig,
    cost_tracker: CostTracker,
    on_text: Callable[[str], None] | None = None,
) -> CoverageReport:
    """Run a coverage check against the user's work_history.md.

    Raises FileNotFoundError if work history is missing, ValueError on empty JD or
    unparseable response.
    """
    if not job_description.strip():
        raise ValueError("Job description is empty.")

    data_path = cfg_mod.data_dir(app_config)
    wh_path = data_path / "work_history.md"
    if not wh_path.exists():
        raise FileNotFoundError("No work history found. Please ingest files first.")

    work_history_md = wh_path.read_text(encoding="utf-8")

    if on_text:
        on_text("\nAnalyzing JD coverage against your work history...\n\n")

    system, user_content = caching.build_coverage_messages(
        prompts.COVERAGE_SYSTEM, work_history_md, job_description
    )

    full_text = ""
    stream = await make_stream(
        app_config, system, [{"role": "user", "content": user_content}], max_tokens=4096
    )
    async with stream:
        async for chunk in stream.text_stream:
            full_text += chunk
            if on_text:
                on_text(chunk)
        final = await stream.get_final_message()

    call_cost = cost_from_usage(final.usage, app_config.pricing)
    cost_tracker.record(call_cost)

    return parse_coverage(full_text, work_history_md)


def parse_coverage(text: str, work_history_md: str) -> CoverageReport:
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ValueError("No JSON object found in coverage response.")
    data = json.loads(match.group())
    report = CoverageReport.model_validate(data)

    # Python-side verification: downgrade fabricated quotes (mirrors scoring discipline).
    for req in report.requirements:
        if req.covered and req.evidence_quote:
            if req.evidence_quote not in work_history_md:
                req.covered = False
                if not req.gap_note:
                    req.gap_note = "Quote not found in work history."

    # Recompute coverage_score with authoritative weights so model can't inflate it.
    musts = [r for r in report.requirements if r.priority == "must"]
    nices = [r for r in report.requirements if r.priority != "must"]
    must_pct = int(100 * sum(1 for r in musts if r.covered) / len(musts)) if musts else 0
    nice_pct = int(100 * sum(1 for r in nices if r.covered) / len(nices)) if nices else 0
    if musts and nices:
        report.coverage_score = int((2 * must_pct + nice_pct) / 3)
    elif musts:
        report.coverage_score = must_pct
    elif nices:
        report.coverage_score = nice_pct
    else:
        report.coverage_score = 0

    return report

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from blastjob import config as cfg_mod
from blastjob.importers.folder import labeled_chunks
from blastjob.llm import caching, prompts
from blastjob.llm.cost import CostTracker, cost_from_usage
from blastjob.llm.providers import make_stream
from blastjob.models.config import BlastJobConfig
from blastjob.models.history import MasterHistory


async def run_ingestion(
    path: Path,
    app_config: BlastJobConfig,
    cost_tracker: CostTracker,
    on_text: Callable[[str], None] | None = None,
) -> Path:
    data_path = cfg_mod.data_dir(app_config)
    data_path.mkdir(parents=True, exist_ok=True)

    def emit(msg: str) -> None:
        if on_text:
            on_text(msg)

    emit("Scanning files...\n")

    def on_file(index: int, total: int, name: str) -> None:
        emit(f"  [{index}/{total}] Reading {name}\n")

    def on_skip(file_path: Path, reason: str) -> None:
        emit(f"  [SKIP] {file_path.name}: {reason}\n")

    chunks = labeled_chunks(path, on_file=on_file, on_skip=on_skip)
    if not chunks:
        raise ValueError("No supported files found at the given path.")

    total_chars = sum(len(c) for c in chunks)
    emit(f"\nFound {len(chunks)} file(s) — ~{total_chars:,} characters\n")
    emit("Sending to Claude for extraction...\n")

    system, user_content = caching.build_ingest_messages(prompts.INGEST_SYSTEM, chunks)

    # Stream response but don't display raw JSON — show elapsed time instead
    history_md = ""
    start = time.monotonic()
    last_report = start
    chars_received = 0

    stream = await make_stream(
        app_config,
        system,
        [{"role": "user", "content": user_content}],
        max_tokens=8192,
    )
    async with stream:
        async for text in stream.text_stream:
            history_md += text
            chars_received += len(text)
            now = time.monotonic()
            if now - last_report >= 3.0:
                elapsed = now - start
                emit(f"  Processing... {chars_received:,} chars | {elapsed:.0f}s elapsed\n")
                last_report = now
        elapsed_total = time.monotonic() - start
        final = await stream.get_final_message()
    call_cost = cost_from_usage(final.usage, app_config.pricing)
    cost_tracker.record(call_cost)

    emit(f"\n  Done in {elapsed_total:.0f}s")
    if call_cost.cost_usd > 0:
        emit(f" (${call_cost.cost_usd:.4f})")
    emit("\n\nParsing results...\n")

    master = _parse_history(history_md)
    _emit_summary(master, emit)

    work_history_path = data_path / "work_history.md"
    work_history_md = _serialize_history(master)
    tmp = work_history_path.with_suffix(".md.tmp")
    tmp.write_text(work_history_md, encoding="utf-8")
    tmp.replace(work_history_path)

    emit("\nGenerating resume templates...\n")
    await _generate_templates(work_history_md, app_config, cost_tracker, emit)

    meta = {
        "last_ingested": datetime.now().isoformat(),
        "source_count": len(chunks),
        "word_count": len(work_history_md.split()),
    }
    (data_path / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    emit(f"\nAll done! Work history saved to {work_history_path}\n")
    return work_history_path


def _emit_summary(master: MasterHistory, emit: Callable[[str], None]) -> None:
    emit(f"  Name:           {master.name or '(not found)'}\n")
    emit(f"  Roles found:    {len(master.experience)}\n")
    emit(f"  Skills:         {len(master.skills)}\n")
    emit(f"  Education:      {len(master.education)}\n")
    emit(f"  Projects:       {len(master.projects)}\n")
    emit(f"  Certifications: {len(master.certifications)}\n")
    if master.experience:
        emit("\n  Roles:\n")
        for exp in master.experience:
            dates = f"{exp.dates.start} - {exp.dates.end}" if exp.dates.start else ""
            emit(f"    • {exp.title} at {exp.company}")
            if dates:
                emit(f" ({dates})")
            emit("\n")


def _parse_history(text: str) -> MasterHistory:
    import re

    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ValueError("Claude's response contained no JSON object — cannot parse work history.")
    data = json.loads(match.group())
    return MasterHistory.model_validate(data)


def _serialize_history(h: MasterHistory) -> str:
    lines = [
        "---",
        f"name: {h.name}",
        f"email: {h.email}",
        f"phone: {h.phone}",
        f"location: {h.location}",
    ]
    if h.linkedin:
        lines.append(f"linkedin: {h.linkedin}")
    if h.github:
        lines.append(f"github: {h.github}")
    lines.append("---\n")

    if h.summary:
        lines.append(f"## Summary\n\n{h.summary}\n")

    if h.experience:
        lines.append("## Experience\n")
        for exp in h.experience:
            dates = f"{exp.dates.start} - {exp.dates.end}" if exp.dates.start else ""
            loc = f" | {exp.location}" if exp.location else ""
            lines.append(f"### {exp.title} | {exp.company}{loc}")
            if dates:
                lines.append(f"*{dates}*\n")
            for b in exp.bullets:
                lines.append(f"- {b}")
            lines.append("")

    if h.education:
        lines.append("## Education\n")
        for edu in h.education:
            dates = f"{edu.dates.start} - {edu.dates.end}" if edu.dates.start else ""
            lines.append(f"### {edu.degree} | {edu.institution}")
            if dates:
                lines.append(f"*{dates}*\n")
            for d in edu.details:
                lines.append(f"- {d}")
            lines.append("")

    if h.skills:
        lines.append("## Skills\n")
        lines.append(", ".join(h.skills))
        lines.append("")

    if h.projects:
        lines.append("## Projects\n")
        for proj in h.projects:
            lines.append(f"### {proj.name}")
            if proj.description:
                lines.append(f"{proj.description}\n")
            for b in proj.bullets:
                lines.append(f"- {b}")
            if proj.technologies:
                lines.append(f"*Technologies: {', '.join(proj.technologies)}*")
            lines.append("")

    if h.certifications:
        lines.append("## Certifications\n")
        for cert in h.certifications:
            line = f"- {cert.name}"
            if cert.issuer:
                line += f" | {cert.issuer}"
            if cert.date:
                line += f" ({cert.date})"
            lines.append(line)
        lines.append("")

    return "\n".join(lines)


async def _generate_templates(
    work_history_md: str,
    app_config: BlastJobConfig,
    cost_tracker: CostTracker,
    emit: Callable[[str], None],
) -> None:
    data_path = cfg_mod.data_dir(app_config)
    templates_path = data_path / "templates"
    templates_path.mkdir(exist_ok=True)

    system = [caching.cached_text(prompts.TEMPLATE_SYSTEM)]
    messages = [
        {"role": "user", "content": [caching.plain_text(f"Work history:\n\n{work_history_md}")]}
    ]

    raw = ""
    start = time.monotonic()
    last_report = start

    stream = await make_stream(app_config, system, messages, max_tokens=4096)
    async with stream:
        async for text in stream.text_stream:
            raw += text
            now = time.monotonic()
            if now - last_report >= 3.0:
                emit(f"  Building templates... {time.monotonic() - start:.0f}s\n")
                last_report = now
        final = await stream.get_final_message()
    call_cost = cost_from_usage(final.usage, app_config.pricing)
    cost_tracker.record(call_cost)

    standard, ats = _split_templates(raw)
    (templates_path / "standard.md").write_text(standard, encoding="utf-8")
    (templates_path / "ats.md").write_text(ats, encoding="utf-8")
    emit(f"  Templates saved ({time.monotonic() - start:.0f}s)\n")


def _split_templates(raw: str) -> tuple[str, str]:
    if "===ATS===" in raw:
        parts = raw.split("===ATS===", 1)
        standard = parts[0].replace("===STANDARD===", "").strip()
        ats = parts[1].strip()
        return standard, ats
    # Delimiter missing — write raw to standard, leave ATS empty
    return raw.replace("===STANDARD===", "").strip(), ""

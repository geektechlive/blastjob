from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from blastjob import config as cfg_mod
from blastjob.core import docx_text, packet_gate, prose_check, quarantine, resume_format
from blastjob.core.cover_letter_score import (
    CoverLetterScore,
    render_cover_letter_score_md,
    score_cover_letter,
)
from blastjob.core.coverage_run import coverage_gaps_for, render_gaps_md
from blastjob.core.packet_report import (
    Artifact,
    CoverageGap,
    Finding,
    PacketReport,
    PacketState,
    Severity,
)
from blastjob.core.paths import application_dir, name_token, person_slug, resolve_role_slug
from blastjob.core.research_facts import CompanyFact, distill_research, facts_block
from blastjob.core.scoring import parse_score as _parse_score
from blastjob.core.scoring import render_score_md as _render_score_md
from blastjob.core.scoring import score_resume as _score_resume
from blastjob.exporters import ats, docx, markdown, pdf
from blastjob.llm import caching, prompts
from blastjob.llm.cost import CallCost, CostTracker, aggregate_calls, cost_from_usage
from blastjob.llm.providers import make_research_call, make_stream
from blastjob.models.config import BlastJobConfig
from blastjob.models.fit_score import FitScore

log = logging.getLogger(__name__)

__all__ = ["run_build", "_parse_score", "_render_score_md", "_score_resume"]


def _history_name(work_history_md: str) -> str:
    """Pull the candidate name from the work_history frontmatter (`name: ...`)."""
    import re

    m = re.search(r"^name:\s*(.+)$", work_history_md, re.MULTILINE)
    return m.group(1).strip() if m else "Candidate"


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

    # `cost_tracker` may be a long-lived, session-wide tracker (the TUI keeps
    # one on `app.cost_tracker` across every build in a sitting), so its full
    # `.calls` list can hold plenty of cost unrelated to this build. Every
    # call below still records into it immediately, exactly as before — a
    # build that fails partway (e.g. the empty-resume check below) must not
    # lose the cost of the calls it DID make. What's new is bookmarking where
    # this build's own calls start, so the total written to metadata.json can
    # be computed as exactly this build's slice, never the caller's whole
    # running total.
    _calls_before_this_build = len(cost_tracker.calls)

    # Step 1: Company research (skipped when company is confidential)
    if confidential:
        if on_text:
            on_text("Skipping company research (confidential posting).\n")
        research_md = ""
    else:
        if on_text:
            on_text(f"Researching {company}...\n")
        research_md = await make_research_call(
            app_config, company, cost_tracker, app_config.generation.max_web_searches
        )

    # Step 1b: the research sidecar is a trust boundary, not a warning label.
    # Scraped or recalled text about an employer is distilled into span-verified,
    # polarity-bearing facts in a call whose context holds the research and
    # nothing else, so it never shares a context window with work_history.md.
    # The cover letter consumes those facts instead of the raw sidecar: a fact
    # that says the company CONSIDERED entering a market cannot then support a
    # claim that it serves that market.
    company_facts = (
        await distill_research(
            research_md, company=company, cfg=app_config, cost_tracker=cost_tracker
        )
        if research_md
        else []
    )

    # Step 2: Generate resume
    system_prompt = prompts.with_profile(
        prompts.RESUME_ATS_SYSTEM if use_ats else prompts.RESUME_SYSTEM, app_config.candidate
    )
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

    # Step 3: Create output folder (one flat folder per company per day) and
    # build professional, self-describing file stems: {Person}_Resume_-_{Co}_{Role}.
    out_dir = application_dir(out_root, company)
    person = person_slug(_history_name(work_history_md))
    co_tok, role_tok = name_token(company), name_token(role)
    resume_stem = f"{person}_Resume_-_{co_tok}_{role_tok}"
    cover_stem = f"{person}_Cover_Letter_-_{co_tok}_{role_tok}"
    # Collision-safe: "VP, IT" and "VP IT" slugify identically, and this folder
    # is shared across every role at this company today, so a bare slugify let a
    # second role silently overwrite the first's sidecars.
    role_slug = resolve_role_slug(out_dir, role)

    # Step 4: Export
    if on_text:
        on_text(f"\n\nExporting to {out_dir}...\n")

    if "md" in formats:
        markdown.write(resume_md, out_dir, stem=resume_stem)
    if "pdf" in formats:
        try:
            pdf.write(resume_md, out_dir, stem=resume_stem)
        except Exception as e:
            if on_text:
                on_text(f"PDF export failed: {e}\n")
    if "docx" in formats:
        try:
            docx.write(resume_md, out_dir, stem=resume_stem)
        except Exception as e:
            if on_text:
                on_text(f"DOCX export failed: {e}\n")
    if "ats" in formats:
        ats.write(resume_md, out_dir, stem=f"{resume_stem}_ATS")

    # Step 5: Sidecars (job description + research), role-prefixed
    (out_dir / f"{role_slug}.job_description.md").write_text(
        f"# Job Description\n\n**Company:** {company}\n**Role:** {role}\n\n{job_description}",
        encoding="utf-8",
    )
    if research_md:
        (out_dir / f"{role_slug}.company_research.md").write_text(research_md, encoding="utf-8")

    # Step 6: Cover letter — fail-soft, optional
    cover_letter_cost = None
    cover_letter_md = ""
    cover_letter_score = None
    if include_cover_letter:
        cover_letter_cost, cover_letter_md = await _generate_cover_letter(
            resume_md,
            work_history_md,
            job_description,
            facts_block(company_facts),
            formats,
            out_dir,
            app_config,
            cost_tracker,
            on_text,
            stem=cover_stem,
            company=company,
        )

    # Step 6b: score the cover letter. Until 2026-09-16 nothing did, which is how
    # a finished letter claimed an employer's adhesives reach "phones and
    # spacecraft" when the research supported only electronics and aerospace.
    # Fail-soft, exactly like the resume score.
    if cover_letter_md:
        try:
            cover_letter_score = await score_cover_letter(
                cover_letter_md,
                work_history_md=work_history_md,
                research_md=research_md,
                cfg=app_config,
                cost_tracker=cost_tracker,
                facts=company_facts,
                on_text=on_text,
            )
            (out_dir / f"{role_slug}.cover_letter_score.json").write_text(
                cover_letter_score.model_dump_json(indent=2), encoding="utf-8"
            )
            (out_dir / f"{role_slug}.cover_letter_score.md").write_text(
                render_cover_letter_score_md(cover_letter_score), encoding="utf-8"
            )
        except Exception as e:  # noqa: BLE001 — a scoring failure must not lose the packet
            if on_text:
                on_text(f"\nCover-letter scoring skipped: {e}\n")

    # Step 7: Score — fail-soft so resume delivery is unaffected if scoring errors
    fit_score = await _score_resume(
        resume_md, work_history_md, job_description, app_config, cost_tracker, on_text
    )
    if fit_score is not None:
        (out_dir / f"{role_slug}.fit_score.json").write_text(
            fit_score.model_dump_json(indent=2), encoding="utf-8"
        )
        (out_dir / f"{role_slug}.fit_score.md").write_text(
            _render_score_md(fit_score), encoding="utf-8"
        )

    # Step 7a: the third bucket — requirements the history proves that this
    # resume never used. Read-only and advisory: the gaps are shown with the user's own
    # sentence quoted verbatim, and nothing is inserted automatically. One LLM
    # call per distinct job description, cached, so a re-tailor is free.
    coverage_gaps = await coverage_gaps_for(
        job_description, resume_md, cfg=app_config, cost_tracker=cost_tracker
    )
    if coverage_gaps:
        (out_dir / f"{role_slug}.coverage_gaps.md").write_text(
            render_gaps_md(coverage_gaps), encoding="utf-8"
        )

    # Step 7b: verify the packet and gate it. The checks read the EXPORTED .docx
    # where they can (V1: the ATS receives that file, not the markdown), and the
    # gate quarantines on a mechanical failure only — a low groundedness score
    # is loud but never blocking, because it is uncalibrated and fires on honest
    # stretch applications.
    packet_report = _verify_packet(
        resume_md=resume_md,
        cover_letter_md=cover_letter_md,
        work_history_md=work_history_md,
        job_description=job_description,
        out_dir=out_dir,
        resume_stem=resume_stem,
        fit_score=fit_score,
        cover_letter_score=cover_letter_score,
        coverage_gaps=coverage_gaps,
        company_facts=company_facts,
    )
    verdict = _gate_and_report(packet_report, out_dir, role_slug, app_config=app_config)
    if on_text:
        if verdict is None:
            on_text("\nPacket check: verification failed — see the packet report.\n")
        else:
            on_text(f"\nPacket check: {verdict.state.value}.\n")
            for reason in verdict.reasons:
                on_text(f"  - {reason}\n")

    # A quarantined artifact's documents leave the folder. "Not ready" next to a
    # working download link is a badge, not a gate. Scoped to the artifact that
    # failed: a cover-letter finding must not take the resume with it, and a
    # held resume must take the ATS file exported from the same markdown.
    # A verification crash (verdict is None) quarantines NOTHING -- one broken
    # checker would otherwise hold every packet.
    if verdict is not None and verdict.state is PacketState.QUARANTINED:
        stems = _quarantine_stems(verdict, resume_stem=resume_stem, cover_stem=cover_stem)
        try:
            if stems:
                quarantine.quarantine(out_dir, stems=stems, reasons=list(verdict.reasons))
        except Exception as e:  # noqa: BLE001 — never lose the packet to its own guard
            log.exception("quarantine failed for %s", role_slug)
            if on_text:
                on_text(f"Quarantine failed: {e}\n")

    # This build made up to six LLM calls (research, fact distillation, resume,
    # cover letter, cover-letter score, fit score), each already recorded into
    # `cost_tracker` immediately as it
    # happened (so a build that fails partway still leaves its real spend in
    # the caller's running total). `build_total` combines just THIS build's
    # slice of that tracker — bookmarked above — into the only number that
    # honestly answers "what did this build cost". `.known` is False if ANY
    # of the calls couldn't be costed (e.g. the claude-cli path failed to
    # parse a usage figure); the *_cost_usd fields below are `None` in that
    # case, never a partial sum passed off as the whole.
    build_total: CallCost = aggregate_calls(cost_tracker.calls[_calls_before_this_build:])

    (out_dir / f"{role_slug}.metadata.json").write_text(
        json.dumps(
            {
                "timestamp": datetime.now().isoformat(),
                "company": company,
                "role": role,
                "confidential": confidential,
                "provider": app_config.llm.provider,
                # Whole-build totals — the honest answer to "what did this cost".
                "build_cost_usd": round(build_total.cost_usd, 6) if build_total.known else None,
                "build_input_tokens": build_total.input_tokens,
                "build_output_tokens": build_total.output_tokens,
                "build_cache_creation_tokens": build_total.cache_creation_tokens,
                "build_cache_read_tokens": build_total.cache_read_tokens,
                "build_cache_hit_ratio": (
                    round(build_total.cache_hit_ratio, 3) if build_total.known else None
                ),
                # Legacy keys, kept ALWAYS NUMERIC for readers that don't know
                # about the `build_*` fields yet and aren't None-safe — the
                # TUI's history screen formats this straight into an f-string
                # (`f"${entry.cost_usd:.4f}"`) with no null check, so writing
                # `null` here would crash it outright. They used to hold the
                # RESUME call's cost only — ambiguously named for what was
                # actually a partial number — and now alias the true build
                # total instead, so those readers stop under-reporting
                # immediately. In the unknown case this reports 0.0 rather
                # than a fabricated partial sum: worse than `build_cost_usd`,
                # but no worse than this app's pre-existing 0.0-means-unknown
                # class of bug on a reader this workstream cannot edit — see
                # the INTEGRATION PATCH for switching these readers to the
                # `build_*` fields, where unknown is `None` and genuinely
                # distinguishable from free.
                "cost_usd": round(build_total.cost_usd, 6) if build_total.known else 0.0,
                "input_tokens": build_total.input_tokens,
                "output_tokens": build_total.output_tokens,
                "cache_creation_tokens": build_total.cache_creation_tokens,
                "cache_read_tokens": build_total.cache_read_tokens,
                "cache_hit_ratio": (
                    round(build_total.cache_hit_ratio, 3) if build_total.known else 0.0
                ),
                # The resume call specifically — unambiguous now that the bare
                # `cost_usd` name above no longer means "resume only".
                "resume_cost_usd": round(call_cost.cost_usd, 6) if call_cost.known else None,
                "resume_input_tokens": call_cost.input_tokens,
                "resume_output_tokens": call_cost.output_tokens,
                "resume_cache_creation_tokens": call_cost.cache_creation_tokens,
                "resume_cache_read_tokens": call_cost.cache_read_tokens,
                "resume_cache_hit_ratio": (
                    round(call_cost.cache_hit_ratio, 3) if call_cost.known else None
                ),
                "formats": list(formats),
                "ats_mode": use_ats,
                "fit_score": fit_score.overall_score if fit_score else None,
                "groundedness_score": fit_score.groundedness_score if fit_score else None,
                "unsupported_claims": fit_score.unsupported_count if fit_score else None,
                "include_cover_letter": include_cover_letter,
                "cover_letter_cost_usd": (
                    round(cover_letter_cost.cost_usd, 6)
                    if cover_letter_cost and cover_letter_cost.known
                    else None
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


def _letterhead(body_md: str, work_history_md: str, company: str = "") -> str:
    """Wrap a generated cover-letter body in an actual letter.

    Without this the document is a bare wall of paragraphs: no name, no contact
    details, no date, no sign-off. The user saw exactly that and
    rightly said it did not look professional. The letterhead is built from the
    work-history frontmatter rather than asked of the model, so the contact
    details cannot drift or be invented.
    """
    import datetime as _dt
    import re as _re

    fm = dict(
        _re.findall(r"^(name|email|phone|location|linkedin):\s*(.+)$", work_history_md, _re.M)
    )
    name = fm.get("name", "").strip()
    if not name:
        return body_md  # nothing trustworthy to build a letterhead from

    contact = " · ".join(
        v.strip().replace("https://www.", "")
        for k in ("location", "phone", "email", "linkedin")
        if (v := fm.get(k, "").strip())
    )
    head = [f"## {name}"]
    if contact:
        head += ["", contact]
    head += ["", _dt.date.today().strftime("%B %d, %Y").replace(" 0", " ")]
    if company:
        # Blank-line separated: the docx exporter joins consecutive plain lines
        # into one paragraph (correct markdown), which would otherwise render
        # the recipient as "Acme Corp Springfield, OH" on a single line.
        head += ["", company]
    head += [""]

    body = body_md.strip()
    tail = [] if body.rstrip().endswith(name) else ["", name]
    if "Sincerely," not in body:
        tail = ["", "Sincerely,"] + tail
    return "\n".join(head) + "\n" + body + "\n".join(tail) + "\n"


def _verify_packet(
    *,
    resume_md: str,
    cover_letter_md: str,
    work_history_md: str,
    job_description: str,
    out_dir: Path,
    resume_stem: str,
    fit_score: FitScore | None,
    cover_letter_score: CoverLetterScore | None = None,
    coverage_gaps: Sequence[CoverageGap] = (),
    company_facts: Sequence[CompanyFact] = (),
) -> PacketReport:
    """Run every deterministic check over a finished packet and aggregate one
    report.

    Structure and characters are checked against the EXPORTED .docx when it
    exists, because that is the file an ATS parses; the markdown is the
    fallback. Prose findings are advisory.

    Every check is fail-soft, and a failed check is RECORDED. Before that, a
    crashed checker was swallowed with a bare `continue`, so a packet whose
    checks never ran was indistinguishable from a clean one: same green badge,
    same "no findings recorded". `checks_run` / `checks_failed` exist so silence
    can be told apart from an all-clear.
    """
    findings: list[Finding] = []

    docx_lines: list[str] | None = None
    docx_path = out_dir / f"{resume_stem}.docx"
    if docx_path.exists():
        try:
            docx_lines = docx_text.extract_text(docx_path)
        except Exception as e:  # noqa: BLE001 — fall back to the markdown
            log.warning("docx read-back failed for %s: %s", docx_path.name, e)
            docx_lines = None

    # Only `source_span` is trusted from a distilled fact: research_facts
    # verifies the span verbatim against the sidecar, but never checks the
    # model-written `statement` against it, so a figure could be minted there.
    trusted_spans = [f.source_span for f in company_facts if getattr(f, "source_span", "")]

    checks: list[tuple[str, Callable[[], list[Finding]]]] = [
        (
            "resume_structure",
            lambda: resume_format.check_structure(
                resume_md,
                work_history_md=work_history_md,
                today=datetime.now().date(),
                docx_lines=docx_lines,
            ),
        ),
        (
            "resume_arithmetic",
            lambda: packet_gate.check_arithmetic(
                resume_md, work_history_md, artifact=Artifact.RESUME
            ),
        ),
        (
            "resume_prose",
            lambda: prose_check.check_prose(
                resume_md,
                kind="resume",
                history=work_history_md,
                job_description=job_description,
            ),
        ),
    ]
    if cover_letter_md:
        checks += [
            (
                "cover_letter_arithmetic",
                lambda: packet_gate.check_arithmetic(
                    cover_letter_md,
                    work_history_md,
                    artifact=Artifact.COVER_LETTER,
                    trusted_spans=trusted_spans,
                ),
            ),
            (
                "cover_letter_prose",
                lambda: prose_check.check_prose(
                    cover_letter_md,
                    kind="cover_letter",
                    history=work_history_md,
                    job_description=job_description,
                ),
            ),
        ]

    ran: list[str] = []
    failed: list[str] = []
    for name, check in checks:
        try:
            findings.extend(check())
            ran.append(name)
        except Exception as e:  # noqa: BLE001 — a broken checker is not a bad packet
            log.exception("packet check %s failed", name)
            failed.append(name)
            findings.append(
                Finding(
                    severity=Severity.MAJOR,
                    code="checker_failed",
                    message=(
                        f"The {name} check crashed ({type(e).__name__}), so it verified nothing."
                    ),
                    artifact=(
                        Artifact.COVER_LETTER
                        if name.startswith("cover_letter")
                        else Artifact.RESUME
                    ),
                    source="core/build.py",
                )
            )

    # An ungrounded claim the resume scorer already found becomes a finding so
    # the gate can name it instead of quoting a bare number at the user.
    #
    # Except date contradictions. `core/scoring.py` injects those into `claims`
    # tagged `source_section="dates"`, and `resume_format` already reports the
    # same defect as FATAL `role_not_in_history` using the stronger history
    # aggregation (every heading per employer, not just the first). Re-emitting
    # them here produced a second finding with an EMPTY evidence field, filed
    # under "claims with no support in your work history" -- which is a mislabel
    # for a date range, and made one defect look like three.
    if fit_score is not None:
        findings.extend(
            Finding(
                severity=Severity.MAJOR,
                code="unsupported_claim",
                message=claim.claim_text,
                artifact=Artifact.RESUME,
                evidence=claim.evidence_quote,
                source="work_history.md",
            )
            for claim in fit_score.claims
            if not claim.grounded and claim.source_section != "dates"
        )
    if cover_letter_score is not None:
        findings.extend(cover_letter_score.findings)

    return PacketReport(
        artifact_dir=str(out_dir),
        groundedness=fit_score.groundedness_score if fit_score else None,
        jd_phrasing=fit_score.jd_alignment_score if fit_score else None,
        # An unscored letter is None, never 0: a call that did not happen is not
        # evidence of a defect.
        cover_letter_groundedness=(
            cover_letter_score.overall_score
            if cover_letter_score is not None and cover_letter_score.scored
            else None
        ),
        findings=tuple(findings),
        coverage_gaps=tuple(coverage_gaps),
        checks_run=tuple(ran),
        checks_failed=tuple(failed),
    )


def _quarantine_stems(verdict, *, resume_stem: str, cover_stem: str) -> list[str]:
    """Which document stems leave the folder.

    RULE (what leaves the folder). Only the quarantined artifacts' documents
    move: RESUME -> the resume stem AND the `_ATS` text file exported from the
    same markdown, COVER_LETTER -> the letter stem. A resume held back while its
    own ATS twin stays downloadable would be the badge-not-a-gate defect
    quarantine exists to end; a letter's fatal taking the resume was the defect
    this scoping exists to end.
    """
    stems: list[str] = []
    if Artifact.RESUME in verdict.quarantined_artifacts:
        stems += [resume_stem, f"{resume_stem}_ATS"]
    if Artifact.COVER_LETTER in verdict.quarantined_artifacts:
        stems.append(cover_stem)
    return stems


def _gate_and_report(report: PacketReport, out_dir: Path, role_slug: str, *, app_config):
    """Gate the packet and write its sidecar. Returns the verdict, or None when
    verification itself failed.

    RULE (a verification bug must never orphan a packet). A raise anywhere in
    here reaches web/build.py, which records `artifact_dir=""` -- the packet
    then exists on disk and is unreachable from the job page forever. So a
    verification crash is reported as `verification_failed, sendable: false` and
    does NOT quarantine: one broken checker would otherwise quarantine every
    packet and teach the user to click Release without reading.
    """
    try:
        verdict = packet_gate.gate(report, cfg=app_config)
        (out_dir / f"{role_slug}.packet_report.json").write_text(
            json.dumps(_report_json(report, verdict), indent=2), encoding="utf-8"
        )
        return verdict
    except Exception as e:  # noqa: BLE001 — never lose the packet to its own guard
        log.exception("packet verification failed for %s", role_slug)
        try:
            (out_dir / f"{role_slug}.packet_report.json").write_text(
                json.dumps(
                    {
                        "state": "verification_failed",
                        "sendable": False,
                        "reasons": [f"Verification crashed: {type(e).__name__}: {e}"],
                        "findings": [],
                        "checks_run": list(report.checks_run),
                        "checks_failed": list(report.checks_failed),
                        "checks_skipped": list(report.checks_skipped),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            log.exception("could not even write the failure sidecar for %s", role_slug)
        return None


def _report_json(report: PacketReport, verdict) -> dict:
    """The packet report as a sidecar. Read by the cockpit; also the record of
    what was known about a packet at the moment it was built."""
    return {
        "state": verdict.state.value,
        "sendable": verdict.sendable,
        "reasons": list(verdict.reasons),
        "artifact_dir": report.artifact_dir,
        # Which documents are unsafe, so the panel can say "the letter is held,
        # the resume is clear" instead of leaving the user to guess.
        "quarantined_artifacts": sorted(a.value for a in verdict.quarantined_artifacts),
        # A packet whose checks did not run is not a clean packet.
        "checks_run": list(report.checks_run),
        "checks_failed": list(report.checks_failed),
        "checks_skipped": list(report.checks_skipped),
        "groundedness": report.groundedness,
        "jd_phrasing": report.jd_phrasing,
        "cover_letter_groundedness": report.cover_letter_groundedness,
        "coverage_gaps": [
            {"requirement": g.requirement, "history_quote": g.history_quote, "priority": g.priority}
            for g in report.coverage_gaps
        ],
        "findings": [
            {
                "severity": f.severity.value,
                "code": f.code,
                "message": f.message,
                "artifact": f.artifact.value,
                "line": f.line,
                "evidence": f.evidence,
                "source": f.source,
            }
            for f in report.findings
        ],
    }


async def _generate_cover_letter(
    resume_md: str,
    work_history_md: str,
    job_description: str,
    company_facts_md: str,
    formats: set[str],
    out_dir: Path,
    app_config: BlastJobConfig,
    cost_tracker: CostTracker,
    on_text: Callable[[str], None] | None = None,
    stem: str = "cover_letter",
    company: str = "",
) -> tuple[CallCost | None, str]:
    """Generate cover letter alongside the resume. Fail-soft.

    `company_facts_md` is the distilled fact block, never the raw research
    sidecar: the letter may only say about an employer what a span-verified,
    polarity-carrying fact supports. Returns (cost, letter text) — the text so
    the caller can score it, which nothing did until 2026-09-16.
    """
    if on_text:
        on_text("\nGenerating cover letter...\n\n")

    try:
        system, user_content = caching.build_cover_letter_messages(
            prompts.with_profile(prompts.COVER_LETTER_SYSTEM, app_config.candidate),
            work_history_md,
            resume_md,
            job_description,
            company_facts_md,
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
            return None, ""

        call_cost = cost_from_usage(final.usage, app_config.pricing)
        cost_tracker.record(call_cost)

        cover_md = _letterhead(cover_md, work_history_md, company)

        markdown.write(cover_md, out_dir, stem=stem)
        if "pdf" in formats:
            try:
                pdf.write(cover_md, out_dir, stem=stem)
            except Exception as e:
                if on_text:
                    on_text(f"\nCover letter PDF export failed: {e}\n")
        if "docx" in formats:
            try:
                docx.write(cover_md, out_dir, stem=stem)
            except Exception as e:
                if on_text:
                    on_text(f"\nCover letter DOCX export failed: {e}\n")

        if on_text:
            metric = (
                f" · ${call_cost.cost_usd:.4f}"
                f" · {call_cost.input_tokens + call_cost.output_tokens} tokens"
            )
            on_text(f"\n\nCover letter saved.{metric}\n")
        return call_cost, cover_md

    except Exception as e:
        if on_text:
            on_text(f"\nCover letter skipped: {e}\n")
        return None, ""

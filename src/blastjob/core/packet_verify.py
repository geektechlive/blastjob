"""Verify, gate and hold a finished packet.

Extracted from `core/build.py` (which was nearing its size limit) so the refine
pipeline can run exactly the same deterministic checks, gate and quarantine over
a revised resume that a fresh build runs. Nothing here calls a model: the LLM
scorers (resume groundedness, cover-letter claims) run in their callers and
their results are passed in.

`core/build.py` re-exports the old private names (`_verify_packet`,
`_gate_and_report`, ...) because tests and `web/packet_view.py` refer to them.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from blastjob.core import docx_text, packet_gate, prose_check, quarantine, resume_format
from blastjob.core.cover_letter_score import CoverLetterScore
from blastjob.core.packet_report import (
    Artifact,
    CoverageGap,
    Finding,
    PacketReport,
    PacketState,
    Severity,
)
from blastjob.core.research_facts import CompanyFact
from blastjob.models.fit_score import FitScore

log = logging.getLogger(__name__)

# `checks_skipped` names for the two model-backed scorers. A scorer that did not
# run is not evidence of a clean packet, so its absence is recorded and shown.
SKIPPED_RESUME_SCORE = "resume_groundedness"
SKIPPED_COVER_LETTER_SCORE = "cover_letter_claims"
# The letter was asked for but its generation failed (e.g. a `claude -p` CLIError).
# Fail-soft so the finished resume is not lost, but never silent.
SKIPPED_COVER_LETTER_GENERATION = "cover_letter_generation"


class PacketNotQuarantinedError(RuntimeError):
    """A held document could not be moved out of the public packet folder.

    Raised at the END of run_build (after metadata.json is written, so the role
    slug stays claimed and the next tailor does not fork a second slug family).
    The caller records the build as failed: the packet is NOT done. `out_dir` is
    the folder the packet sits in, so a caller can still point the user at it."""

    def __init__(self, message: str, out_dir: Path) -> None:
        super().__init__(message)
        self.out_dir = out_dir


def verify_packet(
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
    cover_letter_expected: bool = False,
    carried_cover_findings: Sequence[Finding] | None = None,
    carried_checks_skipped: Sequence[str] = (),
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
    can be told apart from an all-clear. `checks_skipped` does the same for the
    two model scorers: a resume with no fit score, or a letter that exists but
    was not scored, says so instead of passing as grounded.
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
    # `carried_cover_findings` (refine): the letter was not touched, so its earlier
    # findings are carried forward instead of re-running its checks, which would
    # lack the research spans that made its employer figures legitimate.
    if cover_letter_md and carried_cover_findings is None:
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
    if carried_cover_findings is not None:
        findings.extend(carried_cover_findings)

    skipped: list[str] = []
    if fit_score is None:
        skipped.append(SKIPPED_RESUME_SCORE)
    if cover_letter_expected and not cover_letter_md:
        skipped.append(SKIPPED_COVER_LETTER_GENERATION)
    letter_scored = carried_cover_findings is not None or (
        cover_letter_score is not None and cover_letter_score.scored
    )
    if cover_letter_md and not letter_scored:
        skipped.append(SKIPPED_COVER_LETTER_SCORE)
    # A refine does not redo what an earlier run left undone (e.g. a letter whose
    # generation failed): that gap stays on the record.
    skipped.extend(name for name in carried_checks_skipped if name not in skipped)

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
        checks_skipped=tuple(skipped),
    )


def quarantine_stems(verdict, *, resume_stem: str, cover_stem: str) -> list[str]:
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


def _is_stem_file(name: str, stems: list[str]) -> bool:
    return any(name == f"{s}{ext}" for s in stems for ext in quarantine.DOCUMENT_EXTENSIONS)


def write_text_atomic(path: Path, text: str) -> None:
    """Write via a temp file in the SAME folder's hidden staging name, then rename.

    A reader (or a crash) never sees a half-written sidecar. The temp name starts
    with a dot and has no recognised suffix; a failed write removes it."""
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def report_path(out_dir: Path, role_slug: str) -> Path:
    """`{role_slug}.packet_report.json`, or the bare legacy name for a legacy
    per-role folder (no role slug)."""
    return out_dir / (f"{role_slug}.packet_report.json" if role_slug else "packet_report.json")


def gate_and_report(
    report: PacketReport,
    out_dir: Path,
    role_slug: str,
    *,
    app_config,
    resume_stem: str = "",
    cover_stem: str = "",
    extra: dict | None = None,
    will_publish: Sequence[str] = (),
    waived_artifacts: frozenset[Artifact] = frozenset(),
    override: dict | None = None,
):
    """Gate the packet and write its sidecar. Returns the verdict, or None when
    verification itself failed.

    RULE (a verification bug must never orphan a packet). A raise anywhere in
    here reaches web/build.py, which records `artifact_dir=""` -- the packet
    then exists on disk and is unreachable from the job page forever. So a
    verification crash is reported as `verification_failed, sendable: false` and
    does NOT quarantine: one broken checker would otherwise quarantine every
    packet and teach the user to click Release without reading.

    When the verdict is QUARANTINED and the stems are given, the report also
    NAMES the public documents about to be held (`quarantined_files`). It is
    written before the move, so a crash or a failed rename still leaves the
    download resolver refusing exactly those files.

    `waived_artifacts` + `override` (refine): artifacts whose findings are
    unchanged since the user released them stay released. They are dropped from the
    hold, and when nothing is left to hold the packet keeps the user's recorded
    override instead of being quarantined again.
    """
    try:
        verdict = packet_gate.gate(report, cfg=app_config)
        if waived_artifacts:
            verdict = _waive(verdict, waived_artifacts)
        held: list[str] = []
        if verdict.quarantined_artifacts and (resume_stem or cover_stem):
            stems = quarantine_stems(verdict, resume_stem=resume_stem, cover_stem=cover_stem)
            held = quarantine.targeted_files(out_dir, stems)
            # Files about to be published (refine stages first) are named too, so
            # they are refused for the whole window before they are moved.
            held += [n for n in will_publish if _is_stem_file(n, stems) and n not in held]
        data = report_json(report, verdict)
        data["quarantined_files"] = held
        if override and verdict.state is PacketState.ACKNOWLEDGED_OVERRIDE:
            data["override"] = override
        data.update(extra or {})
        write_text_atomic(report_path(out_dir, role_slug), json.dumps(data, indent=2))
        return verdict
    except Exception as e:  # noqa: BLE001 — never lose the packet to its own guard
        log.exception("packet verification failed for %s", role_slug)
        _write_failure_sidecar(out_dir, role_slug, report, e, will_publish)
        return None


def _waive(verdict, waived: frozenset[Artifact]):
    """Drop released artifacts from a QUARANTINED verdict. With none left, the
    packet is the user's acknowledged override (reasons kept: the record of why
    it was held stays)."""
    if verdict.state is not PacketState.QUARANTINED:
        return verdict
    remaining = verdict.quarantined_artifacts - waived
    if remaining == verdict.quarantined_artifacts:
        return verdict
    if remaining:
        return dataclasses.replace(verdict, quarantined_artifacts=remaining)
    return dataclasses.replace(
        verdict, state=PacketState.ACKNOWLEDGED_OVERRIDE, quarantined_artifacts=frozenset()
    )


def _write_failure_sidecar(
    out_dir: Path, role_slug: str, report: PacketReport, error: Exception, will_publish
) -> None:
    """Record that verification crashed. A crash is not a verdict, so it never
    LIFTS a hold: when the previous report held documents, those holds (state,
    artifacts, file names) are carried into this sidecar."""
    path = report_path(out_dir, role_slug)
    data: dict = {
        "state": "verification_failed",
        "sendable": False,
        "reasons": [f"Verification crashed: {type(error).__name__}: {error}"],
        "findings": [],
        "checks_run": list(report.checks_run),
        "checks_failed": list(report.checks_failed),
        "checks_skipped": list(report.checks_skipped),
    }
    try:
        prior = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        prior = {}
    if isinstance(prior, dict) and prior.get("state") == "quarantined":
        artifacts = [a for a in prior.get("quarantined_artifacts") or [] if isinstance(a, str)]
        held = [n for n in prior.get("quarantined_files") or [] if isinstance(n, str)]
        if Artifact.RESUME.value in artifacts:
            held += [n for n in will_publish if n not in held]
        data["state"] = "quarantined"
        data["quarantined_artifacts"] = artifacts
        data["quarantined_files"] = held
        data["reasons"] += [
            r for r in prior.get("reasons") or [] if isinstance(r, str) and r not in data["reasons"]
        ]
    try:
        write_text_atomic(path, json.dumps(data, indent=2))
    except OSError:
        log.exception("could not even write the failure sidecar for %s", role_slug)


def hold_documents(
    out_dir: Path,
    verdict,
    role_slug: str,
    *,
    resume_stem: str,
    cover_stem: str,
) -> str | None:
    """Move a QUARANTINED packet's documents out of the public folder.

    Returns None on success (or when nothing is held), else a FAILED-TO-QUARANTINE
    message naming what stayed public. The report is rewritten with
    `quarantine_failed` so the cockpit can say so; the caller must NOT call the
    packet done. A held-back verdict of None (verification crashed) holds nothing.
    """
    if verdict is None or not verdict.quarantined_artifacts:
        return None
    stems = quarantine_stems(verdict, resume_stem=resume_stem, cover_stem=cover_stem)
    if not stems:
        return None
    try:
        quarantine.quarantine(out_dir, stems=stems, reasons=list(verdict.reasons))
    except Exception as e:  # noqa: BLE001 — any failure to hold is a failed hold
        log.exception("quarantine failed for %s", role_slug)
        leftovers = getattr(e, "leftovers", None) or quarantine.targeted_files(out_dir, stems)
        message = (
            "FAILED-TO-QUARANTINE: "
            + (", ".join(leftovers) if leftovers else f"{type(e).__name__}: {e}")
            + " could not be moved out of the packet folder."
        )
        _mark_quarantine_failed(out_dir, role_slug, message)
        return message
    return None


def _mark_quarantine_failed(out_dir: Path, role_slug: str, message: str) -> None:
    path = report_path(out_dir, role_slug)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["quarantine_failed"] = True
        data["reasons"] = [message, *data.get("reasons", [])]
        write_text_atomic(path, json.dumps(data, indent=2))
    except (OSError, ValueError):
        log.exception("could not record the quarantine failure for %s", role_slug)


def report_json(report: PacketReport, verdict) -> dict:
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

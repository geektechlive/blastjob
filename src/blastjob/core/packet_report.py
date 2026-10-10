"""Shared vocabulary for packet verification: findings, the aggregated report,
and the gate verdict.

Every check in the packet-quality program (structure, prose, arithmetic,
coverage gaps, cover-letter groundedness) returns `Finding`s, and the gate reads
ONE aggregated `PacketReport`. That matters because the checks disagree about
what they are looking at: a resume can be perfectly grounded while its cover
letter invents a fact about the employer, so a gate that takes a single score
cannot express the packet it is judging.

SEVERITY IS CALIBRATED TO THE USER'S BOARDS, NOT TO A PARSER VENDOR. They apply
through Workday, Dayforce, LinkedIn and some Greenhouse; Textkernel (whose
published Resume Quality spec is the most detailed available) sits behind none
of them, and its own docs say those codes are risk signals rather than proof of
parse failure. So FATAL is reserved for failures with evidence in their path:

  - banned characters (a live Workday rejection, 2026-09-10: "Contains illegal
    characters")
  - encoding damage (U+FFFD shipped in a real packet)
  - a role that ended within 20 years carrying no bullets (shipped in three
    packets the user actually sent)
  - a total that does not reconcile against work_history.md
  - a cover-letter claim the research sidecar contradicts

Everything Textkernel-derived is MINOR: advisory, never blocking. A checker that
cries fatal over compliant formatting teaches the reader to dismiss it, which is
worse than no checker at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    """FATAL blocks (see module docstring for what earns it); MAJOR is a real
    defect that still ships; MINOR is advisory."""

    FATAL = "fatal"
    MAJOR = "major"
    MINOR = "minor"


class Artifact(StrEnum):
    """Which file a finding is about. The gate keeps them separate: a clean
    resume does not excuse a cover letter that invents an employer fact."""

    RESUME = "resume"
    COVER_LETTER = "cover_letter"


@dataclass(frozen=True)
class Finding:
    """One defect, located precisely enough to act on.

    `code` is a stable identifier (e.g. "empty_recent_role") so findings can be
    counted across builds without matching on prose. `evidence` quotes the text
    that triggered it; `source` names the authority (a logged ATS rejection, a
    vendor doc, work_history.md) so a rule can be re-argued later against its
    actual basis rather than folklore.
    """

    severity: Severity
    code: str
    message: str
    artifact: Artifact = Artifact.RESUME
    line: int | None = None
    evidence: str = ""
    source: str = ""


@dataclass(frozen=True)
class CoverageGap:
    """A requirement the job description asks for, that work_history.md can
    prove, and that the generated resume did not use — the third bucket.

    Groundedness catches claims with no support. This catches support with no
    claim: the failure mode that quietly undersells a qualified candidate. It
    carries the history text verbatim so the only automatic remedy is inserting
    the user's own sentence, never a model's paraphrase of it.
    """

    requirement: str
    history_quote: str
    priority: str = "must"


@dataclass(frozen=True)
class PacketReport:
    """Everything known about one generated packet, immutable once built.

    Scores are per artifact and may disagree. `groundedness` / `jd_phrasing`
    describe the resume (jd_phrasing measures how far the resume echoes the
    posting's wording — it is NOT a measure of fit, and must never be presented
    as one). `cover_letter_groundedness` is None when the packet has no letter.
    """

    artifact_dir: str = ""
    groundedness: int | None = None
    jd_phrasing: int | None = None
    cover_letter_groundedness: int | None = None
    findings: tuple[Finding, ...] = ()
    coverage_gaps: tuple[CoverageGap, ...] = ()
    # Which checks ran, which blew up, which sat out. A packet whose checks did
    # not run is NOT a clean packet, and before these fields existed it looked
    # exactly like one: `state: ready, findings: []`, green badge, "no findings
    # recorded". Silence has to be distinguishable from an all-clear.
    checks_run: tuple[str, ...] = ()
    checks_failed: tuple[str, ...] = ()
    checks_skipped: tuple[str, ...] = ()

    def fatal(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.FATAL)


class PacketState(StrEnum):
    """Where a packet sits in its lifecycle.

    QUARANTINED artifacts live in a `quarantine/` subdirectory rather than
    beside sendable ones: a "not ready" label next to a working download is the
    same defect as badging a bad packet and calling it handled.
    ACKNOWLEDGED_OVERRIDE is the user deciding to send anyway, with their reason
    recorded — a manual decision always beats automation, but it is written down.
    """

    GENERATED = "generated"
    VERIFIED = "verified"
    REPAIR_ATTEMPTED = "repair_attempted"
    READY = "ready"
    QUARANTINED = "quarantined"
    ACKNOWLEDGED_OVERRIDE = "acknowledged_override"


@dataclass(frozen=True)
class GateVerdict:
    """The gate's decision plus why, so the cockpit can say something specific
    rather than showing a number.

    `state` stays packet-level worst case, so a badge never under-reports.
    `quarantined_artifacts` says WHICH documents are unsafe, which is what
    stops a cover-letter finding from taking the resume down with it.
    """

    state: PacketState
    reasons: tuple[str, ...] = field(default_factory=tuple)
    quarantined_artifacts: frozenset[Artifact] = frozenset()

    @property
    def sendable(self) -> bool:
        return self.state in (PacketState.READY, PacketState.ACKNOWLEDGED_OVERRIDE)


# RULE (artifact scoping). A FATAL finding quarantines the artifact it names and
# every artifact derived from it. The ATS text file derives from the resume, so a
# resume fatal takes both; nothing derives from the cover letter, so a letter
# fatal takes the letter alone. A fatal whose artifact is missing or
# unrecognized quarantines EVERYTHING: unscoped means we do not know which
# document is unsafe, and an unknown document is unsendable.
DERIVED_ARTIFACTS: dict[Artifact, frozenset[Artifact]] = {
    Artifact.RESUME: frozenset({Artifact.RESUME}),
    Artifact.COVER_LETTER: frozenset({Artifact.COVER_LETTER}),
}


def artifacts_to_quarantine(findings: tuple[Finding, ...]) -> frozenset[Artifact]:
    """Which artifacts a set of fatal findings makes unsendable."""
    out: set[Artifact] = set()
    for f in findings:
        if f.severity is not Severity.FATAL:
            continue
        derived = DERIVED_ARTIFACTS.get(f.artifact)
        if derived is None:
            return frozenset(Artifact)
        out |= derived
    return frozenset(out)

"""Read a tailored packet's `fit_score.json` back into the cockpit.

Every tailor build writes per-claim verdicts next to the resume and, until this
module, nothing ever read them: a packet could score 62 on groundedness with 13
unsupported claims and the app would never say so. The user sends these to real
employers, so the number belongs on screen next to the download links.

What is on screen is **model-assisted review, not a guarantee**. One model checked
another's work, and `core.scoring.verify_claims` then re-checked every evidence
quote against `work_history.md` in Python. That catches invented quotes. It does
not catch a figure aggregated from several true numbers, or a real quote stretched
to cover a stronger claim.

Nothing here raises: a packet with no score, a truncated file or a file from a
different schema version all degrade to "not scored". The caller passes the packet
directory in (two output layouts exist on disk); this module never computes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from blastjob.core.scoring import GROUNDEDNESS_FLOOR_DEFAULT, needs_review
from blastjob.models.fit_score import FitScore
from blastjob.web.sidecars import find_sidecars, mismatch_note

NOTE = (
    "Model-assisted review, not a guarantee. Quotes are re-checked against your "
    "work history in code; claims built from several true facts are not."
)

STALE_NOTE = (
    "Packets tailored before the quote check shipped carry the model's own verdicts "
    "— re-tailor to re-verify."
)


@dataclass(frozen=True)
class UnsupportedClaim:
    """One resume claim the review could not tie to the work history."""

    claim_text: str = ""
    evidence_quote: str = ""
    source_section: str = ""


@dataclass(frozen=True)
class GroundednessSummary:
    """What the cockpit shows for one packet. `scored=False` means no usable file."""

    scored: bool = False
    status: str = "not scored"
    reason: str = ""
    overall_score: int = 0
    jd_alignment_score: int = 0
    groundedness_score: int = 0
    total_claims: int = 0
    unsupported_count: int = 0
    unsupported: tuple[UnsupportedClaim, ...] = ()
    undated_employers: tuple[str, ...] = ()
    summary: str = ""
    floor: int = GROUNDEDNESS_FLOOR_DEFAULT
    needs_review: bool = False
    source_path: str = ""
    note: str = NOTE
    stale_note: str = STALE_NOTE
    extra_files: tuple[str, ...] = field(default=())


def _not_scored(reason: str, floor: int) -> GroundednessSummary:
    return GroundednessSummary(scored=False, status="not scored", reason=reason, floor=floor)


def load_fit_score(
    packet_dir: str | Path | None,
    floor: int = GROUNDEDNESS_FLOOR_DEFAULT,
    role_slug: str | None = None,
) -> GroundednessSummary:
    """Summarize the fit score in `packet_dir`. Never raises.

    `packet_dir` comes from the caller (`packet_for_job`); it is not recomputed
    here. `floor` is the uncalibrated needs-review threshold, compared against the
    groundedness score only. `role_slug` disambiguates a directory holding more
    than one role's packet.
    """
    if not packet_dir:
        return _not_scored("No packet directory for this job.", floor)

    directory = Path(packet_dir)
    try:
        if not directory.is_dir():
            return _not_scored("Packet folder is missing.", floor)
        picks = find_sidecars(directory, ".fit_score.json", role_slug, bare_name="fit_score.json")
        if not picks:
            return _not_scored("No fit_score.json in this packet.", floor)
        score = FitScore.model_validate_json(picks[0].path.read_text(encoding="utf-8"))
    except Exception as exc:  # unreadable, truncated, or a shape we do not know
        return _not_scored(f"Fit score could not be read ({type(exc).__name__}).", floor)

    note = NOTE
    warning = mismatch_note(picks, role_slug)
    if warning:
        note = f"{warning} {NOTE}"

    unsupported = tuple(
        UnsupportedClaim(
            claim_text=c.claim_text,
            evidence_quote=c.evidence_quote,
            source_section=c.source_section,
        )
        for c in score.claims
        if not c.grounded
    )
    return GroundednessSummary(
        scored=True,
        status="scored",
        overall_score=score.overall_score,
        jd_alignment_score=score.jd_alignment_score,
        groundedness_score=score.groundedness_score,
        total_claims=len(score.claims),
        # Recounted from the claims, not read from `unsupported_count`: the stored
        # value came from whichever version of the scorer wrote the file.
        unsupported_count=len(unsupported),
        unsupported=unsupported,
        undated_employers=tuple(score.undated_employers),
        summary=score.summary,
        floor=floor,
        needs_review=needs_review(score.groundedness_score, floor),
        source_path=str(picks[0].path),
        note=note,
        extra_files=tuple(str(p.path) for p in picks[1:]),
    )

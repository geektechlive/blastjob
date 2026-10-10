"""Domain models for discovered jobs.

Separate state columns (not one mega-status) so discovery upserts can refresh a
posting without clobbering the user's editorial or application decisions.
Comp is provenance-aware: verified (posted) figures are never mixed with
inferred or estimated ones in a single ranking, and only a posted figure is ever
quoted to a human as the employer's number.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class SourceState(StrEnum):
    ACTIVE = "active"  # still present at the source
    STALE = "stale"  # no longer returned by the source's feed


class ReviewState(StrEnum):
    NEW = "new"  # discovered, not yet triaged
    SURFACED = "surfaced"  # passed the gate, shown in the main list
    QUEUED = "queued"  # unknown/estimated comp or ambiguous geo — review queue
    KEPT = "kept"  # the user marked it a keeper
    ARCHIVED = "archived"  # the user dismissed it


class ArchiveReason(StrEnum):
    """Structured reasons for archiving. Codes, not prose, so each one can be
    routed deterministically (see schema migration 8)."""

    LEVEL = "level"  # wrong seniority / scope (IC, too junior, too senior)
    DOMAIN = "domain"  # wrong field (not IT/technology leadership)
    GEO = "geo"  # location / commute
    COMP = "comp"  # compensation too low
    COMPANY = "company"  # never this company (auto-skips its future postings)
    OTHER = "other"


# Reasons the LLM scorer learns from. geo/comp already have deterministic gates;
# company becomes a deterministic skip; "other" is too vague to generalize.
SCORER_FEEDBACK_REASONS: tuple[str, ...] = (ArchiveReason.LEVEL, ArchiveReason.DOMAIN)
ARCHIVE_REASONS: tuple[str, ...] = tuple(r.value for r in ArchiveReason)
ARCHIVE_REASON_LABELS: dict[str, str] = {
    ArchiveReason.LEVEL: "wrong level / scope",
    ArchiveReason.DOMAIN: "wrong domain",
    ArchiveReason.GEO: "location / commute",
    ArchiveReason.COMP: "comp too low",
    ArchiveReason.COMPANY: "never this company",
    ArchiveReason.OTHER: "other",
}


class BuildState(StrEnum):
    NONE = "none"  # no tailored bundle yet
    TAILORING = "tailoring"  # a build run holds the lease
    TAILORED = "tailored"  # bundle produced
    FAILED = "failed"  # build errored; retryable


class CompProvenance(StrEnum):
    """Where a comp figure came from. Only POSTED is a *verified* number, and
    only a verified number may hard-reject a role at the gate.

    Values are stable strings persisted in `jobs.comp_provenance` — never rename
    or renumber an existing member.
    """

    POSTED = "posted"  # parsed from the posting itself — verified
    INFERRED = "inferred"  # an LLM read it out of the JD prose — NOT verified
    ESTIMATED = "estimated"  # modelled from level + geo, no JD figure at all
    UNKNOWN = "unknown"  # no signal


# The only provenance that counts as a published, quotable figure. Everything
# else is our own reading and must render as such (and must never hard-reject).
VERIFIED_COMP_PROVENANCE: str = CompProvenance.POSTED.value


class GeoStatus(StrEnum):
    VERIFIED = "verified"  # routed drive time is known
    APPROXIMATE = "approximate"  # distance proxy only
    UNKNOWN = "unknown"  # location too imprecise to place


class Comp(BaseModel):
    """Compensation as a range (what boards actually publish) plus a single
    `total_est` for ranking. Provenance decides the bucket: a POSTED range is
    verified and gate-eligible; INFERRED/ESTIMATED/UNKNOWN is our own reading,
    so it is never ranked against verified numbers and never hard-rejects."""

    min: int = 0  # low end of the posted/inferred range
    max: int = 0  # high end — the figure the floor gate checks, POSTED comp only
    # Sort key only, never displayed. Lists order by provenance bucket FIRST
    # (web/service.py), so this only ever ranks rows against others of the same
    # provenance — an inferred figure never outranks a posted one.
    total_est: int = 0
    currency: str = "USD"
    confidence: float = 0.0  # 0..1
    provenance: CompProvenance = CompProvenance.UNKNOWN


class Geo(BaseModel):
    remote: bool = False
    hybrid: bool = False
    location_raw: str = ""
    distance_mi: float | None = None
    commute_min: int | None = None
    status: GeoStatus = GeoStatus.UNKNOWN


class Job(BaseModel):
    """A discovered posting. Identity is (source, external_id) — never
    company+title+location, which would let two distinct roles overwrite."""

    id: int | None = None
    source: str = ""  # e.g. "greenhouse", "adzuna"
    external_id: str = ""  # the source's own req id / slug
    company: str = ""
    title: str = ""
    url: str = ""
    raw_jd: str = ""

    comp: Comp = Field(default_factory=Comp)
    geo: Geo = Field(default_factory=Geo)

    alignment_score: int | None = None  # requirements-alignment %, tunable gate
    score_model_version: str = ""
    rationale: str = ""

    cluster_id: int | None = None  # reversible cross-source duplicate grouping

    source_state: SourceState = SourceState.ACTIVE
    review_state: ReviewState = ReviewState.NEW
    build_state: BuildState = BuildState.NONE
    application_state: str = ""  # mirrors the existing tracking model's status
    applied_at: str = ""  # ISO date stamped on the first move to "applied"
    app_notes: str = ""  # free-text notes for the application
    next_action_due: str = ""  # ISO date a follow-up is due (manual or auto-nudged)
    notified_at: str = ""  # ISO datetime the daily digest pushed this job to Discord
    archive_reason: str = ""  # ArchiveReason code when archived ('' if none given)
    archive_note: str = ""  # optional free-text note alongside the reason

    first_seen: str = ""
    last_seen: str = ""

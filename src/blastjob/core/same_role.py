"""Same role, different route: an agency or aggregator posting that may be an
employer's own opening (and the reverse).

Applying through an agency AND directly to the employer can start a
candidate-ownership dispute and cost the placement. This module only ever
SUGGESTS a pair, with plain evidence; the user confirms ('same', which joins the
two into one cluster) or dismisses ('different', which suppresses it forever).
It never names an agency's client: a candidate is an employer posting that is
already in the database.

Match: equal normalized title, same metro (strict: same city, centroids within
the commute radius, or both remote), first seen within `SUGGEST_WINDOW_DAYS`.
JD text is supporting evidence only: agencies rewrite descriptions.

A second kind, PREVIOUS: the same employer re-posting a role the user already
applied to (any applied state, in progress or closed), not already in that
application's cluster. Equal normalized title, same company key, loose metro,
no time window: a repost can come months later. An in-process repost is
usually merged outright by `role_clusters.auto_same_role`; what is left here
is the case a merge must not decide (a closed application may be a new req).
"""

from __future__ import annotations

import datetime
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, replace

from blastjob.core import role_clusters as rc
from blastjob.core.agencies import is_agency
from blastjob.core.company import company_key
from blastjob.core.titles import normalize_role_title
from blastjob.models.config import DiscoveryConfig

# Agency re-posts trail or lead the employer's by weeks, not months.
SUGGEST_WINDOW_DAYS = 60
# Word n-gram size for the JD overlap figure, and the least worth showing.
SHINGLE_WORDS = 5
MIN_SHOWN_OVERLAP = 0.05
# Application states that mean the user already applied through that posting.
APPLIED_STATES: frozenset[str] = frozenset(
    {"applied", "screen", "interview", "offer", "rejected", "withdrawn"}
)
EMPLOYER = "employer"
AGENCY = "agency"
PREVIOUS = "previous"

_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Candidate:
    """Another posting that may be the same role as the job asked about."""

    job_id: int
    company: str
    title: str
    company_key: str
    kind: str  # EMPLOYER (asked about an agency job), AGENCY (the reverse) or PREVIOUS
    review_state: str
    application_state: str
    applied_at: str
    source_state: str
    decision: str  # 'same' when the user confirmed it, else ''
    evidence: tuple[str, ...]
    jd_overlap: float | None = None

    @property
    def already_applied(self) -> bool:
        return self.review_state == "kept" and (
            self.application_state in APPLIED_STATES or bool(self.applied_at)
        )


def _facts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(f"SELECT {rc.FACT_COLS}, last_seen FROM jobs ORDER BY id").fetchall()


def label_date(iso: str | None) -> str:
    text = (iso or "")[:10]
    try:
        return datetime.date.fromisoformat(text).strftime("%b %-d")
    except ValueError:
        return ""


def _evidence(job: sqlite3.Row, other: sqlite3.Row, geo: DiscoveryConfig) -> tuple[str, ...]:
    a, b = rc.place_of(job, geo), rc.place_of(other, geo)
    if a.remote and b.remote:
        metro = "both remote"
    else:
        metro = f"same metro: {job['geo_city'] or '?'} / {other['geo_city'] or '?'}"
    title = (
        "same title"
        if (job["title"] or "").strip() == (other["title"] or "").strip()
        else f"same title: {job['title']} / {other['title']}"
    )
    seen_a, seen_b = label_date(job["first_seen"]), label_date(other["first_seen"])
    seen = f"both first seen {seen_a}" if seen_a == seen_b else f"first seen {seen_a} / {seen_b}"
    return (title, metro, seen)


def _applied_row(row: sqlite3.Row) -> bool:
    return row["review_state"] == "kept" and (
        (row["application_state"] or "") in APPLIED_STATES or bool(row["applied_at"])
    )


def _same_role_pair(
    job: sqlite3.Row, other: sqlite3.Row, radius_miles: float, geo: DiscoveryConfig
) -> bool:
    """`other` is the same employer's same title in another cluster and a loosely
    shared metro. Says nothing about whether either was applied to (the title rule
    is the caller's: `SameRoleIndex` groups by normalized title)."""
    if job["cluster_id"] is not None and job["cluster_id"] == other["cluster_id"]:
        return False
    key = company_key(job["company"] or "")
    if not key or key != company_key(other["company"] or ""):
        return False
    return rc.same_metro(rc.place_of(job, geo), rc.place_of(other, geo), radius_miles, loose=True)


def _previous(
    job: sqlite3.Row, other: sqlite3.Row, radius_miles: float, geo: DiscoveryConfig
) -> bool:
    """`other` is an application of this same employer's same title (module doc)."""
    return _applied_row(other) and _same_role_pair(job, other, radius_miles, geo)


def _matches(
    job: sqlite3.Row, other: sqlite3.Row, radius_miles: float, geo: DiscoveryConfig
) -> bool:
    gap = rc.days_apart(job["first_seen"], other["first_seen"])
    if gap is None or gap > SUGGEST_WINDOW_DAYS:
        return False
    return rc.same_metro(rc.place_of(job, geo), rc.place_of(other, geo), radius_miles, loose=False)


class SameRoleIndex:
    """Every job's matching facts, loaded once (two queries) so a whole list
    can be checked without a query per row."""

    def __init__(self, conn: sqlite3.Connection, radius_miles: float, geo: DiscoveryConfig):
        self.radius = radius_miles
        self.geo = geo
        self.rows = {int(r["id"]): r for r in _facts(conn)}
        self.decided = rc.decisions(conn)
        self.by_title: dict[str, list[sqlite3.Row]] = {}
        for r in self.rows.values():
            title = normalize_role_title(r["title"])
            if title:
                self.by_title.setdefault(title, []).append(r)

    def for_job(self, job_id: int) -> list[Candidate]:
        job = self.rows.get(job_id)
        if job is None:
            return []
        return [
            _candidate(job, other, decision, kind, self.geo)
            for other, decision, kind in self._scan(job, require_applied=True)
        ]

    def peer_ids(self, job_id: int) -> list[int]:
        """Jobs that may be the same role as `job_id`, whatever the user has
        applied to: the agency/employer pair and the same-employer repost, minus
        pairs ruled 'different'. For the recruiter-submission check; suggestions
        (`for_job`) keep the applied gate on same-employer reposts."""
        job = self.rows.get(job_id)
        if job is None:
            return []
        return [int(o["id"]) for o, _d, _k in self._scan(job, require_applied=False)]

    def _scan(
        self, job: sqlite3.Row, *, require_applied: bool
    ) -> Iterable[tuple[sqlite3.Row, str, str]]:
        job_id = int(job["id"])
        job_is_agency = is_agency(job["company"] or "")
        for other in self.by_title.get(normalize_role_title(job["title"]), []):
            if other["id"] == job_id:
                continue
            decision = self.decided.get(rc.pair_of(job_id, int(other["id"])), "")
            if decision == rc.DIFFERENT:
                continue
            kind = self._kind(job, other, job_is_agency, require_applied)
            if kind:
                yield other, decision, kind

    def _kind(
        self, job: sqlite3.Row, other: sqlite3.Row, job_is_agency: bool, require_applied: bool
    ) -> str:
        """Which suggestion `other` is for `job`, or "" for none."""
        other_is_agency = is_agency(other["company"] or "")
        if other_is_agency != job_is_agency:
            if not _matches(job, other, self.radius, self.geo):
                return ""
            return EMPLOYER if job_is_agency else AGENCY
        if job_is_agency:
            return ""
        pair = _previous if require_applied else _same_role_pair
        return PREVIOUS if pair(job, other, self.radius, self.geo) else ""


def _candidate(
    job: sqlite3.Row, other: sqlite3.Row, decision: str, kind: str, geo: DiscoveryConfig
) -> Candidate:
    return Candidate(
        job_id=int(other["id"]),
        company=other["company"] or "",
        title=other["title"] or "",
        company_key=company_key(other["company"] or ""),
        kind=kind,
        review_state=other["review_state"] or "",
        application_state=other["application_state"] or "",
        applied_at=other["applied_at"] or "",
        source_state=other["source_state"] or "",
        decision=decision,
        evidence=_evidence(job, other, geo),
    )


def possible_same_role_batch(
    conn: sqlite3.Connection, job_ids: Iterable[int], radius_miles: float, geo: DiscoveryConfig
) -> dict[int, list[Candidate]]:
    """Candidates for many jobs at once: a fixed number of queries, whatever
    the list length. Jobs with none are left out."""
    index = SameRoleIndex(conn, radius_miles, geo)
    found = {jid: index.for_job(jid) for jid in job_ids}
    return {jid: cands for jid, cands in found.items() if cands}


def possible_same_role(
    conn: sqlite3.Connection, job_id: int, radius_miles: float, geo: DiscoveryConfig
) -> list[Candidate]:
    """Candidates for one job, each with a JD overlap figure when there is
    meaningful overlap (supporting evidence only)."""
    cands = SameRoleIndex(conn, radius_miles, geo).for_job(job_id)
    if not cands:
        return []
    ids = [job_id, *(c.job_id for c in cands)]
    jds = {
        int(r["id"]): r["raw_jd"] or ""
        for r in conn.execute(
            f"SELECT id, raw_jd FROM jobs WHERE id IN ({','.join('?' for _ in ids)})", ids
        )
    }
    return [_with_overlap(c, jd_overlap(jds.get(job_id, ""), jds.get(c.job_id, ""))) for c in cands]


def _with_overlap(c: Candidate, overlap: float) -> Candidate:
    if overlap < MIN_SHOWN_OVERLAP:
        return c
    evidence = (*c.evidence, f"description overlap {round(overlap * 100)}%")
    return replace(c, jd_overlap=overlap, evidence=evidence)


def jd_overlap(a: str, b: str) -> float:
    """Share of the shorter description's word 5-grams found in the other."""
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def _shingles(text: str) -> set[tuple[str, ...]]:
    words = _WORD.findall((text or "").lower())
    return {tuple(words[i : i + SHINGLE_WORDS]) for i in range(len(words) - SHINGLE_WORDS + 1)}


def group_by_company(cands: Iterable[Candidate]) -> dict[str, list[Candidate]]:
    """Candidates keyed by employer (company_key). More than one key means the
    match is ambiguous: show them all, decide nothing."""
    out: dict[str, list[Candidate]] = {}
    for c in cands:
        out.setdefault(c.company_key, []).append(c)
    return out

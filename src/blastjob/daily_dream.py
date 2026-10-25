"""The weekly Dream list section of the daily digest (#90, ADR 0056).

A far on-site role from a dream-list company is kept by the geo exception (`jobs.dream_far`),
but it is never announced in the daily digest: it waits for one weekly section, on the
search-firm weekday (`discovery.search_firm_weekday`, no setting of its own). A dream role that
passes geo normally (remote or near) stays in the daily digest with its badge.

`pending_dream_jobs` applies the same eligibility rules as `daily.pending_jobs` (active,
surfaced or queued, no decided cluster member), plus current dream membership, the surface
threshold and `dream_notified_at=''`. Delivery state is `dream_notified_at`, stamped per
rendered job only after the digest was sent; a digest chunk Discord accepted before a later
one failed is re-sent next time (at-least-once, as in the job digest). Kept out of `daily.py`,
which is past the file-size budget. See docs/how-it-works/daily.md.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterable

from blastjob.core.company import company_key
from blastjob.core.dream_rule import DreamRule
from blastjob.core.target_store import dream_keys
from blastjob.discovery.searchfirms import due_today
from blastjob.models.config import BlastJobConfig, DiscoveryConfig

MAX_DREAM_JOBS = 15

# The columns the digest line needs; shared with `daily.pending_jobs`.
DIGEST_COLUMNS = (
    "id, company, title, url, alignment_score, review_state, "
    "geo_location_raw, geo_remote, comp_min, comp_max, comp_provenance, "
    "cluster_id, source, source_state, employer_link_source, employer_link_external_id, "
    "employer_link_url"
)

# A row whose cluster holds a kept or archived member is not a new role.
CLUSTER_NOT_DECIDED = (
    "NOT (cluster_id IS NOT NULL AND EXISTS (SELECT 1 FROM jobs d "
    "WHERE d.cluster_id=j.cluster_id AND d.id<>j.id "
    "AND d.review_state IN ('kept','archived')))"
)


def pending_dream_jobs(
    conn: sqlite3.Connection, threshold: int, disc: DiscoveryConfig | None = None
) -> list[sqlite3.Row]:
    """Far dream roles the weekly section has not carried yet, best fit first."""
    rows = conn.execute(
        f"SELECT {DIGEST_COLUMNS} FROM jobs j WHERE source_state='active' "
        "AND dream_far=1 AND COALESCE(dream_notified_at,'')='' "
        "AND review_state IN ('surfaced','queued') AND alignment_score >= ? "
        f"AND {CLUSTER_NOT_DECIDED} "
        "ORDER BY alignment_score DESC, id ASC",
        (threshold,),
    ).fetchall()
    if disc is not None:  # the geo-exception rule (core/dream_rule.py)
        rule = DreamRule.load(conn, disc)
        return [r for r in rows if rule.applies(r["company"] or "", r["source"] or "")]
    dreams = dream_keys(conn)
    return [r for r in rows if company_key(r["company"] or "") in dreams]


def weekly_jobs(cfg: BlastJobConfig, conn: sqlite3.Connection, today: dt.date) -> list[sqlite3.Row]:
    """The roles for today's digest: the pending ones on the weekly day, none on any other."""
    if not due_today(today, cfg.discovery.search_firm_weekday):
        return []
    return pending_dream_jobs(conn, cfg.discovery.alignment_threshold, cfg.discovery)


def rendered_dream_jobs(jobs: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """The roles `build_digest` actually renders; the rest stay unstamped and lead next week."""
    return list(jobs)[:MAX_DREAM_JOBS]


def mark_dream_notified(
    conn: sqlite3.Connection, job_ids: Iterable[int], *, now: dt.datetime | None = None
) -> None:
    when = (now or dt.datetime.now()).isoformat(timespec="seconds")
    conn.executemany(
        "UPDATE jobs SET dream_notified_at=? WHERE id=?", [(when, int(i)) for i in job_ids]
    )
    conn.commit()

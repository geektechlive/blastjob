"""Re-score one job after its description changed (issue #4).

Undecided rows (new/surfaced/queued) go through the ordinary scoped
`run_matching` pass: geo, prefilter, scoring and the gate, with its CAS on
review_state. A kept row is the common case (a Shortlist job blocked from
tailoring by a snippet), and `run_matching` never considers it, so it gets a
score-only update that never touches review_state: the user's Keep stands, but
the fit number now reflects the real posting. Archived rows are left alone.

The kept path is score-only: it does not infer comp from the new text the way
`run_matching` does for undecided rows. A kept row keeps its old score until
the new one lands (`dbc.set_pasted_jd`), so that stale score is dropped from
the reuse table here, or it would be replayed as the new text's. Every write
is conditioned on the JD that was scored, so a later paste wins a race.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from blastjob.db.client import UNDECIDED_STATES
from blastjob.llm.cost import CostTracker
from blastjob.matching import pipeline
from blastjob.matching.scorer import reuse_key, score_candidates
from blastjob.models.config import BlastJobConfig

log = logging.getLogger(__name__)


@dataclass
class RescoreCounts:
    undecided: int = 0  # undecided rows the scoped matching pass scored
    kept: int = 0  # kept rows whose new score landed
    failed: list[int] = field(default_factory=list)  # kept rows left score_stale=1


async def rescore_jobs(
    cfg: BlastJobConfig, conn: sqlite3.Connection, cost_tracker: CostTracker, job_ids: list[int]
) -> RescoreCounts:
    """Re-score many jobs: undecided in ONE batched matching pass, kept one by one.

    A kept row that fails to score (provider error, no score returned, or the JD
    changed mid-flight) keeps `score_stale=1`, is listed in `failed`, and never
    stops the rest. Archived and missing rows are skipped.
    """
    undecided: list[int] = []
    kept: list[sqlite3.Row] = []
    for jid in dict.fromkeys(job_ids):
        row = conn.execute(
            "SELECT id, title, company, raw_jd, review_state FROM jobs WHERE id=?", (jid,)
        ).fetchone()
        if row is None:
            continue
        if row["review_state"] in UNDECIDED_STATES:
            undecided.append(jid)
        elif row["review_state"] == "kept":
            kept.append(row)
    counts = RescoreCounts()
    if undecided:
        res = await pipeline.run_matching(cfg, conn, cost_tracker, job_ids=undecided)
        counts.undecided = res.scored
    if not cfg.candidate.is_configured():
        return counts
    for row in kept:
        try:
            landed = await score_kept(cfg, conn, cost_tracker, row)
        except Exception:
            log.warning("kept re-score failed for job %s", row["id"], exc_info=True)
            landed = 0
        if landed:
            counts.kept += 1
        else:
            counts.failed.append(int(row["id"]))
    return counts


def stale_kept_ids(conn: sqlite3.Connection, limit: int = 10) -> list[int]:
    """Kept rows whose JD changed under their score, at most `limit`, the OLDEST
    text change first (blank stamp, a paste, before any dated one), then id.

    Ordering by id alone starved: the lowest ids were retried first forever. This
    is only a partial rotation: a re-score that keeps failing leaves the row's
    stamp unchanged, so `limit` or more permanently failing rows would still crowd
    out the rest. A durable fix needs an attempt marker column (a migration)."""
    rows = conn.execute(
        "SELECT id FROM jobs WHERE review_state='kept' AND score_stale=1 "
        "ORDER BY COALESCE(jd_fetched_at,'') ASC, id LIMIT ?",
        (limit,),
    ).fetchall()
    return [int(r["id"]) for r in rows]


async def rescore_job(
    cfg: BlastJobConfig, conn: sqlite3.Connection, cost_tracker: CostTracker, job_id: int
) -> int:
    """Score `job_id` against its current JD; returns how many rows were scored."""
    row = conn.execute(
        "SELECT id, title, company, raw_jd, review_state FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    if row is None:
        return 0
    if row["review_state"] in UNDECIDED_STATES:
        res = await pipeline.run_matching(cfg, conn, cost_tracker, job_ids=[job_id])
        return res.scored
    if row["review_state"] != "kept" or not cfg.candidate.is_configured():
        return 0
    return await score_kept(cfg, conn, cost_tracker, row)


async def score_kept(
    cfg: BlastJobConfig, conn: sqlite3.Connection, cost_tracker: CostTracker, row: sqlite3.Row
) -> int:
    job_id = int(row["id"])
    company, title, jd = row["company"] or "", row["title"] or "", row["raw_jd"] or ""
    priors = pipeline.load_prior_scores(conn)
    priors.pop(reuse_key(company, title, jd) or "", None)  # this row's own stale score
    scoring = await score_candidates(
        cfg,
        pipeline.work_history_text(cfg),
        [{"job_id": job_id, "title": title, "company": company, "jd": jd}],
        cost_tracker,
        rejections=pipeline.load_rejections(conn),
        prior_scores=priors,
    )
    sc = scoring.scores.get(job_id)
    if sc is None:
        return 0
    cur = conn.execute(
        "UPDATE jobs SET alignment_score=?, rationale=?, match_strengths=?, match_gaps=?, "
        "score_stale=0 "
        "WHERE id=? AND review_state='kept' AND raw_jd IS ?",
        (
            sc["alignment"],
            sc["rationale"],
            "\n".join(sc.get("strengths", [])),
            "\n".join(sc.get("gaps", [])),
            job_id,
            row["raw_jd"],
        ),
    )
    conn.commit()
    return cur.rowcount


_score_kept = score_kept  # old private name, kept for existing callers

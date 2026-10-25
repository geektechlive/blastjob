"""Where the discovery funnel leaks, computed on demand.

The motivating failure: 54% of everything discovered was being killed by the geo
filter BEFORE any LLM call, and nobody knew for a week. `MatchResult` counted
`geo_rejected` and `prefiltered_out` on every run and threw both away, and
`daily_runs` stores only fetched/scored/new/sent -- so the log read "fetched 731,
scored 28, new 6", which looks like a healthy funnel while a real VP role in
the candidate's home region was being binned.

Rather than add a `drop_reason` column and backfill it, this recomputes each
active job's stage from the same deterministic functions the pipeline uses
(`assess_geo`, `classify_scope`, `gate.decide`). No schema change, no migration,
and it can never disagree with the pipeline because it IS the pipeline's logic.
The cost is that it is O(active jobs) per render, which at ~750 rows is a few
milliseconds.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from blastjob.core.dream_rule import DreamRule
from blastjob.matching.gate import below_floor
from blastjob.matching.geo import assess_geo, is_dream_bypass, is_geo_reject
from blastjob.matching.prefilter import classify_scope
from blastjob.models.config import BlastJobConfig

# Ordered as the pipeline applies them, so the page reads top to bottom.
STAGES: list[tuple[str, str]] = [
    ("discovered", "Discovered"),
    ("stale", "Source says gone"),
    ("geo_rejected", "Rejected on location"),
    ("dream_far", "Dream, on-site far"),
    ("prefiltered", "Dropped as an IC role"),
    ("unscored", "Awaiting scoring"),
    ("below_band", "Scored below the band"),
    ("comp_rejected", "Posted comp below floor"),
    ("surfaced", "Surfaced"),
    ("queued", "In the review queue"),
    ("kept", "Kept"),
    ("archived", "Archived by you"),
]


@dataclass
class Funnel:
    counts: dict[str, int] = field(default_factory=dict)
    examples: dict[str, list[dict]] = field(default_factory=dict)
    runs: list[dict] = field(default_factory=list)
    total: int = 0

    def pct(self, stage: str) -> float:
        return (100.0 * self.counts.get(stage, 0) / self.total) if self.total else 0.0


_COLS = (
    "id, source, company, title, alignment_score, review_state, source_state, "
    "geo_remote, geo_city, geo_region, geo_location_raw, geo_status, "
    "comp_provenance, comp_max, comp_currency"
)
_MAX_EXAMPLES = 12


def _is_dream(row: sqlite3.Row, rule: DreamRule | None) -> bool:
    return rule is not None and rule.applies(row["company"] or "", row["source"] or "")


def _geo_verdict(row: sqlite3.Row, cfg: BlastJobConfig):  # noqa: ANN202 - see matching.geo
    return assess_geo(
        remote=bool(row["geo_remote"]),
        city=row["geo_city"] or "",
        region=row["geo_region"] or "",
        location_raw=row["geo_location_raw"] or "",
        cfg=cfg.discovery,
    )


def _pre_scope_stage(
    row: sqlite3.Row, cfg: BlastJobConfig, dreams: DreamRule | None = None
) -> str | None:
    """Everything decidable without the job description text. None means the
    row is still live at this point in the funnel and needs the scope
    classifier (`raw_jd`) to go further -- see `_post_scope_stage`.

    Split out so `build_funnel` only has to fetch `raw_jd` (up to
    `MAX_PASTED_JD_CHARS` = 60,000 chars each) for the rows that actually
    reach the scope check, instead of every row in the table."""
    if row["review_state"] == "kept":
        return "kept"
    if row["review_state"] == "archived":
        return "archived"
    if row["source_state"] != "active":
        return "stale"

    # The same predicate the pipeline uses, so the page never calls a kept role rejected.
    if is_geo_reject(_geo_verdict(row, cfg), dream=_is_dream(row, dreams)):
        return "geo_rejected"
    return None


def _post_scope_stage(
    row: sqlite3.Row, cfg: BlastJobConfig, raw_jd: str, dreams: DreamRule | None = None
) -> str:
    """The rest of `_stage`, for rows `_pre_scope_stage` couldn't resolve."""
    in_scope, _why = classify_scope(
        row["title"] or "", raw_jd, cfg.discovery.title_keep_terms, cfg.discovery.title_drop_terms
    )
    if not in_scope:
        return "prefiltered"

    if row["review_state"] in ("surfaced", "queued"):
        # A far role the dream exception is keeping reads as that, not as an ordinary hit.
        if is_dream_bypass(_geo_verdict(row, cfg), dream=_is_dream(row, dreams)):
            return "dream_far"
        return str(row["review_state"])

    if row["alignment_score"] is None:
        return "unscored"

    floor = cfg.discovery.comp_floor_usd
    if row["comp_provenance"] == "posted" and below_floor(
        row["comp_max"], row["comp_currency"], floor
    ):
        return "comp_rejected"
    return "below_band"


def build_funnel(conn: sqlite3.Connection, cfg: BlastJobConfig, runs: int = 7) -> Funnel:
    """Stage every job, plus the last few daily runs for trend."""
    f = Funnel()
    dreams = DreamRule.load(conn, cfg.discovery)
    rows = conn.execute(f"SELECT {_COLS} FROM jobs").fetchall()
    f.total = len(rows)
    f.counts["discovered"] = len(rows)

    # Two passes so `raw_jd` (up to 60,000 chars each) is only ever fetched for
    # rows that can reach the scope check -- kept/archived/stale rows never
    # need it. Fetched via a WHERE clause (a superset of the exact live set --
    # it can't also express the geo verdict, which is Python-side) rather than
    # an `id IN (...)` list: with ~1,150 jobs today and growing daily, a
    # per-row placeholder list would eventually hit SQLite's bound-variable
    # ceiling and 500 this page.
    stage_of_id: dict[int, str] = {}
    need_scope = False
    for r in rows:
        stage = _pre_scope_stage(r, cfg, dreams)
        if stage is None:
            need_scope = True
        else:
            stage_of_id[r["id"]] = stage

    jd_by_id: dict[int, str] = {}
    if need_scope:
        jd_by_id = {
            jd_row["id"]: jd_row["raw_jd"]
            for jd_row in conn.execute(
                "SELECT id, raw_jd FROM jobs WHERE review_state NOT IN ('kept','archived') "
                "AND source_state='active'"
            )
        }
    for r in rows:
        if r["id"] not in stage_of_id:
            stage_of_id[r["id"]] = _post_scope_stage(r, cfg, jd_by_id.get(r["id"]) or "", dreams)

    for r in rows:
        stage = stage_of_id[r["id"]]
        f.counts[stage] = f.counts.get(stage, 0) + 1
        bucket = f.examples.setdefault(stage, [])
        # Worst-first within a stage: the highest-scoring thing sitting in a
        # reject bucket is the row most worth a second look.
        if len(bucket) < _MAX_EXAMPLES:
            bucket.append(
                {
                    "id": r["id"],
                    "company": r["company"],
                    "title": r["title"],
                    "score": r["alignment_score"],
                    "where": r["geo_location_raw"],
                }
            )
    for stage, bucket in f.examples.items():
        bucket.sort(key=lambda e: -(e["score"] or 0))

    for row in conn.execute(
        "SELECT started_at, fetched, scored, new_jobs, errors, per_source "
        "FROM daily_runs ORDER BY id DESC LIMIT ?",
        (runs,),
    ):
        try:
            sources = json.loads(row["per_source"] or "{}")
        except ValueError:
            sources = {}
        f.runs.append(
            {
                "when": (row["started_at"] or "")[:16],
                "fetched": row["fetched"],
                "scored": row["scored"],
                "new": row["new_jobs"],
                "errors": row["errors"] or "",
                "sources": len(sources),
            }
        )
    return f


def silent_channels(
    conn: sqlite3.Connection, cfg: BlastJobConfig, quiet_days: int = 21
) -> list[dict]:
    """Configured sources that have gone quiet, and retained-search firms that
    have never spoken at all.

    Two different silences, both previously invisible. One firm failed with an
    identical 422 on every weekly run since it was added and contributed nothing
    -- that shape is caught by "configured but never produced". Another firm
    produced exactly one posting and then went dark, which "never produced"
    misses entirely; it needs a staleness check.

    This is a channel where a senior-level role most plausibly lives, so a
    silence here costs more than the raw counts suggest. Derived from jobs
    already in the database rather than a separate run-history table -- nothing
    to persist, nothing to keep in sync, and it cannot report a firm as healthy
    because a bookkeeping row was missed.
    """
    firms = list(getattr(cfg.discovery, "search_firms", []) or [])
    if not firms:
        return []
    cutoff = (date.today() - timedelta(days=quiet_days)).isoformat()
    seen = {
        r["source"]: r
        for r in conn.execute(
            "SELECT source, COUNT(*) n, MAX(last_seen) newest FROM jobs "
            "WHERE source LIKE 'searchfirm:%' GROUP BY source"
        )
    }
    out = []
    for firm in firms:
        slug = getattr(firm, "slug", "") or ""
        row = seen.get(f"searchfirm:{slug}")
        if row is None:
            out.append(
                {
                    "firm": getattr(firm, "name", slug),
                    "postings": 0,
                    "last": "",
                    "state": "never produced a posting",
                }
            )
        elif (row["newest"] or "")[:10] < cutoff:
            out.append(
                {
                    "firm": getattr(firm, "name", slug),
                    "postings": row["n"],
                    "last": (row["newest"] or "")[:10],
                    "state": f"nothing new in over {quiet_days} days",
                }
            )
    return out


def dead_boards(conn: sqlite3.Connection, cfg: BlastJobConfig) -> list[dict]:
    """Configured boards that have produced nothing worth looking at.

    This is the check that would have caught a Greenhouse token pointing at an
    unrelated, same-named company in another state, 38 postings a day,
    none of them ever relevant -- on day one instead of after a week.
    """
    out = []
    for row in conn.execute(
        "SELECT source, COUNT(*) n, "
        "SUM(CASE WHEN alignment_score >= ? THEN 1 ELSE 0 END) good, "
        "MAX(COALESCE(alignment_score, 0)) best "
        "FROM jobs GROUP BY source ORDER BY n DESC",
        (cfg.discovery.alignment_threshold,),
    ):
        if row["n"] >= 10 and row["good"] == 0:
            out.append(
                {
                    "source": row["source"],
                    "postings": row["n"],
                    "best": row["best"],
                }
            )
    return out

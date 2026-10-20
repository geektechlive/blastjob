"""Matching pipeline: score & disposition active, undecided jobs.

Order per job (cheap -> expensive):
  1. geo assessment (keyless)         — skip LLM for clearly-far on-site roles
  2. scope prefilter (deterministic)  — skip LLM for configured drop families
  3. batched LLM alignment score      — only survivors reach here
  4. gate + rank                      — set review_state, alignment, comp

User-decided jobs (review_state kept/archived) are never re-touched. Comp that a
source posted is authoritative; the LLM may supply posted comp it read from a JD
when the source gave none, but it never invents estimates here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Collection
from dataclasses import dataclass, field

from blastjob import config as cfg_mod
from blastjob.core.company import company_key
from blastjob.llm.cost import CallCost, CostTracker, aggregate_calls
from blastjob.matching.fingerprint import scoring_fingerprint
from blastjob.matching.gate import decide
from blastjob.matching.geo import assess_geo
from blastjob.matching.prefilter import classify_scope
from blastjob.matching.scorer import reuse_key, score_candidates
from blastjob.models.config import BlastJobConfig
from blastjob.models.job import SCORER_FEEDBACK_REASONS, CompProvenance, GeoStatus, ReviewState


@dataclass
class MatchResult:
    considered: int = 0
    prefiltered_out: int = 0
    geo_rejected: int = 0
    geo_unknown: int = 0  # location unplaceable -> review queue, not dropped
    scored: int = 0
    surfaced: int = 0
    queued: int = 0
    rejected: int = 0
    scoring_failures: int = 0  # batches lost to LLM/parse failure (jobs stay unscored)
    company_skipped: int = 0  # auto-archived: company previously rejected by the user
    states: dict[str, int] = field(default_factory=dict)
    # Run-level notes for the cockpit run panel / blastjob-daily digest -- never
    # raised as an exception, since an unconfigured install is not a failure.
    notes: list[str] = field(default_factory=list)
    # Everything the model calls of THIS run cost; `.known` is False if any call's
    # usage was unknown. A run that made no call is a known zero.
    cost: CallCost = field(default_factory=CallCost)


# States the matcher owns. kept/archived belong to the user and are never
# overwritten, even when a scoring result lands after the user acted.
MACHINE_STATES = "'new','surfaced','queued'"
_RESET_TO_NEW = (
    f"UPDATE jobs SET review_state='new' WHERE id=? AND review_state IN ({MACHINE_STATES})"
)

# Shown when [candidate] target_titles is unset (including a "<placeholder>"
# value): matching still runs geo + the scope prefilter, but makes no LLM call
# and leaves every surviving job unscored. Surfaced through the same
# run-notes channel the cockpit run panel and blastjob-daily already use.
NO_CANDIDATE_PROFILE_NOTE = (
    "No candidate profile: set candidate.target_titles at /settings or with "
    "`blastjob setup set` (see SETUP.md) — skipped scoring"
)


def work_history_text(cfg: BlastJobConfig) -> str:
    path = cfg_mod.data_dir(cfg) / "work_history.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def auto_archive_rejected_companies(conn: sqlite3.Connection) -> int:
    """A company the user has excluded: every later posting from it is archived
    on arrival, instead of spending an LLM call and surfacing something they
    already said no to.

    The exclusion is read from `company_exclusions` -- an explicit row the user
    can see and delete -- NOT inferred from any job carrying
    archive_reason='company'. The old inference was a one-way door: unarchiving
    a single row left every sibling still satisfying the subquery, so postings
    kept auto-archiving while the user believed the decision was reversed, and
    nothing anywhere ever cleared archive_reason.
    """
    # Match on company_key, not a lowercased string. exclude/unexclude already
    # key on it, so a raw compare let "Fabrikam's Corp." slip past an exclusion
    # recorded for "Fabrikam's Corp" -- identical keys, different strings.
    excluded = {
        r["company_key"] for r in conn.execute("SELECT company_key FROM company_exclusions")
    }
    if not excluded:
        return 0
    rows = conn.execute(
        f"SELECT id, company FROM jobs WHERE review_state IN ({MACHINE_STATES}) AND company <> ''"
    ).fetchall()
    hits = [r["id"] for r in rows if company_key(r["company"] or "") in excluded]
    archived = 0
    for jid in hits:
        # CAS on the machine states: a Keep clicked since the SELECT above wins.
        cur = conn.execute(
            "UPDATE jobs SET review_state='archived', archive_reason='company', "
            "archive_note='auto: company excluded' "
            f"WHERE id=? AND review_state IN ({MACHINE_STATES})",
            (jid,),
        )
        archived += cur.rowcount
    conn.commit()
    return archived


def load_prior_scores(conn: sqlite3.Connection, fingerprint: str) -> dict[str, dict]:
    """Scores already held for an unchanged posting, keyed by `reuse_key`, and
    computed from the CURRENT scoring inputs (`fingerprint`, see
    `matching/fingerprint.py`). A score from an older profile, work history,
    prompt or model is not replayed: it is what the Re-score changed button exists to redo.

    Without this, a repost is re-judged every run. The live evidence: one job-board
    reseller's listing scored 90, 89, 88 and 84 on four consecutive mornings and crossed the
    surface threshold in both directions, so the digest kept re-announcing the
    same role while its band decision flip-flopped on scorer noise. Replaying the
    stored score is what makes an unchanged posting stable — and it is free.

    Keyed on employer + title + JD text, so two companies sharing boilerplate
    stay two jobs.
    """
    out: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT company, title, raw_jd, alignment_score, rationale, "
        "match_strengths, match_gaps, comp_min, comp_max "
        "FROM jobs WHERE alignment_score IS NOT NULL AND raw_jd <> '' "
        "AND score_model_version = ?",
        (fingerprint,),
    ):
        key = reuse_key(r["company"] or "", r["title"] or "", r["raw_jd"] or "")
        if key is None or key in out:
            continue
        out[key] = {
            "alignment": r["alignment_score"],
            "rationale": r["rationale"] or "",
            "strengths": _split_list(r["match_strengths"]),
            "gaps": _split_list(r["match_gaps"]),
            "comp_min": int(r["comp_min"] or 0),
            "comp_max": int(r["comp_max"] or 0),
        }
    return out


def stale_score_count(conn: sqlite3.Connection, fingerprint: str) -> int:
    """Visible (surfaced/queued), active scores that predate `fingerprint`
    (empty = before #29): the ones the Re-score changed button redoes.

    Not every machine state: a 'new' row that geo or the prefilter now skips is
    reset without a model call, so its old score is never re-stamped and would
    keep this count above zero after Re-score changed, for rows on no stage page.
    A gate reject Re-score changed did score lands in 'new' already stamped."""
    row = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE source_state='active' "
        "AND review_state IN ('surfaced','queued') AND alignment_score IS NOT NULL "
        "AND COALESCE(score_model_version,'') <> ?",
        (fingerprint,),
    ).fetchone()
    return int(row[0]) if row else 0


def score_columns(sc: dict, fingerprint: str) -> tuple[list[str], list[object]]:
    """The score SET fragment and its params, for EVERY score writer
    (`_write_score` here, `rescore.score_kept`), so none can land a score
    without the fingerprint it was computed from."""
    sets = [
        "alignment_score=?",
        "rationale=?",
        "match_strengths=?",
        "match_gaps=?",
        "score_model_version=?",
    ]
    params: list[object] = [
        sc["alignment"],
        sc["rationale"],
        "\n".join(sc.get("strengths", [])),
        "\n".join(sc.get("gaps", [])),
        fingerprint,
    ]
    return sets, params


def _split_list(raw: str | None) -> list[str]:
    return [p for p in (raw or "").split("\n") if p.strip()]


def load_rejections(conn: sqlite3.Connection, limit: int = 15) -> list[dict]:
    """Recent level/domain archives as scorer calibration examples."""
    placeholders = ",".join("?" for _ in SCORER_FEEDBACK_REASONS)
    rows = conn.execute(
        "SELECT title, company, archive_reason, archive_note FROM jobs "
        f"WHERE review_state='archived' AND archive_reason IN ({placeholders}) "
        "ORDER BY id DESC LIMIT ?",
        (*SCORER_FEEDBACK_REASONS, limit),
    ).fetchall()
    return [
        {
            "title": r["title"],
            "company": r["company"],
            "reason": r["archive_reason"],
            "note": r["archive_note"],
        }
        for r in rows
    ]


def _load_candidates(
    conn: sqlite3.Connection,
    *,
    only_unscored: bool,
    job_ids: Collection[int] | None = None,
) -> list[sqlite3.Row]:
    """Machine-state jobs to (re)consider. `only_unscored` limits the LLM spend
    to jobs that have never been scored — the daily run's mode, which never
    treats an old score fingerprint as unscored (#29). The cockpit's Re-score
    changed button passes False: a score whose fingerprint is current is
    replayed, any other is redone, so a profile, work-history or rubric change
    takes effect. Re-score all loads the same rows and replays none of them.

    `job_ids`, when given, scopes the pass to those ids only (e.g. a single
    manually-added job) instead of the whole backlog. An explicit empty
    collection means "nothing to score" — it must not degrade to "all jobs"."""
    if job_ids is not None and not job_ids:
        return []
    extra = " AND alignment_score IS NULL" if only_unscored else ""
    params: tuple[int, ...] = ()
    id_clause = ""
    if job_ids is not None:
        placeholders = ",".join("?" for _ in job_ids)
        id_clause = f" AND id IN ({placeholders})"
        params = tuple(job_ids)
    return conn.execute(
        "SELECT id, title, company, raw_jd, comp_provenance, comp_max, comp_currency, "
        "geo_remote, geo_city, geo_region, geo_location_raw "
        "FROM jobs WHERE source_state='active' "
        f"AND review_state IN ({MACHINE_STATES}){extra}{id_clause}",
        params,
    ).fetchall()


def count_candidates(conn: sqlite3.Connection) -> int:
    """How many jobs a full (`only_unscored=False`) pass loads: what the cockpit's
    Re-score all confirm names. An upper bound on model calls, since geo and the
    title prefilter still drop some of them first."""
    return len(_load_candidates(conn, only_unscored=False))


def _set_geo(conn: sqlite3.Connection, job_id: int, status: GeoStatus, dist: float | None) -> None:
    """Persist the geo verdict. Only status + distance are stored, but that is
    lossless: every outcome of `assess_geo` is recoverable from persisted
    columns, so the DB can answer "why was this dropped" without a new column.

      approximate + distance                 -> pass / conditional / far, by the
                                                configured commute + extended radii
      verified + NULL distance + geo_remote=1 -> remote pass
      verified + NULL distance + geo_remote=0 -> affirmative non-WI hard reject
      unknown                                 -> unplaceable, provisional pass

    A denormalised `geo_reason TEXT` would be friendlier for the cockpit, but
    that is a schema change and is not made here.
    """
    conn.execute(
        "UPDATE jobs SET geo_status=?, geo_distance_mi=? WHERE id=?",
        (status.value, dist, job_id),
    )


def _write_score(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    sc: dict,
    state: ReviewState,
    inferred: bool,
    fingerprint: str,
) -> bool:
    """Score, inferred comp and review state in ONE conditional UPDATE; True only
    when it changed the row.

    CAS on review_state: the cockpit may have been open while the LLM ran (daily
    job + a human clicking Keep), and a user decision made mid-run must win over
    the score that arrives after it. CAS on raw_jd: a JD pasted while this batch
    was in flight must not get the old text's score (it is re-scored on its own;
    issue #4). Inferred comp rides in the same statement, additionally guarded on
    the provenance read at load, so a lost race leaves no half-written row and a
    source's newly posted figure is never overwritten by our reading.
    """
    sets, params = score_columns(sc, fingerprint)
    sets.append("review_state=?")
    params.append(state.value)
    where = f"WHERE id=? AND review_state IN ({MACHINE_STATES}) AND raw_jd IS ?"
    where_params: list[object] = [int(row["id"]), row["raw_jd"]]
    if inferred:
        sets += [
            "comp_min=?",
            "comp_max=?",
            "comp_total_est=?",
            "comp_currency='USD'",
            "comp_provenance=?",
        ]
        params += [sc["comp_min"], sc["comp_max"], sc["comp_max"], CompProvenance.INFERRED.value]
        where += " AND comp_provenance IS ?"
        where_params.append(row["comp_provenance"])
    cur = conn.execute(f"UPDATE jobs SET {', '.join(sets)} {where}", (*params, *where_params))
    return cur.rowcount == 1


async def run_matching(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    cost_tracker: CostTracker,
    on_progress: Callable[[int, int], None] | None = None,
    *,
    only_unscored: bool = False,
    job_ids: Collection[int] | None = None,
    rescore_all: bool = False,
) -> MatchResult:
    """Geo, scope prefilter, then model scoring of the machine-state jobs.

    By default a score stamped with the current fingerprint is replayed (free).
    `rescore_all` replays none, so every candidate that passes geo and the
    prefilter goes to the model (the cockpit's Re-score all). `MatchResult.cost`
    totals only the calls this run recorded, so a shared tracker is safe."""
    first_call = len(cost_tracker.calls)
    res = await _run_matching(
        cfg,
        conn,
        cost_tracker,
        on_progress,
        only_unscored=only_unscored,
        job_ids=job_ids,
        rescore_all=rescore_all,
    )
    res.cost = aggregate_calls(cost_tracker.calls[first_call:])
    return res


async def _run_matching(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    cost_tracker: CostTracker,
    on_progress: Callable[[int, int], None] | None,
    *,
    only_unscored: bool,
    job_ids: Collection[int] | None,
    rescore_all: bool,
) -> MatchResult:
    res = MatchResult()
    wh = work_history_text(cfg)
    # Computed from the same `wh` the scorer is given, once per run.
    fingerprint = scoring_fingerprint(cfg, wh)
    res.company_skipped = auto_archive_rejected_companies(conn)
    rejections = load_rejections(conn)
    rows = _load_candidates(conn, only_unscored=only_unscored, job_ids=job_ids)
    res.considered = len(rows)

    to_score: list[dict] = []
    geo_by_id: dict[int, tuple[GeoStatus, bool, bool]] = {}

    for r in rows:
        jid = int(r["id"])
        status, dist, passes, conditional = assess_geo(
            remote=bool(r["geo_remote"]),
            city=r["geo_city"] or "",
            region=r["geo_region"] or "",
            location_raw=r["geo_location_raw"] or "",
            cfg=cfg.discovery,
        )
        _set_geo(conn, jid, status, dist)
        geo_by_id[jid] = (status, passes, conditional)

        # Cheap rejects before spending an LLM call. An UNKNOWN location is NOT
        # a reject: it means the string was unparseable, so the job goes on to
        # be scored and lands in the review queue. Rejecting it here would also
        # leave alignment_score NULL, so the daily run would re-select and
        # re-reject the same job forever.
        if status == GeoStatus.UNKNOWN:
            res.geo_unknown += 1
        elif not passes:
            conn.execute(_RESET_TO_NEW, (jid,))
            res.geo_rejected += 1
            continue
        keep, _reason = classify_scope(
            r["title"] or "",
            r["raw_jd"] or "",
            cfg.discovery.title_keep_terms,
            cfg.discovery.title_drop_terms,
        )
        if not keep:
            conn.execute(_RESET_TO_NEW, (jid,))
            res.prefiltered_out += 1
            continue

        to_score.append(
            {
                "job_id": jid,
                "title": r["title"] or "",
                "company": r["company"] or "",
                "jd": r["raw_jd"] or "",
            }
        )

    conn.commit()

    if not cfg.candidate.is_configured():
        # Fail safe, not loud: geo + the scope prefilter already ran above, but
        # no candidate profile means there is nothing to judge fit against, so
        # the LLM is never called and every surviving job stays unscored.
        res.notes.append(NO_CANDIDATE_PROFILE_NOTE)
        res.states = {
            row["review_state"]: row["n"]
            for row in conn.execute(
                "SELECT review_state, COUNT(*) n FROM jobs GROUP BY review_state"
            ).fetchall()
        }
        return res

    scoring = await score_candidates(
        cfg,
        wh,
        to_score,
        cost_tracker,
        on_progress=on_progress,
        rejections=rejections,
        prior_scores={} if rescore_all else load_prior_scores(conn, fingerprint),
    )
    scores = scoring.scores
    res.scoring_failures = scoring.failed_batches

    row_by_id = {int(r["id"]): r for r in rows}
    for jid, sc in scores.items():
        r = row_by_id[jid]
        alignment = sc["alignment"]

        # Comp: a figure the SOURCE published stays POSTED (verified). A figure
        # the LLM read out of the prose is INFERRED — our reading, not the
        # employer's number. It never hard-rejects at the gate and never renders
        # as "posted", because a guess must not be quotable in a negotiation.
        provenance = r["comp_provenance"]
        comp_max = int(r["comp_max"] or 0)
        currency = r["comp_currency"] or "USD"
        inferred = provenance != CompProvenance.POSTED.value and sc["comp_max"] > 0
        if inferred:
            provenance = CompProvenance.INFERRED.value
            comp_max = sc["comp_max"]
            currency = "USD"  # the scorer prompt only extracts US-dollar annual figures

        status, passes, conditional = geo_by_id[jid]
        outcome = decide(
            alignment=alignment,
            comp_provenance=provenance,
            comp_max=comp_max,
            comp_currency=currency,
            geo_status=status,
            geo_passes=passes,
            cfg=cfg.discovery,
            geo_conditional=conditional,
        )
        if not _write_score(conn, r, sc, outcome.review_state, inferred, fingerprint):
            continue  # the user (or a pasted JD) got there first: nothing changed
        res.scored += 1
        if outcome.review_state == ReviewState.SURFACED:
            res.surfaced += 1
        elif outcome.review_state == ReviewState.QUEUED:
            res.queued += 1
        else:
            res.rejected += 1

    conn.commit()
    res.states = {
        row["review_state"]: row["n"]
        for row in conn.execute(
            "SELECT review_state, COUNT(*) n FROM jobs GROUP BY review_state"
        ).fetchall()
    }
    return res

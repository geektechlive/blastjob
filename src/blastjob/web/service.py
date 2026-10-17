"""Read/write helpers for the web layer. Thin SQL over the jobs table; the app
opens a short-lived connection per request (WAL + busy_timeout make that safe
alongside background discovery/matching writes)."""

from __future__ import annotations

import datetime
import sqlite3

from blastjob.core import events
from blastjob.core import network as net
from blastjob.core.company import company_key
from blastjob.models.config import BlastJobConfig
from blastjob.models.job import ReviewState
from blastjob.web.stages import stage_of

_COLS = (
    "id, source, company, title, url, alignment_score, rationale, cluster_id, "
    "comp_min, comp_max, comp_total_est, comp_currency, comp_provenance, "
    "geo_remote, geo_city, geo_region, geo_location_raw, geo_status, geo_distance_mi, "
    "review_state, build_state, application_state, applied_at, app_notes, "
    "archive_reason, archive_note, "
    "next_action_due, match_strengths, match_gaps, first_seen, last_seen, source_state, "
    "posting_check, posting_checked_at, posting_closed_at, posting_check_manual, "
    "employer_link_source, employer_link_external_id, employer_link_url, stage_changed_at"
)

# SQL literal list of the closure-lifecycle states (core.closure.AUTOMATION_STATES;
# duplicated here because core.closure imports the web layer).
_CLOSURE_STATES_SQL = "'posting_closed','filled'"


def stage_stamp() -> str:
    """The `stage_changed_at` value for a move made now."""
    return datetime.datetime.now().isoformat(timespec="seconds")


def cluster_overrides(
    conn: sqlite3.Connection, rows: list[sqlite3.Row]
) -> tuple[dict[int, sqlite3.Row], frozenset[int], dict[int, int]]:
    """A cluster has ONE review state: the most user-decided one among its members.

    The list queries filter on `source_state='active'` (and a score floor), so a
    member the user already decided on can be missing from `rows` entirely — a
    kept posting whose source went stale, or a manual record with no score. Its
    live twin then survives as an apparently undecided row. This looks up every
    member of every cluster present in `rows`, ignoring those filters, and returns:

      - `kept_row`:      cluster_id -> the kept member's row (lowest id wins, so
                         the choice is stable when several members are kept)
      - `rejected`:      clusters with an archived member and no kept member —
                         the user said no to this role, so drop it
      - `total_members`: cluster_id -> full member count, for the "+N sources"
                         badge, which is about the real world rather than about
                         what this query happened to select

    One batched query, never per row.
    """
    cids = sorted({r["cluster_id"] for r in rows if r["cluster_id"] is not None})
    if not cids:
        return {}, frozenset(), {}
    placeholders = ",".join("?" for _ in cids)
    members = conn.execute(
        f"SELECT {_COLS} FROM jobs WHERE cluster_id IN ({placeholders}) ORDER BY id", cids
    ).fetchall()

    kept_row: dict[int, sqlite3.Row] = {}
    archived: set[int] = set()
    total: dict[int, int] = {}
    for m in members:
        cid = int(m["cluster_id"])
        total[cid] = total.get(cid, 0) + 1
        if m["review_state"] == ReviewState.KEPT.value:
            kept_row.setdefault(cid, m)
        elif m["review_state"] == ReviewState.ARCHIVED.value:
            archived.add(cid)
    return kept_row, frozenset(archived - set(kept_row)), total


def collapse_clusters(
    rows: list[sqlite3.Row],
    *,
    kept_row: dict[int, sqlite3.Row] | None = None,
    rejected: frozenset[int] = frozenset(),
    total_members: dict[int, int] | None = None,
) -> list[dict]:
    """Collapse cross-source duplicates (rows sharing a non-null `cluster_id` —
    the same role posted on, say, a company board AND Adzuna) to one row per
    cluster: the highest `alignment_score` wins, ties broken by lowest `id`.
    Rows with no cluster_id pass through unchanged. Every returned row is a
    dict (so every `_COLS` column stays available for templates/tests that key
    off it) annotated with:
      - `dupes`: how many other rows in the same cluster were folded in (0 if
        none / not clustered)
      - `agency`: whether the company is a staffing/recruiting agency rather
        than the employer (`compare.is_agency`)

    The keyword arguments carry a cluster's *decided* state, from
    `cluster_overrides` — they all default to today's score-only behavior, so a
    caller that has not opted in is unaffected:
      - `kept_row`:      emit this row for the cluster instead of the winner
      - `rejected`:      drop these clusters entirely
      - `total_members`: authoritative member count for the `dupes` badge

    Input order is preserved — callers already sort by score/comp, and the
    winning row within a cluster keeps its original position."""
    from blastjob.web.compare import is_agency

    kept = kept_row or {}
    totals = total_members or {}

    groups: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        cid = r["cluster_id"]
        if cid is not None:
            groups.setdefault(cid, []).append(r)

    winner_id: dict[int, int] = {}
    local_dupes: dict[int, int] = {}
    for cid, members in groups.items():
        best = min(members, key=lambda r: (-(r["alignment_score"] or 0), r["id"]))
        winner_id[cid] = best["id"]
        local_dupes[cid] = len(members) - 1

    def annotate(row: sqlite3.Row, cid: int | None) -> dict:
        d = dict(row)
        if cid is None:
            d["dupes"] = 0
        elif cid in totals:
            d["dupes"] = max(totals[cid] - 1, 0)
        else:
            d["dupes"] = local_dupes.get(cid, 0)
        d["agency"] = is_agency(row["company"] or "")
        return d

    out: list[dict] = []
    emitted: set[int] = set()
    for r in rows:
        cid = r["cluster_id"]
        if cid is None:
            out.append(annotate(r, None))
            continue
        if cid in rejected:
            continue
        if cid in kept:
            if cid in emitted:
                continue
            emitted.add(cid)
            out.append(annotate(kept[cid], cid))
            continue
        if r["id"] != winner_id[cid]:
            continue
        out.append(annotate(r, cid))
    return out


def job_detail(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_COLS}, raw_jd, jd_pasted_at, jd_source, jd_fetched_at, jd_source_ref, "
        "jd_lookup_at, (SELECT h.url FROM jobs h WHERE jobs.jd_source LIKE 'employer:%' "
        "AND h.source = substr(jobs.jd_source, 10) AND h.external_id = jobs.jd_source_ref "
        "LIMIT 1) AS jd_link_url FROM jobs WHERE id=?",
        (job_id,),
    ).fetchone()


def annotate_network(conn: sqlite3.Connection, rows: list) -> list[dict]:
    """Attach `warm_count` (connections at that company) and `is_alumni`
    (the user already worked there) to a list of job rows. One batched query
    across every company in `rows` — see `net.warm_counts` and
    `cluster_overrides` for the same batched-lookup pattern; never per row."""
    out = [dict(r) for r in rows]
    keys = {company_key(r.get("company") or "") for r in out}
    counts = net.warm_counts(conn, keys)
    for r in out:
        key = company_key(r.get("company") or "")
        r["warm_count"] = counts.get(key, 0)
        r["is_alumni"] = net.is_alumni_company(key)
    return out


def annotate_network_one(conn: sqlite3.Connection, row: sqlite3.Row | None) -> dict | None:
    """Single-row form of `annotate_network`, for the job detail page."""
    if row is None:
        return None
    return annotate_network(conn, [row])[0]


def set_review_state(
    conn: sqlite3.Connection, job_id: int, new_state: ReviewState, expected: list[ReviewState]
) -> bool:
    """CAS transition: only move if currently in an expected state. Returns True
    if this call made the change."""
    placeholders = ",".join("?" for _ in expected)
    cur = conn.execute(
        f"UPDATE jobs SET review_state=? WHERE id=? AND review_state IN ({placeholders})",
        (new_state.value, job_id, *[s.value for s in expected]),
    )
    conn.commit()
    return cur.rowcount == 1


def archive_job(
    conn: sqlite3.Connection,
    job_id: int,
    expected: list[ReviewState],
    *,
    reason: str = "",
    note: str = "",
) -> bool:
    """CAS archive that also records why. `reason` is an ArchiveReason code or
    ''; `note` is free text (kept short)."""
    placeholders = ",".join("?" for _ in expected)
    cur = conn.execute(
        "UPDATE jobs SET review_state='archived', archive_reason=?, archive_note=? "
        f"WHERE id=? AND review_state IN ({placeholders})",
        (reason, note.strip()[:300], job_id, *[s.value for s in expected]),
    )
    ok = cur.rowcount == 1
    if ok and reason == "company":
        # Record the standing decision explicitly. It used to be inferred from
        # this row existing, which made it impossible to reverse.
        row = conn.execute("SELECT company FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row and (row["company"] or "").strip():
            exclude_company(conn, row["company"], note=note)
    conn.commit()
    return ok


def exclude_company(conn: sqlite3.Connection, company: str, *, note: str = "") -> None:
    """Record "never this company" as a policy row the user can later delete."""
    name = (company or "").strip()
    if not name:
        return
    conn.execute(
        "INSERT INTO company_exclusions (company_key, company, reason, created_at) "
        "VALUES (?,?,?,?) ON CONFLICT(company_key) DO NOTHING",
        (
            company_key(name),
            name,
            note.strip()[:300],
            datetime.datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def unexclude_company(conn: sqlite3.Connection, company: str) -> bool:
    """Lift a company-wide exclusion. Returns True if one was actually lifted.

    This does NOT un-archive the individual postings already archived under it;
    those are separate decisions, reversible one at a time via unarchive_job.
    """
    cur = conn.execute(
        "DELETE FROM company_exclusions WHERE company_key=?", (company_key(company or ""),)
    )
    conn.commit()
    return cur.rowcount > 0


def excluded_companies(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT company, company_key, reason, created_at FROM company_exclusions "
        "ORDER BY company COLLATE NOCASE"
    ).fetchall()
    return [dict(r) for r in rows]


def unarchive_job(conn: sqlite3.Connection, job_id: int) -> bool:
    """CAS: archived -> surfaced, clearing archive_reason/archive_note since
    the reason no longer applies once the decision is reversed.

    Reopens only THIS row. It does NOT undo a company-wide exclusion: if
    archive_reason was 'company', matching/pipeline.py still blocks future
    postings from that company on ANY row with archive_reason='company' —
    other rows from the same company keep their own archive_reason untouched
    by this call. See the INTEGRATION PATCH for a proper company-exclusion
    policy design (this function does not attempt one).
    """
    cur = conn.execute(
        "UPDATE jobs SET review_state=?, archive_reason='', archive_note='' "
        "WHERE id=? AND review_state=?",
        (ReviewState.SURFACED.value, job_id, ReviewState.ARCHIVED.value),
    )
    conn.commit()
    return cur.rowcount == 1


# Where a job-detail page's back link returns to, keyed by the list view the
# viewer came from (see back_href).
_BACK_HREFS = {
    "surfaced": "/",
    "queued": "/queue",
    "matches": "/matches",
    "shortlist": "/shortlist",
    "applied": "/applied",
    "closed": "/closed",
    # Links saved before the stages existed.
    "kept": "/shortlist",
}


def back_href(view: str) -> str:
    """Context-aware back link for the job-detail page: return to the list the
    viewer actually came from, not always "/". Unknown or blank views fall
    back to the surfaced list ("/")."""
    return _BACK_HREFS.get(view, "/")


def set_application_state(
    conn: sqlite3.Connection, job_id: int, new_state: str, expected: list[str]
) -> bool:
    """CAS transition of a kept job's application_state. Only moves if the job is
    kept and currently in an expected application_state. Stamps applied_at on the
    first move into 'applied'. Clears closed_prior_state: after a manual move
    there is nothing for a posting reopen to restore.

    Moving a job OUT of posting_closed/filled is the user overruling the closure
    lifecycle, so it pins the verdict (posting_check_manual=1) and clears the
    closure stamp; otherwise the next closure pass would close it again, and the
    21-day sweep would re-file it. A real change stamps stage_changed_at (the
    Closed page sorts by it). Returns True if this call made the change.

    Moving to the state the job already holds is NOT a change: it returns
    False and writes nothing. A real change records its application event
    (`core/events`, source 'state') in the SAME transaction, committed once;
    if the event cannot be written the move is rolled back and the error
    raised, so a state never lands without its event or vice versa."""
    placeholders = ",".join("?" for _ in expected)
    stamp = stage_stamp()
    today = datetime.date.today().isoformat()
    leaving = f"application_state IN ({_CLOSURE_STATES_SQL}) AND ? NOT IN ({_CLOSURE_STATES_SQL})"
    if not conn.in_transaction:
        # Read the prior row and write the move under one write lock.
        conn.execute("BEGIN IMMEDIATE")
    prior = conn.execute(
        "SELECT COALESCE(application_state,'') AS state, COALESCE(applied_at,'') AS applied_at "
        "FROM jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    try:
        cur = conn.execute(
            "UPDATE jobs SET application_state=?, "
            "applied_at = CASE WHEN ?='applied' AND (applied_at IS NULL OR applied_at='') "
            "THEN ? ELSE applied_at END, closed_prior_state='', "
            f"posting_check_manual = CASE WHEN {leaving} THEN 1 ELSE posting_check_manual END, "
            f"posting_closed_at = CASE WHEN {leaving} THEN '' ELSE posting_closed_at END, "
            # Reopening a closed posting is the user saying it is still live: the
            # same pinned verdict as "Still open", or it would stay sunk.
            f"posting_check = CASE WHEN {leaving} THEN 'verified_open' ELSE posting_check END, "
            "stage_changed_at = ? "
            f"WHERE id=? AND review_state='kept' AND application_state IN ({placeholders}) "
            "AND COALESCE(application_state,'') <> ?",
            (
                new_state,
                new_state,
                today,
                new_state,
                new_state,
                new_state,
                stamp,
                job_id,
                *expected,
                new_state,
            ),
        )
        moved = cur.rowcount == 1
        if moved and prior is not None:
            _insert_state_event(conn, job_id, new_state, prior, stamp)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return moved


def _insert_state_event(
    conn: sqlite3.Connection,
    job_id: int,
    new_state: str,
    prior: sqlite3.Row,
    stamp: str,
) -> None:
    """The event for a move that just happened (inside its transaction).

    'applied' is recorded only for the move that stamps applied_at. Going back
    to applied later (Reopen from Closed) is a note: an "Applied" dated today
    would claim a second application that never happened."""
    kind = events.state_event_kind(new_state)
    if kind is None:
        return
    if kind == "applied" and prior["applied_at"]:
        note = f"Back to applied (was {prior['state'] or 'no state'})"
        events.insert_event(conn, job_id, "note", stamp, note=note, source="state")
        return
    events.insert_event(conn, job_id, kind, stamp, source="state")


def set_app_notes(conn: sqlite3.Connection, job_id: int, notes: str) -> bool:
    """Set the free-text application notes on a kept job."""
    cur = conn.execute(
        "UPDATE jobs SET app_notes=? WHERE id=? AND review_state='kept'",
        (notes, job_id),
    )
    conn.commit()
    return cur.rowcount == 1


def set_next_action_due(conn: sqlite3.Connection, job_id: int, due: str) -> bool:
    """Set (or clear, with "") the follow-up due date on a kept job. A malformed
    date is rejected so the reminder logic never chokes on it."""
    due = (due or "").strip()
    if due:
        try:
            datetime.date.fromisoformat(due)
        except ValueError:
            return False
    cur = conn.execute(
        "UPDATE jobs SET next_action_due=? WHERE id=? AND review_state='kept'",
        (due, job_id),
    )
    conn.commit()
    return cur.rowcount == 1


def app_config() -> BlastJobConfig:
    from blastjob import config as cfg_mod

    return cfg_mod.load_config()


# All-page reasons for kept jobs, by stage.
_KEPT_REASONS = {
    "shortlist": "on your shortlist",
    "applied": "in a hiring process ({state})",
    "closed": "finished ({state})",
}


def all_active(conn: sqlite3.Connection, cfg: BlastJobConfig) -> list[dict]:
    """Every active job with a plain-language disposition + reason, so the total
    is fully explorable — nothing is a black box. Ordered best fit first; the
    unscored (filtered, pending) follow, grouped by disposition."""
    from blastjob.matching.geo import assess_geo
    from blastjob.matching.prefilter import classify_scope

    rows = conn.execute(f"SELECT {_COLS} FROM jobs WHERE source_state='active'").fetchall()
    # raw_jd (up to 60,000 pasted chars each) is only needed for the "unscored"
    # branch below (classify_scope) -- every other disposition never reads it,
    # so it's fetched in a second, targeted query instead of for every active
    # row. A WHERE clause rather than an `id IN (...)` list built from
    # `unscored_ids`: with ~1,000 active jobs today and growing daily, a
    # per-row placeholder list would eventually hit SQLite's bound-variable
    # ceiling and 500 this page.
    jd_by_id: dict[int, str] = {
        jd_row["id"]: jd_row["raw_jd"]
        for jd_row in conn.execute(
            "SELECT id, raw_jd FROM jobs WHERE source_state='active' AND alignment_score IS NULL"
        )
    }
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        state = r["review_state"]
        score = r["alignment_score"]
        stage = stage_of(r)
        if stage in _KEPT_REASONS:
            app_state = (r["application_state"] or "").replace("_", " ")
            disp, reason = stage, _KEPT_REASONS[stage].format(state=app_state)
        elif state == "kept":
            disp, reason = "kept", "you kept it"
        elif state == "archived":
            disp, reason = "archived", "you archived it"
        elif state == "surfaced":
            disp, reason = "surfaced", f"{score}% fit, cleared the bar"
        elif state == "queued":
            disp, reason = "queued", "needs a look (borderline / location / comp)"
        elif score is not None:
            disp, reason = "low fit", f"scored {score}%"
        else:
            # unscored: was it geo or the scope prefilter that dropped it?
            _st, _dist, passes, _cond = assess_geo(
                remote=bool(r["geo_remote"]),
                city=r["geo_city"] or "",
                region=r["geo_region"] or "",
                location_raw=r["geo_location_raw"] or "",
                cfg=cfg.discovery,
            )
            keep, _why = classify_scope(
                r["title"] or "",
                jd_by_id.get(r["id"]) or "",
                cfg.discovery.title_keep_terms,
                cfg.discovery.title_drop_terms,
            )
            if not passes:
                disp, reason = "filtered", "outside commute area (not remote)"
            elif not keep:
                disp, reason = "filtered", "different role type (not a leadership role)"
            else:
                disp, reason = "pending", "not scored yet — run Match"
        d["disposition"] = disp
        d["reason"] = reason
        out.append(d)

    order = {
        "surfaced": 0,
        "queued": 1,
        "shortlist": 2,
        "applied": 2,
        "closed": 2,
        "kept": 2,
        "low fit": 3,
        "filtered": 4,
        "archived": 5,
        "pending": 6,
    }
    # Best fit first; disposition only groups the unscored tail and breaks ties.
    out.sort(
        key=lambda j: (
            j["alignment_score"] is None,
            -(j["alignment_score"] or 0),
            order.get(j["disposition"], 9),
        )
    )
    return out

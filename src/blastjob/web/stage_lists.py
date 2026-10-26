"""Page lists for the pipeline stages, their header counts and the board.

Membership comes from `stages` (one rule per stage). This module adds the
cluster rule on top: a cross-source cluster is ONE role, so it shows once, in
the stage its representative sits in, and never again via a sibling:

  - a cluster with a kept member is represented by the kept member furthest
    along (application progress, then fit), so its stage is that member's;
  - otherwise a cluster with an archived member is decided "no" and shows
    nowhere;
  - otherwise the best-fit active surfaced/queued member represents it.

`counts` is built from the same functions, so a header count can never
disagree with the page under it.
"""

from __future__ import annotations

import datetime
import json
import logging
import sqlite3
from typing import Any

from pydantic import ValidationError

from blastjob.models.config import (
    QUIET_AFTER_DAYS_DEFAULT,
    QUIET_AFTER_DAYS_MAX,
    QUIET_AFTER_DAYS_MIN,
    NotifyConfig,
)
from blastjob.web import stages as st
from blastjob.web.apply_link import best_apply_link
from blastjob.web.service import _COLS, cluster_overrides, collapse_clusters

log = logging.getLogger(__name__)

# Pay-dominant ordering, one click away on Surfaced and Matches.
_SORT_COMP_FIRST = (
    "CASE WHEN comp_provenance='posted' THEN 0 ELSE 1 END, comp_total_est DESC, "
    "alignment_score DESC"
)
_SORT_FIT_FIRST = "alignment_score IS NULL, alignment_score DESC"
# Tailoring-build funnel order, a tie-break only. A plain ORDER BY build_state
# sorts alphabetically (failed, none, tailored, tailoring), which is not that.
_BUILD_FUNNEL_RANK = (
    "CASE build_state "
    "WHEN 'none' THEN 0 WHEN 'tailoring' THEN 1 WHEN 'tailored' THEN 2 "
    "WHEN 'failed' THEN 3 ELSE 4 END"
)
# A closed or likely-closed posting sinks below every open one, keeping its own
# order among the sunk. Shortlist: with closure_auto off a proven-closed posting
# stays kept. Surfaced / Queue: closure never moves an undecided job, it only
# stamps the verdict, so the sort is what keeps a dead posting out of the way.
_SINK_CLOSED = "CASE WHEN posting_check IN ('likely_closed','closed') THEN 1 ELSE 0 END"
_SORT_SHORTLIST = f"{_SINK_CLOSED}, {_SORT_FIT_FIRST}, {_BUILD_FUNNEL_RANK}, id"
# Closed: most recently closed first.
_SORT_CLOSED = (
    "COALESCE(NULLIF(stage_changed_at,''), NULLIF(posting_closed_at,''), "
    f"NULLIF(applied_at,''), '') DESC, {_SORT_FIT_FIRST}, id"
)
# Undated follow-ups sort after every dated one.
_NO_DATE = "9999-12-31"

# An "applied" job with no reply after this many days auto-nudges for a follow-up.
FOLLOWUP_STALE_DAYS = 7

# Application progress, weakest to strongest. A cluster's kept members can
# differ only in how far the application has gone (a live pair looked like
# this: one row at 'screen' carrying the notes, one bare row off the ATS
# board). An application in progress outranks a closed twin: a posting can
# close because they are interviewing the user.
_PROGRESS_ORDER: tuple[str, ...] = ("", *st.CLOSED_STATES, *st.IN_PROCESS_STATES)

_UNDECIDED = ("new", "surfaced", "queued")
_UNDECIDED_STAGES: frozenset[str | None] = frozenset({st.SURFACED, st.QUEUED})
_BOARD_BUILD_LANES = ("none", "tailoring", "tailored", "failed")


def progress_rank(row: Any) -> int:
    """How far an application has gone (`_PROGRESS_ORDER`); 0 when unknown."""
    if "application_state" not in row.keys():
        return 0
    try:
        return _PROGRESS_ORDER.index((row["application_state"] or "").strip())
    except ValueError:
        return 0


def representative_key(row: Any) -> tuple[int, int, int]:
    """Which kept member represents a cluster (the max wins): furthest along,
    then best fit, then the oldest id. Also picks the survivor when a merge
    reconciles two applications (web/same_role), so the row that survives is
    the row the lists already showed."""
    return (progress_rank(row), row["alignment_score"] or 0, -row["id"])


def collapse_kept(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """One row per cross-source cluster within a list of kept jobs: the member
    furthest along wins; ties keep the caller's order. Unclustered rows pass
    through. Rows are returned unchanged (not annotated)."""
    best: dict[int, int] = {}
    for i, r in enumerate(rows):
        cid = r["cluster_id"]
        if cid is None:
            continue
        if cid not in best or progress_rank(r) > progress_rank(rows[best[cid]]):
            best[cid] = i
    return [r for i, r in enumerate(rows) if r["cluster_id"] is None or best[r["cluster_id"]] == i]


def _representative(members: list[sqlite3.Row]) -> sqlite3.Row | None:
    kept = [m for m in members if m["review_state"] == "kept"]
    if kept:
        return max(kept, key=representative_key)
    if any(m["review_state"] == "archived" for m in members):
        return None
    live = [m for m in members if st.stage_of(m) in _UNDECIDED_STAGES]
    if not live:
        return None
    return min(live, key=lambda m: (-(m["alignment_score"] or 0), m["id"]))


def resolve_clusters(
    conn: sqlite3.Connection, rows: list[sqlite3.Row]
) -> tuple[dict[int, tuple[str | None, int | None]], dict[int, int]]:
    """For every cluster present in `rows`: (stage, representative id), plus
    each cluster's full member count. One batched query over ALL members,
    ignoring source_state and score filters, never per row."""
    return _resolve(_cluster_members(conn, rows))


def _cluster_members(
    conn: sqlite3.Connection, rows: list[sqlite3.Row]
) -> dict[int, list[sqlite3.Row]]:
    """Every member of every cluster present in `rows`, in ONE query."""
    cids = sorted({r["cluster_id"] for r in rows if r["cluster_id"] is not None})
    if not cids:
        return {}
    placeholders = ",".join("?" for _ in cids)
    members = conn.execute(
        f"SELECT {_COLS} FROM jobs WHERE cluster_id IN ({placeholders}) ORDER BY id", cids
    ).fetchall()
    groups: dict[int, list[sqlite3.Row]] = {}
    for m in members:
        groups.setdefault(int(m["cluster_id"]), []).append(m)
    return groups


def _resolve(
    groups: dict[int, list[sqlite3.Row]],
) -> tuple[dict[int, tuple[str | None, int | None]], dict[int, int]]:
    resolved: dict[int, tuple[str | None, int | None]] = {}
    for cid, ms in groups.items():
        rep = _representative(ms)
        resolved[cid] = (st.stage_of(rep), rep["id"]) if rep is not None else (None, None)
    return resolved, {cid: len(ms) for cid, ms in groups.items()}


def _stage_list(conn: sqlite3.Connection, stage: str, order_sql: str) -> list[dict]:
    """Rows in `stage`, in `order_sql`, with clusters resolved to one row that
    lives in exactly one stage. Each row is annotated with `dupes`, `agency`,
    `stage` and `apply` (the cluster's best apply link, or None)."""
    from blastjob.web.compare import is_agency

    rows = conn.execute(
        f"SELECT {_COLS} FROM jobs WHERE {st.STAGE_WHERE[stage]} ORDER BY {order_sql}"
    ).fetchall()
    groups = _cluster_members(conn, rows)
    resolved, totals = _resolve(groups)
    out: list[dict] = []
    for r in rows:
        cid = r["cluster_id"]
        if cid is not None and resolved.get(cid) != (stage, r["id"]):
            continue
        d = dict(r)
        d["dupes"] = max(totals.get(cid, 1) - 1, 0) if cid is not None else 0
        d["agency"] = is_agency(r["company"] or "")
        d["stage"] = stage
        d["apply"] = best_apply_link(groups.get(cid, [r]) if cid is not None else [r])
        out.append(d)
    return out


def surfaced_jobs(conn: sqlite3.Connection, sort: str = "fit") -> list[dict]:
    """Cleared the bar: fit first, then newest. sort="comp" is pay-dominant."""
    order = _SORT_COMP_FIRST if sort == "comp" else _SORT_FIT_FIRST
    return _stage_list(conn, st.SURFACED, f"{_SINK_CLOSED}, {order}, first_seen DESC, id")


def queued_jobs(conn: sqlite3.Connection) -> list[dict]:
    """Borderline fit, unverified inferred comp, or a location that needs a look."""
    return _stage_list(conn, st.QUEUED, f"{_SINK_CLOSED}, {_SORT_FIT_FIRST}, first_seen DESC, id")


def shortlist_jobs(conn: sqlite3.Connection) -> list[dict]:
    """Kept, not applied: best fit first, closed and likely-closed postings sunk to the
    bottom (still fit-ordered among themselves); build stage breaks ties."""
    return _stage_list(conn, st.SHORTLIST, _SORT_SHORTLIST)


_WARNED_QUIET_VALUES: set[str] = set()


def stored_quiet_after_days(conn: sqlite3.Connection) -> int:
    """`notify.quiet_after_days` as stored, validated by `NotifyConfig`'s own bounds. Anything
    unset, malformed or out of range falls back to the default (with a warning), so a bad stored
    value never reaches the comparison or crashes a page. Read straight from the settings table
    so a page, the job page and the digest agree without each caller threading the config."""
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='notify.quiet_after_days'"
        ).fetchone()
    except sqlite3.Error:
        return QUIET_AFTER_DAYS_DEFAULT
    if row is None:
        return QUIET_AFTER_DAYS_DEFAULT
    try:
        return NotifyConfig(quiet_after_days=json.loads(row[0])).quiet_after_days
    except (ValueError, TypeError, ValidationError):
        if row[0] not in _WARNED_QUIET_VALUES:  # once per distinct bad value, not per render
            _WARNED_QUIET_VALUES.add(row[0])
            log.warning(
                "notify.quiet_after_days is not a whole number of days from %d to %d; using %d",
                QUIET_AFTER_DAYS_MIN,
                QUIET_AFTER_DAYS_MAX,
                QUIET_AFTER_DAYS_DEFAULT,
            )
        return QUIET_AFTER_DAYS_DEFAULT


def _date_of(text: Any) -> datetime.date | None:
    """The calendar day of an ISO date or datetime string, or None when it is not one."""
    try:
        return datetime.date.fromisoformat(str(text or "").strip()[:10])
    except ValueError:
        return None


def quiet_days(row: Any, today: datetime.date) -> int | None:
    """Days since the later of the last stage change and the newest application event, or
    None when neither date is usable. Malformed dates are skipped; a future anchor is 0 or
    less, so it never reads as quiet."""
    keys = row.keys()
    dates = [
        d
        for d in (
            _date_of(row["stage_changed_at"]) if "stage_changed_at" in keys else None,
            _date_of(row["latest_event_at"]) if "latest_event_at" in keys else None,
        )
        if d is not None
    ]
    return (today - max(dates)).days if dates else None


def followup_due(
    row: Any,
    today: datetime.date,
    quiet_after_days: int = QUIET_AFTER_DAYS_DEFAULT,
) -> str:
    """Reason a kept job is due for a follow-up, or "" if it isn't. An explicit
    next_action_due that has arrived always counts; otherwise an "applied" job
    that's gone quiet past the stale threshold auto-nudges, and a screen or interview
    quiet for `quiet_after_days` does too. Quiet is measured from the later of the stage
    change and the newest event (`quiet_days`). At or past the quiet threshold the reason
    also offers marking the job ghosted; it never does that itself."""
    if row["application_state"] not in st.LIVE_STATES:
        return ""
    # Applied, and the posting has since closed: worth a nudge at once, since
    # it goes to `filled` on its own after 21 quiet days.
    closed_at = (row["posting_closed_at"] or "") if "posting_closed_at" in row.keys() else ""
    if row["application_state"] == "applied" and closed_at:
        return f"posting closed {closed_at[:10]} — confirm where this stands"
    due = (row["next_action_due"] or "").strip()
    if due:
        try:
            if datetime.date.fromisoformat(due) <= today:
                return f"follow-up due {due}"
        except ValueError:
            pass
        return ""
    quiet = quiet_days(row, today)
    if row["application_state"] == "applied":
        try:
            days = (today - datetime.date.fromisoformat((row["applied_at"] or "").strip())).days
        except ValueError:
            return ""
        if days < FOLLOWUP_STALE_DAYS:
            return ""
        reason = f"applied {days} days ago, no update logged"
        if quiet is not None and quiet >= quiet_after_days:
            reason += ", or mark it ghosted"
        return reason
    if quiet is not None and quiet >= quiet_after_days:
        return f"no update in {quiet} days: follow up, or mark it ghosted"
    return ""


def followup_reason(
    row: Any,
    conn: sqlite3.Connection | None = None,
    today: datetime.date | None = None,
) -> str:
    """The follow-up nudge for one job row, or "" if none. With `conn`, the job's newest
    application event and the stored quiet threshold are read, so the job page says what
    the Applied list and the digest say."""
    day = today or datetime.date.today()
    if conn is None:
        return followup_due(row, day)
    from blastjob.core import events

    values = dict(row)
    latest = events.latest_event(conn, [int(row["id"])]).get(int(row["id"]))
    values["latest_event_at"] = latest["at"] if latest is not None else ""
    return followup_due(values, day, stored_quiet_after_days(conn))


def _followup_date(job: dict) -> str:
    due = (job.get("next_action_due") or "").strip()
    return due or (job.get("applied_at") or "").strip() or _NO_DATE


def applied_jobs(
    conn: sqlite3.Connection,
    today: datetime.date | None = None,
    quiet_after_days: int | None = None,
) -> list[dict]:
    """In a hiring process: furthest stage first (offer > interview > screen >
    applied), then follow-ups that are due, most overdue first. Each row carries
    `followup_reason`. `quiet_after_days` defaults to the stored setting."""
    day = today or datetime.date.today()
    quiet = stored_quiet_after_days(conn) if quiet_after_days is None else quiet_after_days
    jobs = _with_latest_event(conn, _stage_list(conn, st.APPLIED, f"{_SORT_FIT_FIRST}, id"))
    for j in jobs:
        j["followup_reason"] = followup_due(j, day, quiet)
    return sorted(
        jobs,
        key=lambda j: (
            -st.IN_PROCESS_STATES.index(j["application_state"]),
            not j["followup_reason"],
            _followup_date(j),
        ),
    )


def _with_latest_event(conn: sqlite3.Connection, jobs: list[dict]) -> list[dict]:
    """Annotate each row with its newest application event (`latest_event`:
    label + short date, or None), for the whole list in one query."""
    from blastjob.core import events

    latest = events.latest_event(conn, [j["id"] for j in jobs])
    for j in jobs:
        ev = latest.get(j["id"])
        j["latest_event_at"] = ev["at"] if ev is not None else ""
        j["latest_event"] = (
            {
                "label": events.KIND_LABELS.get(ev["kind"], ev["kind"]),
                "date": datetime.date.fromisoformat(ev["at"][:10]).strftime("%b %-d"),
            }
            if ev is not None
            else None
        )
    return jobs


def closed_jobs(conn: sqlite3.Connection) -> list[dict]:
    """Finished: most recently closed first."""
    return _stage_list(conn, st.CLOSED, _SORT_CLOSED)


def followups_due(
    conn: sqlite3.Connection,
    today: datetime.date | None = None,
    quiet_after_days: int | None = None,
) -> list[dict]:
    """Applied-stage jobs due a follow-up, most overdue first."""
    due = [j for j in applied_jobs(conn, today, quiet_after_days) if j["followup_reason"]]
    return sorted(due, key=_followup_date)


STAGE_LISTS = {
    st.SURFACED: surfaced_jobs,
    st.QUEUED: queued_jobs,
    st.SHORTLIST: shortlist_jobs,
    st.APPLIED: applied_jobs,
    st.CLOSED: closed_jobs,
}


def matches(conn: sqlite3.Connection, floor: int = 65, sort: str = "fit") -> list[dict]:
    """Every undecided, active job scored at or above a browse floor. A cluster
    with a kept or archived member is decided and left out. Also collapses the
    same role posted across cities under one source (company + title)."""
    order = _SORT_COMP_FIRST if sort == "comp" else _SORT_FIT_FIRST
    placeholders = ",".join("?" for _ in _UNDECIDED)
    rows = conn.execute(
        f"SELECT {_COLS} FROM jobs WHERE source_state='active' "
        f"AND review_state IN ({placeholders}) AND alignment_score IS NOT NULL "
        f"AND alignment_score >= ? ORDER BY {order}, first_seen DESC, id",
        (*_UNDECIDED, floor),
    ).fetchall()
    kept_row, rejected, total = cluster_overrides(conn, rows)
    collapsed = collapse_clusters(
        rows, rejected=rejected | frozenset(kept_row), total_members=total
    )
    seen: set[str] = set()
    out: list[dict] = []
    for r in collapsed:
        key = f"{(r['company'] or '').strip().lower()}|{(r['title'] or '').strip().lower()}"
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def stage_list_cache(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Every stage page's rows, computed once.

    `/pipeline` used to build the shortlist, applied and closed lists twice --
    once for the board, once for the header counts -- eight stage-list
    computations where five do. Passing this to both is not just cheaper: it
    makes the counts literally the same rows the page renders, which is the
    invariant docs/how-it-works/web.md states ("header counts are the page lengths") and which
    two independent computations could only ever approximate.
    """
    return {stage: render(conn) for stage, render in STAGE_LISTS.items()}


def counts(conn: sqlite3.Connection, lists: dict[str, list[dict]] | None = None) -> dict[str, int]:
    """Header counts: the length of each stage page, from the page functions
    themselves. `total` is the raw active-row count the All page shows.

    `lists` reuses an already-computed `stage_list_cache`; omitting it computes
    them, so every existing caller is unchanged."""
    rows = lists if lists is not None else stage_list_cache(conn)
    out = {stage: len(rows[stage]) for stage in STAGE_LISTS}
    out["total"] = conn.execute(
        "SELECT COUNT(*) n FROM jobs WHERE source_state='active'"
    ).fetchone()["n"]
    return out


def empty_reason(conn: sqlite3.Connection, view: str) -> str:
    """Why a stage page rendered zero rows although matching rows exist, e.g.
    "2 queued — both in clusters you already decided". "" when there is
    nothing to explain."""
    render = STAGE_LISTS.get(view)
    if render is None:
        return ""
    raw = conn.execute(f"SELECT {_COLS} FROM jobs WHERE {st.STAGE_WHERE[view]}").fetchall()
    if not raw or render(conn):
        return ""
    n = len(raw)
    word = "both" if n == 2 else "all"
    if view in st.KEPT_STAGES:
        return f"{n} {view}: {word} shown under a copy of the same role that is further along"
    kept_row, rejected, _total = cluster_overrides(conn, raw)
    decided = rejected | frozenset(kept_row)
    if all(r["cluster_id"] in decided for r in raw):
        return f"{n} {view} — {word} in clusters you already decided"
    return f"{n} {view} hidden by cluster filtering"


def pipeline_board(
    conn: sqlite3.Connection, lists: dict[str, list[dict]] | None = None
) -> dict[str, list[dict]]:
    """Every kept job in exactly one lane: Shortlist by tailoring build, Applied
    by hiring stage, then Closed. Lanes keep their stage page's order.

    `lists` reuses an already-computed `stage_list_cache` so a page showing both
    the board and the header counts computes each list once."""
    rows = lists if lists is not None else {}
    board: dict[str, list[dict]] = {lane: [] for lane in _BOARD_BUILD_LANES}
    for j in rows.get(st.SHORTLIST) or shortlist_jobs(conn):
        lane = j["build_state"] if j["build_state"] in _BOARD_BUILD_LANES else "none"
        board[lane].append(j)
    for state in st.IN_PROCESS_STATES:
        board[state] = []
    for j in rows.get(st.APPLIED) or applied_jobs(conn):
        board[j["application_state"]].append(j)
    board["closed"] = rows.get(st.CLOSED) or closed_jobs(conn)
    return board

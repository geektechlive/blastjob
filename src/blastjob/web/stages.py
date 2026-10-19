"""The pipeline's exclusive stages: the one place that decides where a job is.

Like a recruiter's board, every job sits in at most one stage:

    surfaced -> queued -> shortlist -> applied -> closed

`stage_of` is the Python form and the `*_WHERE` fragments are the SQL form of
the same rule; every page, header count, board lane and chip goes through one
of them. Archived and plain 'new' jobs are in no stage (the All page shows
them). Cross-source clusters are resolved on top of this in `stage_lists`.

Pure: no database access and no imports from the rest of the web layer, so
`service` can use it without a cycle.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from blastjob.core.stage_sql import (
    _APP_STATE as _APP_STATE,
)

# The state lists and SQL fragments live in core (`core/stage_sql.py`) so core
# can use them without importing the web layer; re-exported here unchanged.
from blastjob.core.stage_sql import (
    APPLIED_WHERE as APPLIED_WHERE,
)
from blastjob.core.stage_sql import (
    CLOSED_STATES as CLOSED_STATES,
)
from blastjob.core.stage_sql import (
    CLOSED_WHERE as CLOSED_WHERE,
)
from blastjob.core.stage_sql import (
    IN_PROCESS_STATES as IN_PROCESS_STATES,
)
from blastjob.core.stage_sql import (
    LIVE_STATES as LIVE_STATES,
)
from blastjob.core.stage_sql import (
    QUEUED_WHERE as QUEUED_WHERE,
)
from blastjob.core.stage_sql import (
    SHORTLIST_STATES as SHORTLIST_STATES,
)
from blastjob.core.stage_sql import (
    SHORTLIST_WHERE as SHORTLIST_WHERE,
)
from blastjob.core.stage_sql import (
    SURFACED_WHERE as SURFACED_WHERE,
)
from blastjob.core.stage_sql import (
    WITHDRAWN as WITHDRAWN,
)
from blastjob.core.stage_sql import (
    _sql_list as _sql_list,
)
from blastjob.matching.gate import BAND

SURFACED = "surfaced"
QUEUED = "queued"
SHORTLIST = "shortlist"
APPLIED = "applied"
CLOSED = "closed"
STAGES: tuple[str, ...] = (SURFACED, QUEUED, SHORTLIST, APPLIED, CLOSED)
KEPT_STAGES: tuple[str, ...] = (SHORTLIST, APPLIED, CLOSED)

STAGE_WHERE: dict[str, str] = {
    SURFACED: SURFACED_WHERE,
    QUEUED: QUEUED_WHERE,
    SHORTLIST: SHORTLIST_WHERE,
    APPLIED: APPLIED_WHERE,
    CLOSED: CLOSED_WHERE,
}

# Page titles, in the nav's words.
LABELS: dict[str, str] = {
    SURFACED: "Surfaced",
    QUEUED: "Queue",
    SHORTLIST: "Shortlist",
    APPLIED: "Applied",
    CLOSED: "Closed",
}
ROUTES: dict[str, str] = {
    SURFACED: "/",
    QUEUED: "/queue",
    SHORTLIST: "/shortlist",
    APPLIED: "/applied",
    CLOSED: "/closed",
}


def stage_of(job: Mapping[str, Any] | sqlite3.Row) -> str | None:
    """The stage one job row belongs to, ignoring its cluster; None if none."""
    review = _get(job, "review_state")
    app_state = _get(job, "application_state") or ""
    if review == "kept":
        if app_state in SHORTLIST_STATES:
            return SHORTLIST
        if app_state in IN_PROCESS_STATES:
            return APPLIED
        if app_state in CLOSED_STATES:
            return CLOSED
        return None
    source_active = (_get(job, "source_state") or "active") == "active"
    if review == SURFACED and source_active:
        return SURFACED
    if review == QUEUED and source_active:
        return QUEUED
    return None


def _get(job: Mapping[str, Any] | sqlite3.Row, key: str) -> Any:
    # Works for dicts and sqlite3.Row alike; callers pass their own column lists.
    return job[key] if key in job.keys() else None


def next_stage(app_state: str) -> str:
    """The next hiring-process state (applied -> screen -> interview -> offer),
    or "" when there is none to advance to."""
    try:
        i = IN_PROCESS_STATES.index(app_state)
    except ValueError:
        return ""
    return IN_PROCESS_STATES[i + 1] if i + 1 < len(IN_PROCESS_STATES) else ""


def unkeep_target(*, source_state: str | None, alignment_score: int | None, threshold: int) -> str:
    """Where a removed shortlist job goes: where the gate would have put it by
    its stored score. `matching.gate.decide` can't be called here because the
    geo verdict it needs is not stored, so this reuses its BAND arithmetic."""
    if (source_state or "active") != "active":
        return "new"
    if alignment_score is None:
        return QUEUED
    if alignment_score >= threshold:
        return SURFACED
    if alignment_score >= threshold - BAND:
        return QUEUED
    return "new"

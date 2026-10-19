"""The stage membership rules as plain constants and SQL fragments.

`web/stages.py` is the one place that decides where a job is; these are its
building blocks, kept in core so core modules (the packet filer and renamer)
use the same rule without importing the web layer. `web/stages.py` re-exports
every name, so existing imports keep working.
"""

from __future__ import annotations

# application_state values per kept stage.
SHORTLIST_STATES: tuple[str, ...] = ("", "drafted")
# In a hiring process, furthest last (advance walks this order).
IN_PROCESS_STATES: tuple[str, ...] = ("applied", "screen", "interview", "offer")
# posting_closed / filled come from the posting-closure lifecycle (core/closure.py).
CLOSED_STATES: tuple[str, ...] = ("rejected", "ghosted", "withdrawn", "posting_closed", "filled")
# Stages where a follow-up still makes sense (offer/closed don't need nudging).
LIVE_STATES: tuple[str, ...] = ("applied", "screen", "interview")
WITHDRAWN = "withdrawn"


def _sql_list(values: tuple[str, ...]) -> str:
    return ",".join(f"'{v}'" for v in values)


_APP_STATE = "COALESCE(application_state,'')"
SURFACED_WHERE = "review_state='surfaced' AND source_state='active'"
QUEUED_WHERE = "review_state='queued' AND source_state='active'"
# Kept stages carry no source_state filter: a kept job whose posting went stale
# is still the user's decision and stays on their board.
SHORTLIST_WHERE = f"review_state='kept' AND {_APP_STATE} IN ({_sql_list(SHORTLIST_STATES)})"
APPLIED_WHERE = f"review_state='kept' AND {_APP_STATE} IN ({_sql_list(IN_PROCESS_STATES)})"
CLOSED_WHERE = f"review_state='kept' AND {_APP_STATE} IN ({_sql_list(CLOSED_STATES)})"

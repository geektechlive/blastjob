"""The daily run's call-budget wording (docs/how-it-works/daily.md): the stop note and the
"model calls yesterday" digest line. Split out of `daily.py`, which owns the run's frame."""

from __future__ import annotations

import datetime as dt
import sqlite3

from blastjob.llm import gateway, ledger_stats
from blastjob.models.config import LimitsConfig

STOPPED_BUDGET = "budget"
NOT_RUN_LABEL = "daily call limit"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def budget_note(conn: sqlite3.Connection, cap: int, skipped: list[str], *, score: bool) -> str:
    """The digest line for a run the call budget cut short: what is left and which steps did
    not run (never shown as done)."""
    from blastjob.matching.pipeline import count_unscored

    left = count_unscored(conn) if score else 0
    base = f"Stopped at the daily limit of {_plural(cap, 'scheduled model call')}"
    if left:
        # An upper bound: rows the geo check or title prefilter will turn away are unscored too.
        note = f"{base}; up to {_plural(left, 'job')} left for the next run"
    else:
        note = f"{base}; the remaining steps run next time"
    if skipped:
        note += f". Not run ({NOT_RUN_LABEL}): {', '.join(dict.fromkeys(skipped))}"
    return note


def yesterday_calls_line(conn: sqlite3.Connection, today: dt.date | None = None) -> str:
    """ "Model calls yesterday: N scheduled, M clicked (est. $X or unknown)", as a footer line.
    Part of a digest that goes out anyway: it can never make a quiet day send."""
    day = (today or dt.date.today()) - dt.timedelta(days=1)
    line = ledger_stats.calls_line(conn, day)
    return f"\n_{line}_" if line else ""


def clicked_cut_off(state: gateway.DeadlineState, limits: LimitsConfig) -> str:
    """The log line for a clicked full pass (Find + Score, Re-score) that a refused call stopped:
    it names the limit that was actually hit, never the time limit for a budget refusal."""
    if state.reason == STOPPED_BUDGET:
        cap = limits.clicked_calls_per_day
        return (
            f"Stopped at the daily limit of {_plural(cap, 'clicked model call')}; "
            "unscored jobs wait for the next run."
        )
    minutes = limits.daily_deadline_minutes
    return f"Stopped at the {minutes}-minute limit; unscored jobs wait for the next run."

"""The digest's weekly pace line: last week's applications, outreach asks and reply events.

Fixed periods, plain counts, no targets and no rates (docs/how-it-works/daily.md). The period
is the previous full ISO week (Monday to Sunday). The line is due on the first run on or after
`notify.pace_weekday` in the following week; its stamp is the reported ISO year-week, held in
`digest_stamps`, so a delayed run still reports the right week and no two windows overlap.
The stamp is written by the caller only after a digest carrying the line was sent.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass

from blastjob.core.channel_counts import REPLIED_EVENT_KINDS

PACE_STAMP = "pace"
WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEK = 7


@dataclass(frozen=True)
class WeekCounts:
    applied: int
    asks: int
    replies: int


@dataclass(frozen=True)
class PaceReport:
    """`line` is the digest text, `label` the ISO year-week it reports (what gets stamped),
    and `on_day` whether today IS the configured weekday (only then does the line count as
    something to say on an otherwise quiet day)."""

    line: str
    label: str
    on_day: bool


def previous_iso_week(today: dt.date) -> tuple[dt.date, dt.date, str]:
    """(Monday, Sunday, 'YYYY-Www') of the full ISO week before the one `today` is in."""
    start = today - dt.timedelta(days=today.weekday() + _WEEK)
    iso = start.isocalendar()
    return start, start + dt.timedelta(days=_WEEK - 1), f"{iso.year}-W{iso.week:02d}"


def week_counts(conn: sqlite3.Connection, start: dt.date, end: dt.date) -> WeekCounts:
    """Applied events, asks logged (`asked_on`) and reply-kind events from `start` to `end`
    inclusive. Events are counted as events, not as distinct applications."""
    lo, hi = start.isoformat(), end.isoformat()
    kinds = ",".join("?" for _ in REPLIED_EVENT_KINDS)
    applied = conn.execute(
        "SELECT COUNT(*) FROM application_events "
        "WHERE kind='applied' AND substr(at, 1, 10) BETWEEN ? AND ?",
        (lo, hi),
    ).fetchone()[0]
    replies = conn.execute(
        f"SELECT COUNT(*) FROM application_events "
        f"WHERE kind IN ({kinds}) AND substr(at, 1, 10) BETWEEN ? AND ?",
        (*REPLIED_EVENT_KINDS, lo, hi),
    ).fetchone()[0]
    asks = conn.execute(
        "SELECT COUNT(*) FROM outreach_asks WHERE asked_on BETWEEN ? AND ?", (lo, hi)
    ).fetchone()[0]
    return WeekCounts(applied=int(applied), asks=int(asks), replies=int(replies))


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _span(start: dt.date, end: dt.date) -> str:
    return f"{start.strftime('%b')} {start.day} to {end.strftime('%b')} {end.day}"


def format_line(start: dt.date, end: dt.date, counts: WeekCounts) -> str:
    return (
        f"**Last week** ({_span(start, end)}): "
        f"{_count(counts.applied, 'application', 'applications')}, "
        f"{_count(counts.asks, 'outreach ask', 'outreach asks')}, "
        f"{_count(counts.replies, 'reply event', 'reply events')}"
    )


def stamped_label(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM digest_stamps WHERE name=?", (PACE_STAMP,)).fetchone()
    return str(row[0]) if row else ""


def pace_report(conn: sqlite3.Connection, weekday: str, today: dt.date) -> PaceReport | None:
    """The pace line due today, or None: switched off, before the weekday this week, or last
    week already reported."""
    if weekday not in WEEKDAYS:
        return None
    index = WEEKDAYS.index(weekday)
    if today.weekday() < index:
        return None
    start, end, label = previous_iso_week(today)
    if stamped_label(conn) >= label:
        return None
    return PaceReport(
        line=format_line(start, end, week_counts(conn, start, end)),
        label=label,
        on_day=today.weekday() == index,
    )


def stamp(conn: sqlite3.Connection, label: str) -> None:
    """Record that the digest reported `label`. Call only after a successful send."""
    conn.execute(
        "INSERT INTO digest_stamps (name, value) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
        (PACE_STAMP, label),
    )
    conn.commit()

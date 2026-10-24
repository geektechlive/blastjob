"""Applications by channel, counts only (#81, docs/how-it-works/web.md#funnel).

One row per application channel (`APPLICATION_CHANNELS` order) plus "unknown"
for jobs whose channel was never recorded. Counts only, by design: with a
handful of applications a rate is noise, and outcomes deliberately show none.
Do not add percentages, rates or a ranking here.

A job counts as an application when `applied_at` is set. The columns are
cumulative, so a job that got further also counts in the earlier columns:

- replied or screened: current state screen, interview or offer, or an
  application event of kind recruiter_reply, screen, interview or offer.
- interviewed: current state interview or offer, or an interview or offer event.

Events are read as well as the current state because a job that was rejected
after an interview no longer sits in `interview`. A bare rejection or ghosting
is not a reply: an automated rejection says nothing about the channel.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from blastjob.models.tracking import APPLICATION_CHANNELS

UNKNOWN_KEY = "unknown"
UNKNOWN_LABEL = "Unknown"

REPLIED_STATES: tuple[str, ...] = ("screen", "interview", "offer")
INTERVIEWED_STATES: tuple[str, ...] = ("interview", "offer")
REPLIED_EVENT_KINDS: tuple[str, ...] = ("recruiter_reply", "screen", "interview", "offer")
INTERVIEWED_EVENT_KINDS: tuple[str, ...] = ("interview", "offer")


@dataclass(frozen=True)
class ChannelRow:
    key: str
    label: str
    applications: int = 0
    replied: int = 0
    interviewed: int = 0


def _marks(values: tuple[str, ...]) -> str:
    return ",".join("?" for _ in values)


def _reached(states: tuple[str, ...], kinds: tuple[str, ...]) -> str:
    """SQL boolean: the job is in one of `states` or has an event of one of `kinds`."""
    return (
        f"(COALESCE(j.application_state,'') IN ({_marks(states)}) OR EXISTS ("
        f"SELECT 1 FROM application_events e WHERE e.job_id=j.id AND e.kind IN ({_marks(kinds)})))"
    )


def channel_counts(conn: sqlite3.Connection) -> list[ChannelRow]:
    """Counts per channel, fixed order, unknown last. Pure read."""
    sql = (
        "SELECT j.app_channel AS channel, COUNT(*) AS applications, "
        f"SUM({_reached(REPLIED_STATES, REPLIED_EVENT_KINDS)}) AS replied, "
        f"SUM({_reached(INTERVIEWED_STATES, INTERVIEWED_EVENT_KINDS)}) AS interviewed "
        "FROM jobs j WHERE COALESCE(j.applied_at,'') != '' GROUP BY j.app_channel"
    )
    params = (*REPLIED_STATES, *REPLIED_EVENT_KINDS, *INTERVIEWED_STATES, *INTERVIEWED_EVENT_KINDS)
    found = {r["channel"]: r for r in conn.execute(sql, params)}
    labelled = [*APPLICATION_CHANNELS.items(), (UNKNOWN_KEY, UNKNOWN_LABEL)]
    rows: list[ChannelRow] = []
    for key, label in labelled:
        r = found.get(None if key == UNKNOWN_KEY else key)
        if r is None:
            rows.append(ChannelRow(key, label))
        else:
            rows.append(ChannelRow(key, label, r["applications"], r["replied"], r["interviewed"]))
    return rows

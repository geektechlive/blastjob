"""Read-side views of the call ledger, for the `/runs` page and the digest line
(docs/how-it-works/llm.md). Spend counts only calls whose cost is known; the calls whose cost is
not are counted separately and shown as unknown, never as $0."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from blastjob.llm.ledger import ORIGINS, day_bounds


@dataclass(frozen=True)
class CallGroup:
    caller: str
    origin: str
    calls: int  # chargeable calls (everything except refused_budget; abandoned counts)
    known_cost: float
    unknown_cost_calls: int
    refused: int


@dataclass(frozen=True)
class OriginTotal:
    origin: str
    calls: int
    known_cost: float
    unknown_cost_calls: int
    refused: int


@dataclass(frozen=True)
class AbandonedCall:
    id: int
    started_at: str
    caller: str
    origin: str


def by_caller(conn: sqlite3.Connection, day: date) -> list[CallGroup]:
    start, end = day_bounds(day)
    rows = conn.execute(
        "SELECT caller, origin, "
        "SUM(outcome != 'refused_budget') AS calls, "
        "COALESCE(SUM(CASE WHEN cost_known=1 THEN cost_usd END), 0) AS known_cost, "
        "SUM(outcome != 'refused_budget' AND cost_known=0 AND outcome != 'started') AS unknown, "
        "SUM(outcome = 'refused_budget') AS refused "
        "FROM model_calls WHERE started_at >= ? AND started_at < ? "
        "GROUP BY caller, origin ORDER BY calls DESC, caller",
        (start, end),
    ).fetchall()
    return [
        CallGroup(
            r["caller"],
            r["origin"],
            r["calls"] or 0,
            r["known_cost"],
            r["unknown"] or 0,
            r["refused"] or 0,
        )
        for r in rows
    ]


def origin_totals(conn: sqlite3.Connection, day: date) -> list[OriginTotal]:
    """One entry per origin (zeros included), so the page and the digest never miss one."""
    groups = by_caller(conn, day)
    out = []
    for origin in ORIGINS:
        mine = [g for g in groups if g.origin == origin]
        out.append(
            OriginTotal(
                origin,
                sum(g.calls for g in mine),
                sum(g.known_cost for g in mine),
                sum(g.unknown_cost_calls for g in mine),
                sum(g.refused for g in mine),
            )
        )
    return out


def abandoned_calls(conn: sqlite3.Connection, day: date) -> list[AbandonedCall]:
    start, end = day_bounds(day)
    rows = conn.execute(
        "SELECT id, started_at, caller, origin FROM model_calls WHERE outcome='abandoned' "
        "AND started_at >= ? AND started_at < ? ORDER BY started_at DESC",
        (start, end),
    ).fetchall()
    return [AbandonedCall(r["id"], r["started_at"], r["caller"], r["origin"]) for r in rows]


def describe_spend(total: OriginTotal) -> str:
    """ "$0.1234", "$0.1234 known + 2 calls of unknown cost", "unknown" when no call's cost is
    known, or "no calls"."""
    if not total.calls:
        return "no calls"
    if total.unknown_cost_calls and not total.known_cost:
        return "unknown"
    if total.unknown_cost_calls:
        n = total.unknown_cost_calls
        return f"${total.known_cost:.4f} known + {n} call{'s' if n != 1 else ''} of unknown cost"
    return f"${total.known_cost:.4f}"


def calls_line(conn: sqlite3.Connection, day: date) -> str:
    """ "Model calls yesterday: N scheduled, M clicked (est. $X or unknown)", or "" when there
    were none. Appended only to a digest that is sent anyway; it never makes a quiet day send."""
    totals = {t.origin: t for t in origin_totals(conn, day)}
    sched, clicked = totals["scheduled"], totals["clicked"]
    if not (sched.calls or clicked.calls):
        return ""
    unknown = sched.unknown_cost_calls + clicked.unknown_cost_calls
    cost = sched.known_cost + clicked.known_cost
    if unknown and not cost:
        spend = "unknown"
    elif unknown:
        spend = f"est. ${cost:.2f} known, {unknown} unknown"
    else:
        spend = f"est. ${cost:.2f}"
    return f"Model calls yesterday: {sched.calls} scheduled, {clicked.calls} clicked ({spend})"

"""What a same-role recluster would do to the cockpit, in words.

For `blastjob-daily --recluster-report` / `--recluster`: each proposed merge,
its members, and the stage the role is visible in before and after. The stage
comes from the exclusive-stage cluster rule (`stage_lists`), so a merge that
joins a queued posting to one the user archived shows the role leaving the Queue.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from blastjob.core.recluster import Merge
from blastjob.web import stages as st
from blastjob.web.service import _COLS
from blastjob.web.stage_lists import _representative

NOWHERE = "hidden"


def _visible(members: list[sqlite3.Row]) -> str:
    if len(members) == 1 and members[0]["cluster_id"] is None:
        stage = st.stage_of(members[0])
    else:
        rep = _representative(members)
        stage = st.stage_of(rep) if rep is not None else None
    return st.LABELS[stage] if stage else NOWHERE


def describe(conn: sqlite3.Connection, plan: list[Merge]) -> list[dict[str, Any]]:
    """One dict per merge: members, stages before (one per current cluster)
    and after, and whether the visible stage changes."""
    out: list[dict[str, Any]] = []
    for merge in plan:
        rows = conn.execute(
            f"SELECT {_COLS} FROM jobs WHERE id IN ({','.join('?' for _ in merge.job_ids)}) "
            "ORDER BY id",
            merge.job_ids,
        ).fetchall()
        groups: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            key = f"c{r['cluster_id']}" if r["cluster_id"] is not None else f"j{r['id']}"
            groups.setdefault(key, []).append(r)
        shown = {_visible(g) for g in groups.values()} - {NOWHERE}
        before = sorted(shown) or [NOWHERE]
        after = _visible_after(rows)
        out.append(
            {
                "merge": merge,
                "rows": rows,
                "before": before,
                "after": after,
                "changes": before != [after],
            }
        )
    return out


def _visible_after(rows: list[sqlite3.Row]) -> str:
    rep = _representative(rows)
    stage = st.stage_of(rep) if rep is not None else None
    return st.LABELS[stage] if stage else NOWHERE


def format_report(items: list[dict[str, Any]], *, applied: int | None = None) -> str:
    if not items:
        return "no same-role merges proposed"
    lines: list[str] = []
    for item in items:
        m: Merge = item["merge"]
        flag = "  STAGE CHANGES" if item["changes"] else ""
        lines.append(
            f"merge {m.company} · {m.title}: jobs {', '.join(map(str, m.job_ids))} | "
            f"visible before: {', '.join(item['before'])} -> after: {item['after']}{flag}"
        )
        for r in item["rows"]:
            where = r["geo_city"] or r["geo_location_raw"] or "-"
            lines.append(
                f"    {r['id']} | {r['source']} | {r['company']} | {r['title']} | {where} | "
                f"{(r['first_seen'] or '')[:10]} | {r['review_state']} "
                f"{r['application_state'] or ''} {r['source_state']} | cluster {r['cluster_id']}"
            )
    changed = sum(1 for i in items if i["changes"])
    lines.append(f"{len(items)} merges proposed, {changed} change a visible stage")
    if applied is not None:
        lines.append(f"applied {applied}")
    return "\n".join(lines)

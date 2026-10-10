"""Backfill and daily application of the same-role auto rule.

`plan_recluster` proposes cluster merges from `role_clusters.auto_same_role`;
`apply_recluster` performs them. Only ever merges: an existing cluster is
never split here, and a pair the user marked 'different' is never joined.
The daily discovery run applies it to groups holding a newly inserted
posting; `blastjob-daily --recluster[-report]` runs it over everything.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from blastjob.core import role_clusters as rc
from blastjob.core.agencies import is_agency
from blastjob.core.company import company_key
from blastjob.core.titles import normalize_role_title
from blastjob.models.config import DiscoveryConfig


@dataclass(frozen=True)
class Merge:
    """One proposed merge: every job that ends up in one cluster."""

    job_ids: tuple[int, ...]
    cluster_ids: tuple[int, ...]
    company: str
    title: str


def _node(row: sqlite3.Row) -> str:
    return f"c{row['cluster_id']}" if row["cluster_id"] is not None else f"j{row['id']}"


def _groups(rows: list[sqlite3.Row]) -> dict[tuple[str, str], list[sqlite3.Row]]:
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in rows:
        if is_agency(r["company"] or ""):
            continue
        key = (company_key(r["company"] or ""), normalize_role_title(r["title"]))
        if key[0] and key[1]:
            groups.setdefault(key, []).append(r)
    return {k: v for k, v in groups.items() if len(v) > 1}


class _Union:
    """Union-find over cluster nodes that refuses a join across a conflict."""

    def __init__(self, members: dict[str, list[sqlite3.Row]], decided: dict[rc.Pair, str]):
        self.parent = {n: n for n in members}
        self.members = {n: list(ms) for n, ms in members.items()}
        self.decided = decided

    def find(self, n: str) -> str:
        while self.parent[n] != n:
            self.parent[n] = self.parent[self.parent[n]]
            n = self.parent[n]
        return n

    def join(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb or rc.conflict(self.members[ra], self.members[rb], self.decided, auto=True):
            return
        self.parent[rb] = ra
        self.members[ra].extend(self.members.pop(rb))


def plan_recluster(
    conn: sqlite3.Connection,
    radius_miles: float,
    geo: DiscoveryConfig,
    only_ids: set[int] | None = None,
) -> list[Merge]:
    """Merges the auto rule proposes. `only_ids` limits it to groups holding
    one of those jobs (the daily run passes the postings it just inserted)."""
    rows = conn.execute(f"SELECT {rc.FACT_COLS} FROM jobs ORDER BY id").fetchall()
    members: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        members.setdefault(_node(r), []).append(r)
    union = _Union(members, rc.decisions(conn))
    labels: dict[str, tuple[str, str]] = {}
    for group in _groups(rows).values():
        if only_ids is not None and not any(r["id"] in only_ids for r in group):
            continue
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if rc.auto_same_role(a, b, radius_miles, geo):
                    union.join(_node(a), _node(b))
                    labels.setdefault(_node(a), (a["company"], a["title"]))
    return _merges(union, members, labels)


def _merges(
    union: _Union, original: dict[str, list[sqlite3.Row]], labels: dict[str, tuple[str, str]]
) -> list[Merge]:
    by_root: dict[str, list[str]] = {}
    for node in original:
        by_root.setdefault(union.find(node), []).append(node)
    out: list[Merge] = []
    for nodes in by_root.values():
        if len(nodes) < 2:
            continue
        rows = sorted((r for n in nodes for r in original[n]), key=lambda r: r["id"])
        company, title = next(
            (labels[n] for n in nodes if n in labels), (rows[0]["company"], rows[0]["title"])
        )
        out.append(
            Merge(
                job_ids=tuple(int(r["id"]) for r in rows),
                cluster_ids=tuple(sorted({int(n[1:]) for n in nodes if n.startswith("c")})),
                company=company or "",
                title=title or "",
            )
        )
    return sorted(out, key=lambda m: m.job_ids)


def apply_recluster(conn: sqlite3.Connection, plan: list[Merge]) -> int:
    """Perform `plan`; each join is re-checked for conflicts. Returns how many
    merges changed the database. Commits."""
    changed = 0
    for merge in plan:
        anchor = merge.job_ids[0]
        before = _cluster_count(conn, merge.job_ids)
        for other in merge.job_ids[1:]:
            rc.merge_jobs(conn, anchor, other)
        if _cluster_count(conn, merge.job_ids) < before:
            changed += 1
    conn.commit()
    return changed


def _cluster_count(conn: sqlite3.Connection, ids: tuple[int, ...]) -> int:
    rows = conn.execute(
        f"SELECT DISTINCT COALESCE(cluster_id, -id) c FROM jobs "
        f"WHERE id IN ({','.join('?' for _ in ids)})",
        ids,
    ).fetchall()
    return len(rows)

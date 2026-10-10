"""Audit: two applications to what looks like one role (read-only).

`blastjob-daily --duplicate-applications` lists every pair of kept rows that
both hold an application (`role_clusters.has_application`) and share a
`company_key` + `normalize_role_title`, clustered or not. It is a prompt to
look, never a merge: linking two postings stays the user's call (the detail
page's "same role" button, which reconciles the applications into one).
"""

from __future__ import annotations

import sqlite3
from itertools import combinations

from blastjob.core.company import company_key
from blastjob.core.role_clusters import has_application
from blastjob.core.titles import normalize_role_title

_COLS = (
    "id, source, company, title, review_state, cluster_id, "
    "COALESCE(application_state,'') AS application_state, COALESCE(applied_at,'') AS applied_at"
)


def duplicate_applications(
    conn: sqlite3.Connection,
) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
    rows = conn.execute(f"SELECT {_COLS} FROM jobs WHERE review_state='kept' ORDER BY id")
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in rows.fetchall():
        if not has_application(r):
            continue
        key = (company_key(r["company"] or ""), normalize_role_title(r["title"]))
        if key[0] and key[1]:
            groups.setdefault(key, []).append(r)
    return [pair for members in groups.values() for pair in combinations(members, 2)]


def _describe(r: sqlite3.Row) -> str:
    cluster = f"cluster {r['cluster_id']}" if r["cluster_id"] is not None else "no cluster"
    return (
        f"#{r['id']} {r['source']} | {r['company']} | {r['title']} | "
        f"{r['application_state'] or '-'} {r['applied_at'] or ''} | {cluster}"
    ).rstrip()


def format_pairs(pairs: list[tuple[sqlite3.Row, sqlite3.Row]]) -> str:
    if not pairs:
        return "no duplicate applications"
    lines = [f"{_describe(a)}\n    <-> {_describe(b)}" for a, b in pairs]
    n = len(pairs)
    lines.append(f"{n} pair{'s' if n != 1 else ''} of applications that may be one role")
    return "\n".join(lines)

"""One "apply here" link per role: the best URL across a cluster's members.

A role seen on LinkedIn, Adzuna and the employer's own Workday board should be
applied to on Workday. Preference, best first:

1. an authoritative member's own posting (`closure_links.is_authoritative`),
   labelled with its board, e.g. "employer site (Workday)";
2. an employer link closure found on an authoritative board;
3. an employer careers page link (`page:<host>`) or the user's manual link;
4. a non-agency member's posting: LinkedIn > Indeed > Adzuna > anything else;
5. an agency's posting, last.

Every URL passes `safe_external_url`; one that fails is skipped. Within a
tier an active posting beats a stale one, then the lowest id.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from blastjob.core.agencies import is_agency
from blastjob.core.closure_links import Link, is_authoritative
from blastjob.core.page_links import apply_url
from blastjob.web.urls import safe_external_url

# The user's pasted employer posting URL (web/routes_employer_link.py). DISPLAY-ONLY:
# the preferred apply link, never closure evidence (core/closure skips it).
MANUAL_LINK = "manual"
PAGE_LINK_PREFIX = "page:"
EMPLOYER_SITE = "employer site"

_BOARD_NAMES = {
    "workday:": "Workday",
    "greenhouse:": "Greenhouse",
    "lever:": "Lever",
    "ashby:": "Ashby",
    "dayforce:": "Dayforce",
}
# Sources that are neither a board nor an aggregator and get a plain label (limited trust).
_SOURCE_LABELS = {"category-web": "web search result"}
_AGGREGATOR_ORDER = ("linkedin", "indeed", "adzuna")
_AGGREGATOR_LABELS = {"linkedin": "LinkedIn", "indeed": "Indeed", "adzuna": "Adzuna"}
# Tiers: 0 own board posting, 1 board link, 2 page/manual link,
# 3..5 linkedin/indeed/adzuna, 6 other source, 7 agency.
_TIER_OTHER = 3 + len(_AGGREGATOR_ORDER)
_TIER_AGENCY = _TIER_OTHER + 1

APPLY_COLS = (
    "id, source, company, url, employer_link_source, employer_link_external_id, "
    "employer_link_url, source_state, cluster_id"
)


@dataclass(frozen=True)
class ApplyLink:
    url: str
    label: str


def _get(row: Any, key: str) -> str:
    try:
        return str(row[key] or "")
    except (KeyError, IndexError):
        return ""


def board_name(source: str) -> str:
    """ "Workday" / "Greenhouse" / ... for an ATS source or link source, else ""."""
    return next((v for k, v in _BOARD_NAMES.items() if source.startswith(k)), "")


def _board_label(source: str) -> str:
    name = board_name(source)
    return f"{EMPLOYER_SITE} ({name})" if name else EMPLOYER_SITE


def _candidates(row: Any) -> list[tuple[int, str, str]]:
    """(tier, url, label) for everything one member offers."""
    out: list[tuple[int, str, str]] = []
    source = _get(row, "source")
    link_source = _get(row, "employer_link_source")
    link_url = _get(row, "employer_link_url")
    if link_source and is_authoritative(link_source):
        out.append((1, link_url, _board_label(link_source)))
    elif link_source == MANUAL_LINK:
        out.append((2, link_url, EMPLOYER_SITE))
    elif link_source.startswith(PAGE_LINK_PREFIX):
        # Limited trust (#23) shows the ATS tenant root, never the page.
        link = Link(link_source, _get(row, "employer_link_external_id"), link_url)
        out.append((2, apply_url(link, _get(row, "company")), EMPLOYER_SITE))
    own = _get(row, "url")
    if is_authoritative(source):
        out.append((0, own, _board_label(source)))
    elif is_agency(_get(row, "company")):
        out.append((_TIER_AGENCY, own, f"{_get(row, 'company')} (agency)"))
    elif source in _AGGREGATOR_ORDER:
        out.append((3 + _AGGREGATOR_ORDER.index(source), own, _AGGREGATOR_LABELS[source]))
    else:
        out.append((_TIER_OTHER, own, _SOURCE_LABELS.get(source, source or "posting")))
    return out


def best_apply_link(members: Iterable[Any]) -> ApplyLink | None:
    """The best link across `members` (rows or mappings with APPLY_COLS), or None."""
    best: tuple[tuple[int, int, int], ApplyLink] | None = None
    for row in members:
        stale = int((_get(row, "source_state") or "active") != "active")
        rid = int(_get(row, "id") or 0)
        for tier, url, label in _candidates(row):
            safe = safe_external_url(url)
            if not safe:
                continue
            rank = (tier, stale, rid)
            if best is None or rank < best[0]:
                best = (rank, ApplyLink(safe, label))
    return best[1] if best else None


def apply_links_for(conn: sqlite3.Connection, rows: Iterable[Any]) -> dict[int, ApplyLink]:
    """`{job_id: ApplyLink}` for `rows`, each from its whole cluster (a row with
    no cluster uses itself). One query loads every cluster member."""
    rows = list(rows)
    cids = sorted({int(r["cluster_id"]) for r in rows if _get(r, "cluster_id")})
    members: dict[int, list[Any]] = {}
    if cids:
        marks = ",".join("?" for _ in cids)
        for m in conn.execute(
            f"SELECT {APPLY_COLS} FROM jobs WHERE cluster_id IN ({marks}) ORDER BY id", cids
        ).fetchall():
            members.setdefault(int(m["cluster_id"]), []).append(m)
    out: dict[int, ApplyLink] = {}
    for r in rows:
        cid = _get(r, "cluster_id")
        link = best_apply_link(members.get(int(cid), [r]) if cid else [r])
        if link is not None:
            out[int(r["id"])] = link
    return out

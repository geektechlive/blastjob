"""Repair LinkedIn rows stored from the whole-card alert layout.

Before `linkedin_cards`, a whole-card anchor's full text ("<Title> <Company> ·
<City, ST> $pay Easy Apply") became the title and the company stayed empty.
`blastjob-daily --repair-linkedin-titles-report` / `--repair-linkedin-titles`
fixes those rows (`source='linkedin' AND company=''`):

- a split on structure is applied: the text ends with a cluster sibling's
  company (BASIS_SIBLING), else with another DB row's company (BASIS_DB);
- a vocabulary-only split (BASIS_VOCAB) is listed as "needs confirmation" and
  applied only for the ids passed with `--accept`;
- nothing is guessed when neither finds a split (BASIS_NONE).

The repaired rows then go through the same-role recluster. The report stages
the repairs inside a SAVEPOINT, plans the recluster on that staged data, and
rolls back, so it predicts exactly what the apply run would merge. No network.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field

from blastjob.core import recluster
from blastjob.discovery.base import RawPosting
from blastjob.discovery.linkedin_cards import (
    SPLIT_KNOWN,
    SPLIT_VOCAB,
    CardSplit,
    is_card_text,
    known_company_names,
    split_card,
)
from blastjob.discovery.normalize import to_job_columns
from blastjob.discovery.textutil import split_location
from blastjob.models.config import DiscoveryConfig

BASIS_SIBLING = "sibling"
BASIS_DB = "db"
BASIS_VOCAB = "vocab"
BASIS_NONE = "none"

_REPAIRED_COLS = (
    "company",
    "title",
    "geo_location_raw",
    "geo_city",
    "geo_region",
    "geo_remote",
)
_SAVEPOINT = "linkedin_repair_report"


@dataclass(frozen=True)
class Repair:
    job_id: int
    old_title: str
    card: CardSplit
    basis: str
    accepted: bool

    @property
    def status(self) -> str:
        if self.basis == BASIS_NONE:
            return "no split (left as is)"
        if self.basis == BASIS_VOCAB:
            return "accepted (vocab)" if self.accepted else "needs confirmation (vocab)"
        return f"apply ({self.basis})"


@dataclass(frozen=True)
class RepairOutcome:
    repairs: list[Repair]
    repaired_ids: list[int]
    merges: list[recluster.Merge]
    describe: list[dict] = field(default_factory=list)
    merged: int | None = None  # None in report mode


def _sibling_companies(conn: sqlite3.Connection) -> dict[int, set[str]]:
    rows = conn.execute(
        "SELECT cluster_id, company FROM jobs "
        "WHERE cluster_id IS NOT NULL AND TRIM(COALESCE(company,'')) <> ''"
    ).fetchall()
    out: dict[int, set[str]] = {}
    for r in rows:
        out.setdefault(int(r["cluster_id"]), set()).add(r["company"].strip())
    return out


def _classify(title: str, siblings: set[str], db_names: frozenset[str]) -> tuple[CardSplit, str]:
    card = split_card(title, siblings)
    if card.basis == SPLIT_KNOWN:
        return card, BASIS_SIBLING
    card = split_card(title, db_names)
    if card.basis == SPLIT_KNOWN:
        return card, BASIS_DB
    if card.basis == SPLIT_VOCAB:
        return card, BASIS_VOCAB
    return card, BASIS_NONE


def plan_repairs(conn: sqlite3.Connection, accept: Iterable[int] = ()) -> list[Repair]:
    """One Repair per broken row, in id order. `accept` confirms vocab splits."""
    accepted_ids = set(accept)
    rows = conn.execute(
        "SELECT id, title, cluster_id FROM jobs "
        "WHERE source='linkedin' AND COALESCE(company,'')='' ORDER BY id"
    ).fetchall()
    siblings = _sibling_companies(conn)
    db_names = known_company_names(conn)
    out: list[Repair] = []
    for r in rows:
        title = r["title"] or ""
        if not is_card_text(title):
            continue
        cid = r["cluster_id"]
        card, basis = _classify(title, siblings.get(int(cid), set()) if cid else set(), db_names)
        accepted = basis in (BASIS_SIBLING, BASIS_DB) or (
            basis == BASIS_VOCAB and int(r["id"]) in accepted_ids
        )
        out.append(Repair(int(r["id"]), title, card, basis, accepted))
    return out


def _columns(card: CardSplit) -> dict[str, object]:
    """The repaired columns, built by the same normalizer discovery uses."""
    city, region = split_location(card.location)
    posting = RawPosting(
        source="linkedin",
        external_id="",
        company=card.company,
        title=card.title,
        location_raw=card.location,
        city=city,
        region=region,
        remote=True if card.remote else None,
    )
    cols = to_job_columns(posting)
    return {k: cols[k] for k in _REPAIRED_COLS if k in cols}


def stage_repairs(conn: sqlite3.Connection, repairs: list[Repair]) -> list[int]:
    """UPDATE the accepted rows (CAS on the broken shape). Does not commit."""
    done: list[int] = []
    for rep in repairs:
        if not rep.accepted:
            continue
        cols = _columns(rep.card)
        sets = ", ".join(f"{k}=?" for k in cols)
        cur = conn.execute(
            f"UPDATE jobs SET {sets} WHERE id=? AND source='linkedin' "
            "AND COALESCE(company,'')='' AND title=?",
            (*cols.values(), rep.job_id, rep.old_title),
        )
        if cur.rowcount == 1:
            done.append(rep.job_id)
    return done


def _plan_merges(
    conn: sqlite3.Connection, radius_miles: float, geo: DiscoveryConfig, ids: list[int]
) -> tuple[list[recluster.Merge], list[dict]]:
    from blastjob.web import recluster_view

    plan = recluster.plan_recluster(conn, radius_miles, geo, only_ids=set(ids)) if ids else []
    return plan, recluster_view.describe(conn, plan)


def run_repair(
    conn: sqlite3.Connection,
    radius_miles: float,
    geo: DiscoveryConfig,
    *,
    apply: bool,
    accept: Iterable[int] = (),
) -> RepairOutcome:
    """Report (`apply=False`): stage in a savepoint, plan, roll back. Apply:
    stage, commit, recluster the repaired rows."""
    repairs = plan_repairs(conn, accept)
    if not apply:
        conn.execute(f"SAVEPOINT {_SAVEPOINT}")
        try:
            ids = stage_repairs(conn, repairs)
            plan, items = _plan_merges(conn, radius_miles, geo, ids)
        finally:
            conn.execute(f"ROLLBACK TO {_SAVEPOINT}")
            conn.execute(f"RELEASE {_SAVEPOINT}")
        return RepairOutcome(repairs, ids, plan, items)
    ids = stage_repairs(conn, repairs)
    conn.commit()
    plan, items = _plan_merges(conn, radius_miles, geo, ids)
    merged = recluster.apply_recluster(conn, plan)
    return RepairOutcome(repairs, ids, plan, items, merged)


def format_repair(outcome: RepairOutcome) -> str:
    from blastjob.web import recluster_view

    lines = ["id | old title | new title | company | location | status"]
    for rep in outcome.repairs:
        lines.append(
            f"{rep.job_id} | {rep.old_title} | {rep.card.title} | {rep.card.company or '-'} | "
            f"{rep.card.location or '-'} | {rep.status}"
        )
    verb = "repaired" if outcome.merged is not None else "would repair"
    lines.append(f"{len(outcome.repairs)} broken rows, {verb} {len(outcome.repaired_ids)}")
    lines.append(recluster_view.format_report(outcome.describe, applied=outcome.merged))
    return "\n".join(lines)

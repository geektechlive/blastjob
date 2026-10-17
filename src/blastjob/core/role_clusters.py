"""One role, many hats: when two postings are the same role, and cluster edits.

Clusters are display-only and reversible (see discovery/pipeline.py). This
module owns the two things that change them after insert:

  - the AUTO rule (`auto_same_role`): same employer + same normalized title,
    and either both from aggregator/incremental sources within
    `AUTO_WINDOW_DAYS` (aggregators syndicate one req to many cities), or
    different sources in the same metro. The loose allowance (a remote or
    unplaceable side counts as the same metro) applies only when BOTH sides
    are non-authoritative: a board posting in a cluster is proof of closure
    for its siblings (core/closure_links.db_link), so a board posting joins
    only on real proximity or when both sides say remote. Two rows from the
    same authoritative ATS board are different reqs, always.
    Agencies and aggregator companies never auto-merge (see core.same_role:
    those are suggestions the user confirms).
    A REPOST of an application in progress (applied/screen/interview/offer)
    joins it with no time window, same source allowed, when exactly one side
    is that application, the other has no application history, both sides are
    non-authoritative and the metro is strict. A closed application's repost
    is never merged (it may be a new req): core.same_role suggests it.
  - The user's decisions (`same_role_decisions`): 'same' merges two clusters,
    'different' splits a pair and blocks every later merge of it.

Merges are cluster-level (every member moves), and never cross a 'different'
pair or put two reqs from one authoritative board together.
"""

from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from blastjob.core.agencies import is_agency
from blastjob.core.closure_links import Link, employer_jd_ref, is_authoritative
from blastjob.core.company import company_key
from blastjob.core.titles import normalize_role_title
from blastjob.db import client as dbc
from blastjob.discovery.textutil import normalize_region
from blastjob.matching.geo import classify_location, haversine_miles, town_centroid
from blastjob.models.config import DiscoveryConfig

# Aggregators re-list one req across cities in the same few days.
AUTO_WINDOW_DAYS = 7
# An application still in progress: a repost of it is the same req. Closed
# applications are excluded on purpose (a repost after a rejection may be a
# genuinely new req, and merging would hide it in the Closed stage).
IN_PROCESS_STATES = frozenset({"applied", "screen", "interview", "offer"})
SAME = "same"
DIFFERENT = "different"
DECISIONS: tuple[str, ...] = (SAME, DIFFERENT)

_COUNTRY_ONLY = frozenset({"united states", "usa", "us"})
FACT_COLS = (
    "id, source, external_id, company, title, geo_city, geo_region, geo_location_raw, "
    "geo_remote, first_seen, cluster_id, review_state, application_state, applied_at, "
    "source_state"
)

Row = Mapping[str, Any] | sqlite3.Row
Pair = tuple[int, int]


# --- places ------------------------------------------------------------------


@dataclass(frozen=True)
class Place:
    remote: bool
    city: str
    centroid: tuple[float, float] | None
    region: str = ""  # upper-cased state/region code, "" when unknown

    @property
    def unplaceable(self) -> bool:
        return not self.remote and self.centroid is None and self.city in ("", *_COUNTRY_ONLY)


def _state_code(region: str) -> str:
    """Upper-cased two-letter state/province code, else "" (unknown). Unrecognised
    text ("United States", "USA") must not read as a state that conflicts with
    "WI" for the same city."""
    code = normalize_region(region or "").upper()
    return code if len(code) == 2 and code.isalpha() and code.lower() not in _COUNTRY_ONLY else ""


def _norm_city(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else " " for ch in (text or "").lower())
    return " ".join(cleaned.split())


def place_of(row: Row, geo: DiscoveryConfig) -> Place:
    """Where `row` is, for clustering purposes: the configured town that
    `matching.geo.classify_location` finds, so an explicit other-state or
    foreign location ("Jackson, MS" when a home town is also named Jackson)
    gets no centroid rather than borrowing the home town's.

    A multi-location row ("A; B; C") is placed at its primary location only:
    geo may let it through on a later home-state location, but two postings
    are only the same metro by where the req is anchored, so a far req can't
    auto-merge with a different req an aggregator posted locally."""
    city = _norm_city(row["geo_city"] or "")
    primary = (row["geo_location_raw"] or "").split(";", 1)[0]
    _signal, key = classify_location(row["geo_city"] or "", row["geo_region"] or "", primary, geo)
    return Place(
        remote=bool(row["geo_remote"]),
        city=city,
        centroid=town_centroid(geo.towns, key) if key else None,
        region=_state_code(row["geo_region"] or ""),
    )


def same_metro(a: Place, b: Place, radius_miles: float, *, loose: bool) -> bool:
    """Same normalized city (in the same state, when both are known), both
    centroids within `radius_miles`, or both remote. `loose` also accepts a
    remote or unplaceable side ("United States", empty): fine for the same
    employer's own title, too weak to tie an agency posting to an employer.
    The city-name shortcut needs equal regions because "Springfield, IL" and
    "Springfield, MO" are not one metro; radius matching is unchanged."""
    if a.remote and b.remote:
        return True
    if loose and (a.remote or b.remote or a.unplaceable or b.unplaceable):
        return True
    same_region = not (a.region and b.region) or a.region == b.region
    if a.city and a.city == b.city and a.city not in _COUNTRY_ONLY and same_region:
        return True
    if a.centroid and b.centroid:
        return haversine_miles(*a.centroid, *b.centroid) <= radius_miles
    return False


def days_apart(a: str | None, b: str | None) -> float | None:
    try:
        da = datetime.datetime.fromisoformat((a or "")[:26])
        db = datetime.datetime.fromisoformat((b or "")[:26])
    except ValueError:
        return None
    return abs((da - db).total_seconds()) / 86_400


# --- the auto rule -----------------------------------------------------------


def is_active_application(row: Row) -> bool:
    return row["review_state"] == "kept" and (row["application_state"] or "") in (IN_PROCESS_STATES)


def has_application(row: Row) -> bool:
    """A kept row the user actually applied through: any application state past
    'drafted', or an applied_at date. 'drafted' alone is a packet, not an
    application, so a merge never archives it (web/same_role.decide)."""
    if row["review_state"] != "kept":
        return False
    state = (row["application_state"] or "").strip()
    return state not in ("", "drafted") or bool((row["applied_at"] or "").strip())


def _no_application_history(row: Row) -> bool:
    if row["review_state"] != "kept":
        return True
    return not (row["application_state"] or "") and not (row["applied_at"] or "")


def _repost_of_application(a: Row, b: Row, radius_miles: float, geo: DiscoveryConfig) -> bool:
    """Exactly one side is an application in progress, the other was never
    acted on, and they are strictly the same metro. No time window and the
    same source is fine: a repost is the same req re-served under a new id.
    Two applications, or a loose location, are left to the user (core.same_role
    suggests them as PREVIOUS)."""
    active_a, active_b = is_active_application(a), is_active_application(b)
    if active_a == active_b:
        return False
    fresh = b if active_a else a
    if not _no_application_history(fresh):
        return False
    return same_metro(place_of(a, geo), place_of(b, geo), radius_miles, loose=False)


def auto_same_role(a: Row, b: Row, radius_miles: float, geo: DiscoveryConfig) -> bool:
    """True when two rows are the same role by the automatic rule (module doc)."""
    if a["id"] == b["id"] or is_agency(a["company"] or "") or is_agency(b["company"] or ""):
        return False
    if not company_key(a["company"] or "") or company_key(a["company"] or "") != company_key(
        b["company"] or ""
    ):
        return False
    title = normalize_role_title(a["title"])
    if not title or title != normalize_role_title(b["title"]):
        return False
    auth_a, auth_b = is_authoritative(a["source"]), is_authoritative(b["source"])
    if a["source"] == b["source"] and auth_a:
        return False  # one board, two external ids: two reqs
    both_aggregated = not auth_a and not auth_b
    if both_aggregated and _repost_of_application(a, b, radius_miles, geo):
        return True
    if both_aggregated:
        gap = days_apart(a["first_seen"], b["first_seen"])
        if gap is not None and gap <= AUTO_WINDOW_DAYS:
            return True
    return a["source"] != b["source"] and same_metro(
        place_of(a, geo), place_of(b, geo), radius_miles, loose=both_aggregated
    )


def sibling_is_proof(
    conn: sqlite3.Connection, job_id: int, other_id: int, radius: float, geo: DiscoveryConfig
) -> bool:
    """May a cluster sibling stand in for this job's posting (closure)? Only
    when the user confirmed them the same role, or they are strictly the same
    metro (same city, centroids within `radius`, or both remote). A loose or
    legacy grouping is not proof: closing a live job on it is the costly error."""
    job_a, job_b = pair_of(job_id, other_id)
    decided = conn.execute(
        "SELECT decision FROM same_role_decisions WHERE job_a=? AND job_b=?", (job_a, job_b)
    ).fetchone()
    if decided is not None:
        return bool(decided["decision"] == SAME)
    rows = conn.execute(
        f"SELECT {FACT_COLS} FROM jobs WHERE id IN (?, ?)", (job_id, other_id)
    ).fetchall()
    if len(rows) != 2:
        return False
    return same_metro(place_of(rows[0], geo), place_of(rows[1], geo), radius, loose=False)


# --- decisions ---------------------------------------------------------------


def pair_of(a: int, b: int) -> Pair:
    return (a, b) if a < b else (b, a)


def decisions(conn: sqlite3.Connection) -> dict[Pair, str]:
    rows = conn.execute("SELECT job_a, job_b, decision FROM same_role_decisions").fetchall()
    return {(int(r["job_a"]), int(r["job_b"])): r["decision"] for r in rows}


def record_decision(conn: sqlite3.Connection, a: int, b: int, decision: str) -> None:
    if decision not in DECISIONS or a == b:
        raise ValueError(f"bad same-role decision {decision!r} for {a}/{b}")
    job_a, job_b = pair_of(a, b)
    conn.execute(
        "INSERT INTO same_role_decisions (job_a, job_b, decision, decided_at) VALUES (?,?,?,?) "
        "ON CONFLICT(job_a, job_b) DO UPDATE SET decision=excluded.decision, "
        "decided_at=excluded.decided_at",
        (job_a, job_b, decision, datetime.datetime.now().isoformat(timespec="seconds")),
    )


# --- cluster edits -----------------------------------------------------------


def _members(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    row = conn.execute(f"SELECT {FACT_COLS} FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return []
    if row["cluster_id"] is None:
        return [row]
    return conn.execute(
        f"SELECT {FACT_COLS} FROM jobs WHERE cluster_id=? ORDER BY id", (row["cluster_id"],)
    ).fetchall()


def conflict(
    side_a: Iterable[Row], side_b: Iterable[Row], decided: Mapping[Pair, str], *, auto: bool = False
) -> str:
    """Why two groups must not be joined, or "" if they may. `auto` (the
    backfill's transitive union) also refuses to join two groups that each hold
    an active application: the pairwise rule never merges two applications, and
    one repost matching both must not bridge them. The user can still join them by
    hand ('same')."""
    side_a, rows_b = list(side_a), list(side_b)
    if auto and any(map(is_active_application, side_a)) and any(map(is_active_application, rows_b)):
        return "both hold an application in progress"
    for x in side_a:
        for y in rows_b:
            if decided.get(pair_of(x["id"], y["id"])) == DIFFERENT:
                return f"#{x['id']} and #{y['id']} were marked not the same role"
            if (
                x["source"] == y["source"]
                and is_authoritative(x["source"])
                and x["external_id"] != y["external_id"]
            ):
                return f"#{x['id']} and #{y['id']} are two reqs on one board"
    return ""


def _new_cluster(conn: sqlite3.Connection, fingerprint: str) -> int:
    cur = conn.execute(
        "INSERT INTO job_clusters (fingerprint, created_at) VALUES (?, ?)",
        (fingerprint, datetime.datetime.now().isoformat()),
    )
    return int(cur.lastrowid or 0)


def merge_jobs(conn: sqlite3.Connection, a: int, b: int) -> str:
    """Join the clusters of jobs `a` and `b` (every member of both). Returns ""
    on success (or if already together), else the reason it was refused.
    Does not commit."""
    side_a, side_b = _members(conn, a), _members(conn, b)
    if not side_a or not side_b:
        return "job not found"
    if side_a[0]["cluster_id"] is not None and side_a[0]["cluster_id"] == side_b[0]["cluster_id"]:
        return ""
    reason = conflict(side_a, side_b, decisions(conn))
    if reason:
        return reason
    existing = [s[0]["cluster_id"] for s in (side_a, side_b) if s[0]["cluster_id"] is not None]
    target = min(existing) if existing else _new_cluster(conn, f"same-role:{min(a, b)}")
    ids = [r["id"] for r in (*side_a, *side_b)]
    conn.execute(
        f"UPDATE jobs SET cluster_id=? WHERE id IN ({','.join('?' for _ in ids)})",
        (target, *ids),
    )
    return ""


def split_pair(
    conn: sqlite3.Connection, keep: int, other: int, radius_miles: float, geo: DiscoveryConfig
) -> list[int]:
    """The user says `keep` and `other` are not the same role: record it, and if
    they share a cluster, move `other` out with whatever still belongs with it
    (members linked to it by the auto rule or a 'same' decision, not through
    `keep`). If `keep` is reachable from `other` some other way, `other` moves
    alone; the recorded 'different' then blocks any merge back. Commits, once, at
    the end: the description reverts (see `clear_links_between`) share the
    caller's transaction, so a failure anywhere rolls all of it back. Returns the
    ids whose adopted description was reverted (they need a re-score)."""
    record_decision(conn, keep, other, DIFFERENT)
    reverted = clear_links_between(conn, keep, other)
    members = _members(conn, keep)
    by_id = {int(m["id"]): m for m in members}
    if other not in by_id or len(members) < 2:
        conn.commit()
        return reverted
    decided = decisions(conn)
    component = _component(other, by_id, decided, radius_miles, geo)
    # When `keep` is still reachable from `other` (keep~z~other), only `other`
    # moves, even if that strands a true duplicate z in `keep`'s cluster. Moving
    # the whole component would drag `keep` along and undo the split the user just
    # made; the recorded 'different' then blocks the backfill from re-joining.
    # Do not "fix" this by moving the component: a stranded duplicate is
    # visible and splittable, a silently re-merged pair is not.
    moving = {other} if keep in component else component
    target = _new_cluster(conn, f"split:{other}")
    conn.execute(
        f"UPDATE jobs SET cluster_id=? WHERE id IN ({','.join('?' for _ in moving)})",
        (target, *sorted(moving)),
    )
    conn.commit()
    return reverted


def clear_links_between(conn: sqlite3.Connection, a: int, b: int) -> list[int]:
    """Drop the employer link each of two jobs holds TO the other. A stored
    sibling link was proven by the pairing the user just rejected, so it must not
    keep standing in for the posting; neither may a description adopted from the
    other job's posting, which is reverted too (both ways). The user's manual
    link is display-only data and is never touched. Returns the ids whose
    description was reverted. Does not commit: the caller's transaction owns it."""
    from blastjob.web.apply_link import MANUAL_LINK

    reverted: list[int] = []
    for job, target in ((a, b), (b, a)):
        conn.execute(
            "UPDATE jobs SET employer_link_source='', employer_link_external_id='', "
            "employer_link_url='' WHERE id=? AND COALESCE(employer_link_source,'')<>? "
            "AND (employer_link_source, employer_link_external_id) = "
            "(SELECT source, external_id FROM jobs WHERE id=?)",
            (job, MANUAL_LINK, target),
        )
        held = conn.execute("SELECT source, external_id FROM jobs WHERE id=?", (target,)).fetchone()
        if held is None:
            continue
        ref = employer_jd_ref(Link(held["source"], held["external_id"]))
        if dbc.revert_employer_jd(conn, job, ref=ref):
            reverted.append(job)
    return reverted


def _component(
    start: int,
    by_id: Mapping[int, Row],
    decided: Mapping[Pair, str],
    radius_miles: float,
    geo: DiscoveryConfig,
) -> set[int]:
    def linked(x: int, y: int) -> bool:
        verdict = decided.get(pair_of(x, y))
        if verdict == DIFFERENT:
            return False
        return verdict == SAME or auto_same_role(by_id[x], by_id[y], radius_miles, geo)

    seen, frontier = {start}, [start]
    while frontier:
        x = frontier.pop()
        for y in by_id:
            if y not in seen and linked(x, y):
                seen.add(y)
                frontier.append(y)
    return seen

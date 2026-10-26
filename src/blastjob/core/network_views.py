"""Turns `network_contacts` (rich, but a dead end today) into three actionable
views: who to talk to about a specific job, a ranked recruiter desk, and a
ranked list of dormant senior ties worth reviving.

Deterministic only -- no LLM, no network calls. Every returned dict traces to
a stored `network_contacts` row; nothing here invents a person.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import date

from blastjob.core import network as net
from blastjob.core.company import company_key

# --------------------------------------------------------------------------
# warmth_score
# --------------------------------------------------------------------------

# Seniority contributes a flat bonus -- a senior contact is worth reviving
# even with modest message history. Values are arbitrary but ordered, and
# unknown/'' labels fall back to 0 rather than raising.
_SENIORITY_WEIGHT: dict[str, float] = {"exec": 6.0, "director": 4.0, "manager": 2.0, "ic": 0.0}

# Message-count contribution caps out -- the 15th message with someone tells
# you little more than the 10th did, and an uncapped count would let one
# very chatty stale contact dominate the whole list.
_MESSAGE_COUNT_CAP = 20
_MESSAGE_COUNT_WEIGHT = 2.0

# Recency decays linearly to zero over two years. Chosen so the ranking
# visibly separates "talked last month" from "talked in 2024" (see
# recruiter_desk's docstring) rather than only distinguishing "ever
# messaged" from "never messaged".
_RECENCY_ZERO_AT_DAYS = 730.0


def _days_since(last_message_at: str, today: str) -> float | None:
    """None means "never" (blank or unparseable) -- never raises."""
    if not last_message_at:
        return None
    try:
        then = date.fromisoformat(last_message_at[:10])
        now = date.fromisoformat(today[:10])
    except ValueError:
        return None
    return float((now - then).days)


# Maps the agentic pass's finer-grained tier back onto the four the UI ranks on.
_ENRICH_TIER_TO_SENIORITY = {
    "c_suite": "exec",
    "evp_svp": "exec",
    "vp": "exec",
    "director": "director",
    "manager": "manager",
    "ic": "ic",
}


def _effective_tier(row) -> str:
    """The agentic verdict when there is one, else the deterministic classifier.

    Without this the enrichment pass writes `enrich_tier` and nothing ever reads
    it -- a data-collection step with no consumer.
    """
    try:
        enriched = row["enrich_tier"]
    except (KeyError, IndexError):
        enriched = None
    if enriched:
        return _ENRICH_TIER_TO_SENIORITY.get(enriched, row["seniority"] or "ic")
    return row["seniority"] or "ic"


def warmth_score(message_count: int, last_message_at: str, seniority: str, today: str) -> float:
    """Pure, deterministic ranking signal for one contact. Higher is warmer.

    Combines three independent, always-non-negative components so a caller
    can reason about each in isolation:
      - seniority bonus (flat, by tier)
      - message-volume contribution (capped, so one very chatty stale
        contact can't dominate)
      - recency multiplier applied to the volume contribution (0..1, linear
        decay over ~2 years, 0 for "never" or an unparseable date) -- this is
        what keeps a merely-more-recent contact from being buried under a
        much higher lifetime message count.
    """
    seniority_bonus = _SENIORITY_WEIGHT.get(seniority, 0.0)

    volume = min(max(message_count, 0), _MESSAGE_COUNT_CAP) * _MESSAGE_COUNT_WEIGHT

    days = _days_since(last_message_at, today)
    if days is None:
        recency = 0.0
    else:
        recency = max(0.0, 1.0 - max(days, 0.0) / _RECENCY_ZERO_AT_DAYS)

    return round(seniority_bonus + volume * recency, 4)


# --------------------------------------------------------------------------
# warm_paths_for_job
# --------------------------------------------------------------------------


def warm_paths_for_job(conn: sqlite3.Connection, job_id: int) -> list[dict]:
    """The named people at this job's company, shaped for direct display:
    recommenders first (they already wrote an endorsement -- the strongest
    possible ask), then seniority, then recency -- same ordering as
    `core.network.warm_paths`, just reshaped into the fields a job page
    needs instead of a bare count badge."""
    rows = net.warm_paths(conn, job_id)
    return [
        {
            "id": int(r["id"]),
            "name": r["full_name"],
            "position": r["position"],
            "seniority": _effective_tier(r),
            "url": r["url"],
            "last_message_at": r["last_message_at"],
            "recommended_me": bool(r["recommended_me"]),
        }
        for r in rows
    ]


# --------------------------------------------------------------------------
# Shared: which companies currently have a live (non-archived) posting
# --------------------------------------------------------------------------


def _live_job_company_keys(conn: sqlite3.Connection, keys: Iterable[str]) -> frozenset[str]:
    """Company_keys among `keys` that have at least one non-archived job.
    One batched query regardless of how many keys are passed -- never a
    per-contact lookup."""
    unique = sorted({k for k in keys if k})
    if not unique:
        return frozenset()
    rows = conn.execute(
        "SELECT DISTINCT company FROM jobs WHERE review_state != 'archived' AND company != ''"
    ).fetchall()
    live_keys = {company_key(r["company"]) for r in rows}
    return frozenset(k for k in unique if k in live_keys)


# --------------------------------------------------------------------------
# recruiter_desk
# --------------------------------------------------------------------------

# Keyword match against the recruiter's OWN employer name -- distinguishes a
# search/staffing firm (their business IS recruiting) from an in-house
# recruiter/TA person who happens to carry a recruiter-shaped title at the
# actual hiring company. Deliberately conservative and keyword-based, same
# spirit as core.network's own classify_seniority / is_recruiter -- no LLM.
#
# The named agencies and search firms in YOUR network are a setting
# (`discovery.network_agency_keywords`), installed by
# `set_extra_agency_keywords` whenever the config is loaded.
_AGENCY_COMPANY_KEYWORDS: tuple[str, ...] = (
    "staffing",
    "recruiting",
    "recruitment",
    "executive search",
    "search partners",
    "talent solutions",
    "talent acquisition group",
    "headhunters",
    "consulting group",
)


_EXTRA_AGENCY_KEYWORDS: tuple[str, ...] = ()


def set_extra_agency_keywords(keywords: Iterable[str]) -> None:
    """Replace the configured agency keywords (lowercased substrings)."""
    global _EXTRA_AGENCY_KEYWORDS
    _EXTRA_AGENCY_KEYWORDS = tuple(k.strip().lower() for k in keywords if k.strip())


def _is_agency_company(company: str) -> bool:
    text = (company or "").lower()
    return any(k in text for k in _AGENCY_COMPANY_KEYWORDS + _EXTRA_AGENCY_KEYWORDS)


# Desk groups, most valuable first: retained search matters most for an executive seat.
RECRUITER_GROUPS: tuple[str, ...] = ("exec_search", "agency", "in_house")
# What the user may store as a manual verdict (`recruiter_override`); NULL is automatic.
RECRUITER_OVERRIDES: frozenset[str] = frozenset((*RECRUITER_GROUPS, "not_recruiter"))
_ENRICH_LISTED = ("agency", "exec_search")


def _desk_group(row) -> str:
    """The user's override wins, then the enrichment label, then the keyword rule
    (only for a contact the enrichment has not labelled, or labelled `none`)."""
    for label in (row["recruiter_override"], row["enrich_recruiter"]):
        if label in RECRUITER_GROUPS:
            return label
    return "agency" if _is_agency_company(row["company"]) else "in_house"


def recruiter_desk(conn: sqlite3.Connection) -> list[dict]:
    """Every recruiter connection, grouped exec_search, agency, in_house (then
    ranked by `warmth_score` within a group), each flagged when their own
    employer currently has a live posting.

    A contact is listed when the user marked them a recruiter, or the title rule
    (`is_recruiter`) or the enrichment label (agency / exec_search) says so. The
    user's "not a recruiter" removes them whatever the other signals say.

    Most of these ties are cold -- the highest lifetime message count is
    someone last touched over a year ago -- so warmth_score's recency
    component is what keeps this from just being a volume leaderboard.
    """
    rows = conn.execute(
        "SELECT id, full_name, url, company, company_key, position, seniority, enrich_tier, "
        "enrich_recruiter, recruiter_override, last_message_at, message_count "
        "FROM network_contacts "
        f"WHERE {net.EFFECTIVE_RECRUITER_SQL}"
    ).fetchall()
    if not rows:
        return []

    today = date.today().isoformat()
    live_keys = _live_job_company_keys(conn, (r["company_key"] for r in rows))

    desk = [
        {
            "id": r["id"],
            "name": r["full_name"],
            "url": r["url"],
            "company": r["company"],
            "position": r["position"],
            "group": _desk_group(r),
            "override": r["recruiter_override"] or "",
            "has_live_jobs": r["company_key"] in live_keys,
            "last_message_at": r["last_message_at"],
            "warmth_score": warmth_score(
                r["message_count"], r["last_message_at"], _effective_tier(r), today
            ),
        }
        for r in rows
    ]
    desk.sort(key=lambda d: (RECRUITER_GROUPS.index(d["group"]), -d["warmth_score"]))
    return desk


def set_recruiter_override(conn: sqlite3.Connection, contact_id: int, value: str) -> bool:
    """Store the user's verdict ("auto" clears it). False for an unknown contact;
    ValueError for a value outside the allowed set."""
    if value != "auto" and value not in RECRUITER_OVERRIDES:
        raise ValueError(value)
    cur = conn.execute(
        "UPDATE network_contacts SET recruiter_override=? WHERE id=?",
        (None if value == "auto" else value, contact_id),
    )
    conn.commit()
    return cur.rowcount == 1


_SEARCH_LIMIT = 25


def search_contacts(conn: sqlite3.Connection, query: str) -> list[dict]:
    """Network contacts whose name, employer or title contains `query`, so one
    that is not on the desk can be marked a recruiter. Empty query, no rows."""
    text = query.strip()
    if not text:
        return []
    like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = conn.execute(
        "SELECT id, full_name, company, position, enrich_recruiter, recruiter_override "
        "FROM network_contacts WHERE full_name LIKE ? ESCAPE '\\' "
        "OR company LIKE ? ESCAPE '\\' OR position LIKE ? ESCAPE '\\' "
        "ORDER BY full_name LIMIT ?",
        (like, like, like, _SEARCH_LIMIT),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["full_name"],
            "company": r["company"],
            "position": r["position"],
            "label": r["enrich_recruiter"] or "",
            "override": r["recruiter_override"] or "",
        }
        for r in rows
    ]


# --------------------------------------------------------------------------
# dormant_ties
# --------------------------------------------------------------------------

_WARM_SENIORITIES = frozenset({"exec", "director"})


def dormant_ties(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Exec/director contacts the user has actually exchanged messages with,
    ranked so a live opening at their current employer always wins, and
    warmth_score breaks ties within that. These are real relationships with
    senior people that are currently invisible anywhere in the cockpit."""
    placeholders = ",".join("?" for _ in _WARM_SENIORITIES)
    rows = conn.execute(
        "SELECT full_name, url, company, company_key, position, seniority, enrich_tier, "
        "last_message_at, message_count FROM network_contacts "
        f"WHERE {net._EFFECTIVE_TIER_SQL} IN ({placeholders}) AND message_count > 0",
        tuple(_WARM_SENIORITIES),
    ).fetchall()
    if not rows:
        return []

    today = date.today().isoformat()
    live_keys = _live_job_company_keys(conn, (r["company_key"] for r in rows))

    ties = [
        {
            "name": r["full_name"],
            "url": r["url"],
            "company": r["company"],
            "position": r["position"],
            "seniority": _effective_tier(r),
            "last_message_at": r["last_message_at"],
            "has_live_jobs": r["company_key"] in live_keys,
            "warmth_score": warmth_score(
                r["message_count"], r["last_message_at"], _effective_tier(r), today
            ),
        }
        for r in rows
    ]
    ties.sort(key=lambda d: (d["has_live_jobs"], d["warmth_score"]), reverse=True)
    return ties[:limit]

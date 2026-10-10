"""Generic "Compare" view: a side-by-side table of surfaced (or queued/kept)
jobs with red flags called out — aggregator postings, unverified/below-floor
comp, vague location. Pure SQL + dict building; no LLM calls here."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any, Union

from blastjob.core import company as co

# Re-exported: the rule lives in core so discovery and core can use it too.
from blastjob.core.agencies import AGGREGATOR_COMPANIES as AGGREGATOR_COMPANIES
from blastjob.core.agencies import is_agency as is_agency
from blastjob.web import stage_lists

_COUNTRY_ONLY = {"united states", "usa", "us"}


_COLS = (
    "id, source, company, title, url, alignment_score, rationale, match_strengths, "
    "match_gaps, comp_min, comp_max, comp_provenance, geo_remote, geo_location_raw, "
    "geo_city, geo_region, review_state, application_state, first_seen, cluster_id"
)


# These helpers read a job row by key and are handed either a sqlite3.Row (from
# the routes) or a plain dict (from templates and tests). sqlite3.Row supports
# __getitem__ and keys() but is not a typeshed Mapping, so spell out both.
RowLike = Union[Mapping[str, Any], "sqlite3.Row"]


def red_flags(row: RowLike, profile: dict | None, floor: int) -> list[str]:
    """Things worth noticing before spending tailoring effort on this role."""
    flags: list[str] = []
    if is_agency(row["company"]):
        flags.append("aggregator / recruiting platform — not the employer")
    if row["comp_provenance"] != "posted":
        flags.append("comp not posted")
    elif (row["comp_max"] or 0) < floor:
        flags.append("posted max below floor")
    location = (row["geo_location_raw"] or "").strip().lower()
    if location in _COUNTRY_ONLY and not row["geo_remote"]:
        flags.append("location: country only")
    return flags


_COMP_SUFFIX = {"posted": "posted", "inferred": "inferred", "estimated": "est."}


def comp_label(row: RowLike) -> str:
    """A short human-readable comp summary, provenance-aware.

    Only `posted` is the employer's published number; everything else is labelled
    as our own reading. A lookup rather than a chain of branches, because the
    chain's `else` silently turned a real inferred range into "comp n/a".
    """
    provenance = row["comp_provenance"]
    low, high = row["comp_min"], row["comp_max"]
    suffix = _COMP_SUFFIX.get(provenance, "")
    if not suffix or not (low or high):
        return "comp n/a"
    if low and high and low != high:
        return f"${low // 1000}k–${high // 1000}k {suffix}"
    value = high or low
    return f"${value // 1000}k {suffix}"


def _where(row: RowLike) -> str:
    location = row["geo_location_raw"] or row["geo_city"] or row["geo_region"] or ""
    if row["geo_remote"]:
        return f"Remote · {location}" if location else "Remote"
    return location or "n/a"


def _split_lines(text: str | None) -> list[str]:
    return [line.strip() for line in (text or "").split("\n") if line.strip()]


def compare_rows(
    conn: sqlite3.Connection,
    *,
    source: str = "",
    state: str = "surfaced",
    floor: int = 0,  # the caller passes discovery.comp_floor_usd; 0 = no floor
) -> list[dict]:
    """Active jobs in `state` (optionally filtered to one `source`), ranked by
    alignment, each enriched with its company profile and computed flags."""
    sql = f"SELECT {_COLS} FROM jobs WHERE source_state='active' AND review_state=?"
    params: list = [state]
    if source:
        sql += " AND source=?"
        params.append(source)
    sql += " ORDER BY alignment_score DESC, id"
    rows = conn.execute(sql, params).fetchall()
    # One row per cross-source cluster. Rows are already ranked by alignment, so
    # for a review state the first member wins; for kept, application progress
    # decides (see stage_lists.collapse_kept).
    rows = stage_lists.collapse_kept(rows)

    out: list[dict] = []
    for row in rows:
        profile = co.get_profile(conn, row["company"])
        size = ""
        if profile:
            size = profile.get("size_band") or (
                f"{profile['employees']:,} employees" if profile.get("employees") else ""
            )
        out.append(
            {
                **dict(row),
                "profile": profile,
                "company_key": co.company_key(row["company"]),
                "industry": profile.get("industry", "") if profile else "",
                "size": size,
                "hq": profile.get("hq", "") if profile else "",
                "where": _where(row),
                "comp": comp_label(row),
                "strengths": _split_lines(row["match_strengths"]),
                "gaps": _split_lines(row["match_gaps"]),
                "flags": red_flags(row, profile, floor),
            }
        )
    return out

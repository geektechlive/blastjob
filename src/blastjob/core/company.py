"""Company profiles and compensation signals as queryable data.

A company workup is useful on the day of the interview; the
numbers in it are useful for months (comparing employers, spotting a lowball,
remembering that "the recruiter didn't blink at the asking number"). This module owns the
`company_profiles` and `comp_signals` tables (schema migrations 11-12).

Profiles are keyed by a normalized company name so a posting from Adzuna
("ACME WIDGET CORP") and one from LinkedIn ("Acme Widget Corp") hit
the same row. Typed columns hold what gets compared across companies; JSON
columns hold lists (leadership, stack, events, sources); `extra` holds
anything that doesn't fit yet, so a field is never lost for lack of a column.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from blastjob.core.paths import canonical

_NORM = re.compile(r"[^a-z0-9]+")
# "co" is deliberately guarded with a negative lookahead: "\bco\b" alone also
# matches the "co" in "CO-OP" (a hyphen counts as a word boundary), which
# mangled "CO-OP Financial Services" down to "op financial services".
_SUFFIX = re.compile(r"\b(?:inc|llc|corp|corporation|company|ltd|plc|co(?!-?op\b))\b")
_LEADING_THE = re.compile(r"^the\s+")
_APOSTROPHES = re.compile(r"[’']")

COMP_KINDS: tuple[str, ...] = (
    "aggregator_estimate",  # Glassdoor / ZipRecruiter / Indeed model guesses
    "posted_range",  # stated in the posting
    "market_band",  # our own read of the market for the seat
    "asked_minimum",  # what the candidate told them
    "employer_response",  # how they reacted (low/high may be empty)
    "verbal_range",  # a range the employer said out loud
    "offer",
    # What the candidate earns today. Without this the scorer can measure skills
    # overlap but cannot answer the question that actually decides whether a role
    # is worth their scarce time: is this a raise, and is it a step up? Recorded
    # against their current employer, with no job_id.
    "current_salary",
    # What they earned in a PRIOR role. A career peak total comp figure is often
    # a far better read of market value than a current salary, which may reflect
    # a post-layoff step down rather than true level.
    "prior_salary",
)

JSON_COLUMNS = ("it_leadership", "tech_stack", "recent_events", "sources", "extra")
PROFILE_COLUMNS = (
    "company",
    "legal_name",
    "website",
    "hq",
    "founded",
    "industry",
    "ownership",
    "size_band",
    "employees",
    "revenue_usd",
    "revenue_growth_pct",
    "total_assets_usd",
    "net_income_usd",
    "customers",
    "locations",
    "fiscal_period",
    "ceo",
    "ceo_since",
    "it_leadership",
    "tech_stack",
    "strategy",
    "recent_events",
    "culture_rating",
    "culture_notes",
    "risks",
    "questions",
    "summary",
    "sources",
    "workup_path",
    "extra",
)


def _base_company_key(name: str) -> str:
    """The generic normalization: lowercase, drop apostrophes (so "Fabrikam's"
    lines up with "Fabrikams" instead of splitting into "fabrikam s"), drop a
    leading "The", strip the common legal suffixes, then collapse remaining
    punctuation to spaces. 'ACME WIDGET CORP, Inc.' -> 'acme widget
    corp'; 'The Globex Company' -> 'globex'."""
    low = _APOSTROPHES.sub("", (name or "").lower())
    low = _LEADING_THE.sub("", low)
    low = _SUFFIX.sub("", low)
    return _NORM.sub(" ", low).strip()


# Legal-name / brand-name pairs that no generic suffix regex can cover --
# e.g. the legal entity on a LinkedIn profile ("Contoso Mutual Life Insurance
# Company") and the brand name on a job posting ("Contoso Mutual") otherwise
# produce different keys and warm_paths silently returns zero connections.
# Also covers a work_history.md former employer written one way (e.g. "Acme
# IntelliServ") vs. a posting that just says the shorter brand (see
# `[candidate] former_employers`).
#
# This is deliberately an explicit, reviewable list rather than a wider regex
# -- widening the suffix/descriptor stripping to cover cases like these risks
# collapsing two genuinely different companies into one key. The pairs are a
# setting (`discovery.company_aliases`, raw name -> raw name, empty by
# default), installed here by `set_company_aliases` whenever the config is
# loaded (`config.apply_runtime_settings`), because `company_key` is called
# from everywhere without a config in hand. Re-key existing stored rows with
# `rekey_companies` (a one-time, hand-run pass -- never automatic on import).
_COMPANY_ALIASES: dict[str, str] = {}


def set_company_aliases(pairs: Mapping[str, str]) -> None:
    """Replace the alias table with `pairs` (raw names, normalized here)."""
    global _COMPANY_ALIASES
    _COMPANY_ALIASES = {
        _base_company_key(src): _base_company_key(dst)
        for src, dst in pairs.items()
        if _base_company_key(src) and _base_company_key(dst)
    }


# A delimited trailing segment naming an org unit: "Acme Marketing Group -
# Global Division" is Acme Marketing Group. Narrow on purpose: it needs a
# " - " / " – " / " | " delimiter AND one of these three words in the segment,
# so "Foo - Bar", "Acme Holdings" and "Division Street Partners" are untouched
# (see the anti-collapse note on `_COMPANY_ALIASES`).
_DIVISION_SUFFIX = re.compile(r"\s+[-–|]\s+[^-–|]*\b(?:division|region|subsidiary)\b[^-–|]*$", re.I)


def company_key(name: str) -> str:
    """'ACME WIDGET CORP, Inc.' -> 'acme widget'. Drops a
    trailing division segment (`_DIVISION_SUFFIX`), applies the generic
    normalization, then an explicit legal-name/brand-name alias table (see
    `_COMPANY_ALIASES`) for the cases a regex can't safely cover."""
    base = _base_company_key(_DIVISION_SUFFIX.sub("", name or ""))
    return _COMPANY_ALIASES.get(base, base)


_REKEY_TABLES: tuple[tuple[str, str], ...] = (
    ("company_profiles", "company"),
    ("network_contacts", "company"),
    ("company_follows", "company"),
)
# Keyed tables that need their own SELECT: (table, rows as id/name/company_key,
# key column update by id). company_exclusions has no id column (its key is the
# PK), and is matched by key in matching, so a stale key would silently lapse an
# exclusion. comp_signals has no name: a signal tied to a job takes the job's
# company; a job-less one (current/prior salary) is left alone.
_REKEY_QUERIES: tuple[tuple[str, str, str], ...] = (
    (
        "company_exclusions",
        "SELECT rowid AS id, company AS name, company_key FROM company_exclusions",
        "UPDATE company_exclusions SET company_key=? WHERE rowid=?",
    ),
    (
        "comp_signals",
        "SELECT s.id AS id, j.company AS name, s.company_key FROM comp_signals s "
        "JOIN jobs j ON j.id = s.job_id WHERE TRIM(COALESCE(j.company,'')) <> ''",
        "UPDATE comp_signals SET company_key=? WHERE id=?",
    ),
)


@dataclass(frozen=True)
class RekeyResult:
    """Per-table outcome of one `rekey_companies` pass."""

    updated: int = 0
    conflicts: int = 0


def rekey_companies(conn: sqlite3.Connection) -> dict[str, RekeyResult]:
    """One-time, hand-run pass to re-key rows stored under an older (narrower)
    `company_key` normalization. Recomputes `company_key` from each row's
    stored display name and updates only the rows whose key actually changed.
    Deliberately NOT called automatically on import -- re-keying is a data
    migration the user should run and review once, not something that silently
    reshuffles warm-path matches on the next archive import.

    `company_profiles.company_key` and `company_follows.company_key` are both
    UNIQUE -- widening the normalization can legitimately collapse two
    previously-distinct keys onto one (e.g. a stray "Macy's" row alongside a
    "Macys" row). That's a real duplicate this pass surfaces, not a bug in
    it: the conflicting row is left on its old key rather than raising mid
    pass or silently deleting data, and counted in `conflicts` for the user to
    resolve by hand. `network_contacts.company_key` has no such constraint
    (many contacts legitimately share one company), so it only ever reports
    `updated`. `company_exclusions` (key is the PK) and `comp_signals` (see
    `_REKEY_QUERIES`) are re-keyed the same way.

    Returns a `RekeyResult(updated, conflicts)` per table name."""
    specs = [
        (
            table,
            f"SELECT id, {col} AS name, company_key FROM {table}",
            f"UPDATE {table} SET company_key=? WHERE id=?",
        )
        for table, col in _REKEY_TABLES
    ]
    specs.extend(_REKEY_QUERIES)
    return {table: _rekey(conn, select, update) for table, select, update in specs}


def _rekey(conn: sqlite3.Connection, select_sql: str, update_sql: str) -> RekeyResult:
    updated = conflicts = 0
    for row in conn.execute(select_sql).fetchall():
        new_key = company_key(row["name"] or "")
        if new_key == row["company_key"]:
            continue
        try:
            conn.execute(update_sql, (new_key, row["id"]))
            updated += 1
        except sqlite3.IntegrityError:
            conflicts += 1
    conn.commit()
    return RekeyResult(updated=updated, conflicts=conflicts)


class CompanyProfile(BaseModel):
    """Validated input for upsert. Lists are stored as JSON."""

    model_config = {"extra": "forbid"}

    company: str = Field(min_length=1)
    legal_name: str = ""
    website: str = ""
    hq: str = ""
    founded: int | None = None
    industry: str = ""
    ownership: str = ""
    size_band: str = ""
    employees: int | None = None
    revenue_usd: int | None = None
    revenue_growth_pct: float | None = None
    total_assets_usd: int | None = None
    net_income_usd: int | None = None
    customers: int | None = None
    locations: int | None = None
    fiscal_period: str = ""
    ceo: str = ""
    ceo_since: str = ""
    it_leadership: list[dict[str, Any]] = Field(default_factory=list)
    tech_stack: list[dict[str, Any]] = Field(default_factory=list)
    strategy: str = ""
    recent_events: list[dict[str, Any]] = Field(default_factory=list)
    culture_rating: float | None = None
    culture_notes: str = ""
    risks: str = ""
    questions: str = ""
    summary: str = ""
    sources: list[dict[str, Any]] = Field(default_factory=list)
    workup_path: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _profile_data(profile: CompanyProfile, output_root: str | Path | None) -> dict[str, Any]:
    """The profile as column values: JSON columns encoded and, given the output
    dir, `workup_path` in ROOT form (`paths.canonical`). Its entry may later be
    filed into a bucket; readers find it with `paths.locate` (issue #20). Every
    profile write goes through this."""
    data = profile.model_dump()
    if output_root is not None and data.get("workup_path"):
        data["workup_path"] = str(canonical(data["workup_path"], output_root))
    for col in JSON_COLUMNS:
        data[col] = json.dumps(data[col], ensure_ascii=False)
    return data


def upsert_profile(
    conn: sqlite3.Connection, profile: CompanyProfile, output_root: str | Path | None = None
) -> int:
    """Insert or fully refresh the profile for this company. Returns the row id.

    With `output_root`, a `workup_path` inside the output dir is stored in ROOT
    form (`_profile_data`)."""
    key = company_key(profile.company)
    data = _profile_data(profile, output_root)
    now = _now()
    existing = conn.execute(
        "SELECT id FROM company_profiles WHERE company_key=?", (key,)
    ).fetchone()
    if existing is None:
        cols = ["company_key", *PROFILE_COLUMNS, "created_at", "updated_at"]
        vals = [key, *(data[c] for c in PROFILE_COLUMNS), now, now]
        cur = conn.execute(
            f"INSERT INTO company_profiles ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            vals,
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    assignments = ", ".join(f"{c}=?" for c in PROFILE_COLUMNS)
    conn.execute(
        f"UPDATE company_profiles SET {assignments}, updated_at=? WHERE id=?",
        [*(data[c] for c in PROFILE_COLUMNS), now, existing["id"]],
    )
    conn.commit()
    return int(existing["id"])


# Who owns each profile column when automated research re-runs over an existing
# row. Facts the web can re-prove refresh; anything the owner curates is never
# touched (owner decision 2026-10-16: facts refresh, curated fields never touched).
RESEARCH_OWNED = (
    "legal_name", "website", "hq", "founded", "industry", "ownership", "size_band",
    "employees", "revenue_usd", "revenue_growth_pct", "total_assets_usd", "net_income_usd",
    "customers", "locations", "fiscal_period", "ceo", "ceo_since", "it_leadership",
    "tech_stack", "recent_events", "sources",
)  # fmt: skip
# Prose the owner may have rewritten: research fills it only while it is empty.
FILL_IF_EMPTY = ("strategy", "summary")
# Curated by the owner (ratings, notes, risks, questions, the workup pointer, extra).
USER_OWNED = (
    "workup_path", "culture_rating", "culture_notes", "risks", "questions", "extra",
)  # fmt: skip


def refresh_research_facts(
    conn: sqlite3.Connection,
    profile: CompanyProfile,
    *,
    expected_updated_at: str | None,
    output_root: str | Path | None = None,
) -> int | None:
    """Save a research result without overwriting curated data. Returns the row
    id, or None (nothing written) when the profile changed since `expected_updated_at`
    was read at the start of the research run (None = "there was no profile").

    A new company is inserted whole; an existing one gets only the
    RESEARCH_OWNED columns, plus FILL_IF_EMPTY prose that is still empty.
    Compare-and-set on `updated_at` under one write lock. Pass `output_root`
    so a `workup_path` is stored in root form."""
    key = company_key(profile.company)
    data = _profile_data(profile, output_root)
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT id, updated_at, strategy, summary FROM company_profiles WHERE company_key=?",
        (key,),
    ).fetchone()
    now = _now()
    if row is None and expected_updated_at is None:
        cols = ["company_key", *PROFILE_COLUMNS, "created_at", "updated_at"]
        cur = conn.execute(
            f"INSERT INTO company_profiles ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            [key, *(data[c] for c in PROFILE_COLUMNS), now, now],
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    if row is None or row["updated_at"] != expected_updated_at:
        conn.rollback()
        return None
    # A thinner re-run (the model returned null/""/[]) must not erase stored facts.
    fresh = [c for c in RESEARCH_OWNED if getattr(profile, c) not in (None, "", [])]
    columns = [*fresh, *(c for c in FILL_IF_EMPTY if not (row[c] or "").strip())]
    if not columns:
        conn.rollback()
        return int(row["id"])
    assignments = ", ".join(f"{c}=?" for c in columns)
    conn.execute(
        f"UPDATE company_profiles SET {assignments}, updated_at=? WHERE id=?",
        [*(data[c] for c in columns), now, row["id"]],
    )
    conn.commit()
    return int(row["id"])


def get_profile(conn: sqlite3.Connection, company: str) -> dict | None:
    """Profile as a dict with JSON columns decoded, or None."""
    row = conn.execute(
        "SELECT * FROM company_profiles WHERE company_key=?", (company_key(company),)
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    for col in JSON_COLUMNS:
        try:
            d[col] = json.loads(d[col]) if d[col] else ([] if col != "extra" else {})
        except json.JSONDecodeError:
            d[col] = [] if col != "extra" else {}
    return d


def get_profile_by_key(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute(
        "SELECT company FROM company_profiles WHERE company_key=?", (key,)
    ).fetchone()
    return get_profile(conn, row["company"]) if row else None


def list_profiles(conn: sqlite3.Connection) -> list[dict]:
    """One row per profile plus how many jobs we hold for that company."""
    rows = conn.execute(
        "SELECT company_key, company, hq, industry, employees, revenue_usd, total_assets_usd, "
        "culture_rating, fiscal_period, updated_at FROM company_profiles ORDER BY company"
    ).fetchall()
    counts: dict[str, int] = {}
    for r in conn.execute("SELECT company FROM jobs").fetchall():
        k = company_key(r["company"])
        counts[k] = counts.get(k, 0) + 1
    return [{**dict(r), "job_count": counts.get(r["company_key"], 0)} for r in rows]


def jobs_for_company(conn: sqlite3.Connection, key: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, company, title, review_state, application_state, alignment_score, source "
        "FROM jobs ORDER BY review_state='kept' DESC, alignment_score DESC"
    ).fetchall()
    return [dict(r) for r in rows if company_key(r["company"]) == key]


def add_comp_signal(
    conn: sqlite3.Connection,
    *,
    company: str,
    kind: str,
    job_id: int | None = None,
    low_usd: int | None = None,
    high_usd: int | None = None,
    source: str = "",
    note: str = "",
    observed_at: str | None = None,
) -> int:
    if kind not in COMP_KINDS:
        raise ValueError(f"kind must be one of {COMP_KINDS}, got {kind!r}")
    cur = conn.execute(
        "INSERT INTO comp_signals (job_id, company_key, kind, low_usd, high_usd, source, note, "
        "observed_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            company_key(company),
            kind,
            low_usd,
            high_usd,
            source,
            note.strip()[:500],
            observed_at or datetime.now().date().isoformat(),
            _now(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def comp_signals(
    conn: sqlite3.Connection, *, job_id: int | None = None, company: str | None = None
) -> list[dict]:
    """Signals for a job, or every signal for a company (all its roles), oldest first."""
    if job_id is not None:
        rows = conn.execute(
            "SELECT * FROM comp_signals WHERE job_id=? ORDER BY observed_at, id", (job_id,)
        ).fetchall()
    elif company is not None:
        rows = conn.execute(
            "SELECT * FROM comp_signals WHERE company_key=? ORDER BY observed_at, id",
            (company_key(company),),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM comp_signals ORDER BY observed_at, id").fetchall()
    return [dict(r) for r in rows]


_NO_PUSHBACK_RE = re.compile(r"\bno\s+push(?:ed)?[\s-]*back\b")
# Any of these, once an explicit "no pushback" has been stripped out of the
# note, means the employer rejected the number -- it must NOT be anchored on.
# Matching only the literal token "pushback" missed "they pushed back" and
# resolved a mixed note ("no pushback at 240, pushback at 260") to accepted.
_REJECTION_PHRASES: tuple[str, ...] = (
    "pushback",
    "push back",
    "pushed back",
    "balked",
    "above band",
    "above the band",
    "too high",
    "out of band",
    "declined",
    "rejected",
)


def _note_signals_rejection(note: str) -> bool:
    """True unless the note is either empty or an unambiguous acceptance.
    An explicit 'no pushback' is stripped out first so it doesn't self-match
    the "pushback" rejection phrase; anything left over that still names a
    rejection signal -- including a mixed note that also says "no pushback"
    about a DIFFERENT number -- counts as rejected. Inventing an accepted
    floor the employer actually rejected is worse than reporting none."""
    low = _NO_PUSHBACK_RE.sub("", (note or "").lower())
    return any(phrase in low for phrase in _REJECTION_PHRASES)


def negotiated_floor(conn: sqlite3.Connection, job_id: int) -> int | None:
    """The highest minimum the user has stated for this job that an employer
    actually responded to without pushing back -- the number to anchor the next
    conversation on.

    Returns None if they never asked, if no employer has responded at all, or if
    any response signals rejection. Requiring a response is the point: without
    it, a desired-salary box filled in on an application form was being reported
    as a negotiated floor."""
    rows = comp_signals(conn, job_id=job_id)
    asked = [r for r in rows if r["kind"] == "asked_minimum" and r["low_usd"]]
    if not asked:
        return None
    responses = [r for r in rows if r["kind"] == "employer_response"]
    # Silence is not agreement. A number typed into an application form that no
    # employer has responded to is an ASK, not a negotiated floor -- and this
    # figure reaches the interview prep pack as "the floor to anchor on", so
    # presenting an unanswered ask that way would put a fabricated agreement in
    # the user's mouth in the one conversation where it costs money.
    if not responses:
        return None
    if any(_note_signals_rejection(r["note"] or "") for r in responses):
        return None
    return max(int(r["low_usd"]) for r in asked)

"""Category role searches (#91): the saved searches and the jobs they found.

A category is an interest that is not one company ("Example robotics firms"). It holds up to
three search terms; the weekly run (`discovery/category_search.py`) looks for leadership roles
through Adzuna and web search and records which category found each job in `category_hits`.

Rules kept here, not in the routes:

- Every edit, toggle and delete carries the `version` the form was opened at and is a
  conditional UPDATE/DELETE on it, so a stale tab never overwrites newer text.
- `terms_version` moves only when the terms or the dream flag change. A hit stores the
  `terms_version` it was found under and counts (badge, dream exception) only while that
  equals the category's current one and the category is enabled. A label edit keeps hits.
- Deleting a category cascades to its hits and keeps its jobs.

See docs/how-it-works/discovery.md#category-searches.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

MAX_CATEGORIES = 10
MAX_LABEL = 60
MAX_TERMS = 3
MAX_TERM = 80
MAX_SLUG = 40
_SLUG_JUNK = re.compile(r"[^a-z0-9]+")
_CHUNK = 500  # stays under SQLite's bound-variable ceiling


class CategoryError(ValueError):
    """The submitted category breaks a rule; the message says which."""


class CategoryNotFound(LookupError):
    """No category has that id."""


class StaleCategory(RuntimeError):
    """The category changed (or was deleted) since the form was opened."""


@dataclass(frozen=True)
class Hit:
    """One current category hit on a job. `geo_known` is False while the job's place is
    unknown, so a dream hit is only ever shown for a role whose location is actually known."""

    label: str
    is_dream: bool
    geo_known: bool


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="microseconds")


def slugify(label: str) -> str:
    """Lowercase letters, digits and single hyphens, at most `MAX_SLUG` characters."""
    slug = _SLUG_JUNK.sub("-", label.lower()).strip("-")[:MAX_SLUG]
    return slug.strip("-")


def parse_terms(raw: str) -> list[str]:
    """The terms from a textarea: one per line, blanks ignored, each checked."""
    text = raw.replace("\r\n", "\n")
    terms: list[str] = []
    for line in text.split("\n"):
        term = line.strip()
        if not term:
            continue
        if "\r" in term:
            raise CategoryError("A search term cannot contain a carriage return.")
        if not term.isprintable():
            raise CategoryError("A search term can only contain printable characters.")
        if len(term) > MAX_TERM:
            raise CategoryError(f"A search term is too long ({MAX_TERM} characters at most).")
        terms.append(term)
    if not terms:
        raise CategoryError("Give at least one search term.")
    if len(terms) > MAX_TERMS:
        raise CategoryError(f"Use {MAX_TERMS} search terms at most, one per line.")
    return terms


def _clean_label(label: str) -> str:
    text = " ".join(label.split())
    if not text:
        raise CategoryError("Give the category a label.")
    if not text.isprintable():
        raise CategoryError("The label can only contain printable characters.")
    if len(text) > MAX_LABEL:
        raise CategoryError(f"The label is too long ({MAX_LABEL} characters at most).")
    return text


def terms_of(row: Mapping[str, object] | sqlite3.Row) -> list[str]:
    return [t for t in str(row["terms"]).split("\n") if t]


def get_category(conn: sqlite3.Connection, category_id: int) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM category_searches WHERE id=?", (category_id,)
    ).fetchone()
    return row


def list_categories(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM category_searches ORDER BY id").fetchall()


def add_category(conn: sqlite3.Connection, *, label: str, terms: str, is_dream: bool) -> int:
    clean_label = _clean_label(label)
    clean_terms = parse_terms(terms)
    slug = slugify(clean_label)
    if not slug:
        raise CategoryError("The label needs at least one letter or digit.")
    count = conn.execute("SELECT COUNT(*) FROM category_searches").fetchone()[0]
    if count >= MAX_CATEGORIES:
        raise CategoryError(f"You can keep {MAX_CATEGORIES} category searches at most.")
    if conn.execute("SELECT 1 FROM category_searches WHERE slug=?", (slug,)).fetchone():
        raise CategoryError("A category with that name already exists.")
    cur = conn.execute(
        "INSERT INTO category_searches (slug, label, terms, is_dream, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (slug, clean_label, "\n".join(clean_terms), 1 if is_dream else 0, _now()),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _stale_or_missing(conn: sqlite3.Connection, category_id: int) -> Exception:
    return StaleCategory() if get_category(conn, category_id) else CategoryNotFound(category_id)


def update_category(
    conn: sqlite3.Connection,
    category_id: int,
    version: int,
    *,
    label: str,
    terms: str,
    is_dream: bool,
) -> None:
    """Save an edit made from `version`. StaleCategory leaves the stored row untouched."""
    clean_label = _clean_label(label)
    clean_terms = "\n".join(parse_terms(terms))
    dream = 1 if is_dream else 0
    cur = conn.execute(
        "UPDATE category_searches SET label=?, terms=?, is_dream=?, version=version+1,"
        " terms_version=terms_version + (CASE WHEN terms<>? OR is_dream<>? THEN 1 ELSE 0 END)"
        " WHERE id=? AND version=?",
        (clean_label, clean_terms, dream, clean_terms, dream, category_id, version),
    )
    conn.commit()
    if cur.rowcount != 1:
        raise _stale_or_missing(conn, category_id)


def set_enabled(conn: sqlite3.Connection, category_id: int, version: int, enabled: bool) -> None:
    cur = conn.execute(
        "UPDATE category_searches SET enabled=?, version=version+1 WHERE id=? AND version=?",
        (1 if enabled else 0, category_id, version),
    )
    conn.commit()
    if cur.rowcount != 1:
        raise _stale_or_missing(conn, category_id)


def delete_category(conn: sqlite3.Connection, category_id: int, version: int) -> None:
    cur = conn.execute(
        "DELETE FROM category_searches WHERE id=? AND version=?", (category_id, version)
    )
    conn.commit()
    if cur.rowcount != 1:
        raise _stale_or_missing(conn, category_id)


def attach_hit(conn: sqlite3.Connection, job_id: int, category_id: int, terms_version: int) -> None:
    """Record that `category_id`'s search found `job_id`. Leaves the commit to the caller so the
    job upsert and its hit land together. Finding a job again under newer terms restores it."""
    conn.execute(
        "INSERT INTO category_hits (job_id, category_id, terms_version, first_seen)"
        " VALUES (?, ?, ?, ?) ON CONFLICT(job_id, category_id)"
        " DO UPDATE SET terms_version=excluded.terms_version",
        (job_id, category_id, terms_version, _now()),
    )


# A hit counts only for an enabled category at the terms it was found under.
_CURRENT = (
    "FROM category_hits h JOIN category_searches c ON c.id = h.category_id"
    " WHERE c.enabled=1 AND h.terms_version = c.terms_version"
)


def current_dream_job_ids(conn: sqlite3.Connection) -> frozenset[int]:
    """Jobs with a current hit from a dream category: the ones the geo exception applies to."""
    rows = conn.execute(f"SELECT DISTINCT h.job_id {_CURRENT} AND c.is_dream=1").fetchall()
    return frozenset(int(r[0]) for r in rows)


def current_hits(conn: sqlite3.Connection, job_ids: Iterable[int]) -> dict[int, list[Hit]]:
    """job id -> its current hits, in chunks, never per row. Jobs with none are absent."""
    ids = sorted({int(i) for i in job_ids})
    out: dict[int, list[Hit]] = {}
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i : i + _CHUNK]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT h.job_id, c.label, c.is_dream, COALESCE(j.geo_status, '') AS geo_status "
            "FROM category_hits h JOIN category_searches c ON c.id = h.category_id "
            "JOIN jobs j ON j.id = h.job_id "
            "WHERE c.enabled=1 AND h.terms_version = c.terms_version "
            f"AND h.job_id IN ({marks}) ORDER BY c.id",
            chunk,
        ).fetchall()
        for r in rows:
            known = r["geo_status"] not in ("", "unknown")
            out.setdefault(int(r["job_id"]), []).append(
                Hit(str(r["label"]), bool(r["is_dream"]), known)
            )
    return out


def dream_labels(conn: sqlite3.Connection, job_ids: Iterable[int]) -> dict[int, str]:
    """job id -> comma-joined labels of its current dream categories (weekly Dream list)."""
    return {
        jid: ", ".join(h.label for h in hits if h.is_dream)
        for jid, hits in current_hits(conn, job_ids).items()
        if any(h.is_dream for h in hits)
    }

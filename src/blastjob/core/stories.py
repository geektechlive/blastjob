"""The story bank (#101): interview stories the owner writes once and keeps.

Storage only. Nothing here feeds the prep pack or any model prompt (docs/how-it-works/
stories.md). Every edit and delete carries the `version` the form was opened at and is a
conditional UPDATE/DELETE on it, so a stale tab can never overwrite or remove newer text.
Saving is never blocked by a figure the work history lacks; `unverified_figures` only lists
them for review, by the same whole-token rule the prep pack uses.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from collections.abc import Mapping

from blastjob.core.prep import unverified_figures as _unverified

MAX_TITLE = 80
MAX_EMPLOYER = 80
MAX_STAR = 1500
MAX_TAGS = 200
MIN_YEAR = 1970
STAR_FIELDS = ("situation", "task", "action", "result")
_YEAR = re.compile(r"\d{4}")


class StoryError(ValueError):
    """The submitted story breaks a rule; the message says which."""


class StoryNotFound(LookupError):
    """No story has that id."""


class StaleStory(RuntimeError):
    """The story changed (or was deleted) since the form was opened."""


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="microseconds")


def _norm(text: str) -> str:
    """Trim, and turn a browser's CRLF (or a lone CR) into LF so it doesn't count twice."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _year(raw: str) -> int | None:
    text = raw.strip()
    if not text:
        return None
    this_year = dt.date.today().year
    if not _YEAR.fullmatch(text) or not MIN_YEAR <= int(text) <= this_year:
        raise StoryError(f"The year must be four digits from {MIN_YEAR} to {this_year}, or empty.")
    return int(text)


def _clean(
    *,
    title: str,
    employer: str,
    year: str,
    situation: str,
    task: str,
    action: str,
    result: str,
    tags: str,
) -> dict[str, str | int | None]:
    """Trimmed, checked field values, or StoryError."""
    values = {
        "title": _norm(title),
        "employer": _norm(employer),
        "situation": _norm(situation),
        "task": _norm(task),
        "action": _norm(action),
        "result": _norm(result),
        "tags": _norm(tags),
    }
    if not values["title"]:
        raise StoryError("Give the story a title.")
    if not any(values[f] for f in STAR_FIELDS):
        raise StoryError("Write at least one of situation, task, action or result.")
    limits = {"title": MAX_TITLE, "employer": MAX_EMPLOYER, "tags": MAX_TAGS}
    limits.update(dict.fromkeys(STAR_FIELDS, MAX_STAR))
    for name, limit in limits.items():
        if len(values[name]) > limit:
            raise StoryError(f"{name.capitalize()} is too long ({limit} characters at most).")
    return {**values, "year": _year(year)}


def add_story(
    conn: sqlite3.Connection,
    *,
    title: str,
    employer: str = "",
    year: str = "",
    situation: str = "",
    task: str = "",
    action: str = "",
    result: str = "",
    tags: str = "",
) -> int:
    v = _clean(
        title=title,
        employer=employer,
        year=year,
        situation=situation,
        task=task,
        action=action,
        result=result,
        tags=tags,
    )
    now = _now()
    cur = conn.execute(
        "INSERT INTO stories (title, employer, year, situation, task, action, result, tags,"
        " version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (
            v["title"],
            v["employer"],
            v["year"],
            v["situation"],
            v["task"],
            v["action"],
            v["result"],
            v["tags"],
            now,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def get_story(conn: sqlite3.Connection, story_id: int) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM stories WHERE id=?", (story_id,)
    ).fetchone()
    return row


def list_stories(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Newest edit first; the id breaks a tie so the order never depends on insertion luck."""
    return conn.execute("SELECT * FROM stories ORDER BY updated_at DESC, id DESC").fetchall()


def _stale_or_missing(conn: sqlite3.Connection, story_id: int) -> Exception:
    return StaleStory() if get_story(conn, story_id) else StoryNotFound(story_id)


def update_story(
    conn: sqlite3.Connection,
    story_id: int,
    version: int,
    *,
    title: str,
    employer: str = "",
    year: str = "",
    situation: str = "",
    task: str = "",
    action: str = "",
    result: str = "",
    tags: str = "",
) -> None:
    """Save an edit made from `version`. StaleStory leaves the stored text untouched."""
    v = _clean(
        title=title,
        employer=employer,
        year=year,
        situation=situation,
        task=task,
        action=action,
        result=result,
        tags=tags,
    )
    cur = conn.execute(
        "UPDATE stories SET title=?, employer=?, year=?, situation=?, task=?, action=?,"
        " result=?, tags=?, version=version+1, updated_at=? WHERE id=? AND version=?",
        (
            v["title"],
            v["employer"],
            v["year"],
            v["situation"],
            v["task"],
            v["action"],
            v["result"],
            v["tags"],
            _now(),
            story_id,
            version,
        ),
    )
    conn.commit()
    if cur.rowcount != 1:
        raise _stale_or_missing(conn, story_id)


def delete_story(conn: sqlite3.Connection, story_id: int, version: int) -> None:
    cur = conn.execute("DELETE FROM stories WHERE id=? AND version=?", (story_id, version))
    conn.commit()
    if cur.rowcount != 1:
        raise _stale_or_missing(conn, story_id)


def unverified_figures(story: Mapping[str, object], work_history: str) -> list[str]:
    """Figures in the story's text that are not whole figures in the work history."""
    parts = ("title", *STAR_FIELDS)
    text = "\n".join(str(story[p] or "") for p in parts if p in story)
    return _unverified(text, work_history)

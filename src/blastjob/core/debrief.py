"""The interview debrief: figures computed from one parsed transcript, and their storage (#84).

Everything is deterministic and local: no model call, no network. Word share is the primary
talk-share figure; time figures are labelled estimates (see `SILENCE_CAP_S`). Only the
owner's own words are ever kept (a short excerpt per long answer, per closing question and
per number prompt); the interviewer's words and the transcript itself are not stored.
Number mismatches are review prompts, never verdicts. See docs/how-it-works/debrief.md and
docs/truth-guarantees.md.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from blastjob.core.number_tokens import NUMERIC, figure_keys, normalize_number
from blastjob.core.transcript import MAX_NAME_CHARS, Transcript, Turn

# Silence credited to a speaker when the format has no cue end times.
SILENCE_CAP_S = 90
CLOSING_FRACTION = 0.15
LONGEST_SHOWN = 3
EXCERPT_CHARS = 160
CLOSING_EXCERPTS = 3
MAX_NUMBER_PROMPTS = 10
YEAR_RANGE = range(1990, 2040)
MAX_LABEL_CHARS = 60
INTERROGATIVES = frozenset(
    "what how why who when where which can could would should is are do does did will".split()
)
_SENTENCE_BREAK = re.compile(r"(?<=[.?!])\s+|\n+")
_FIRST_WORD = re.compile(r"[A-Za-z']+")
_YEAR = re.compile(r"\d{4}")
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_MINUTE = 60
_PERCENT = 100


@dataclass(frozen=True)
class Answer:
    start: float
    words: int
    seconds: float | None
    excerpt: str


@dataclass(frozen=True)
class NumberPrompt:
    token: str
    at: float


@dataclass(frozen=True)
class Debrief:
    owner_label: str
    duration_s: float
    time_basis: str  # "cue" (exact cue times) or "estimate" (start to next start, capped)
    owner_words: int
    other_words: int
    talk_share_words: float
    owner_seconds: float
    other_seconds: float
    talk_share_time: float | None
    longest: tuple[Answer, ...]
    closing_questions: int
    closing_excerpts: tuple[str, ...]
    numbers_status: str  # "checked" or "unavailable"
    numbers: tuple[NumberPrompt, ...]


def fmt_clock(seconds: float) -> str:
    whole = int(seconds)
    h, rem = divmod(whole, _SECONDS_PER_HOUR)
    m, s = divmod(rem, _SECONDS_PER_MINUTE)
    return f"{h}:{m:02d}:{s:02d}"


def _words(text: str) -> int:
    return len(text.split())


def _credits(turns: tuple[Turn, ...]) -> list[float | None]:
    """Seconds each turn is credited with, or None where it cannot be known (the last turn
    of a format with no end times). Cue ends are clipped to the next start; otherwise
    start-to-next-start, with silence capped."""
    out: list[float | None] = []
    for i, turn in enumerate(turns):
        nxt = turns[i + 1].start if i + 1 < len(turns) else None
        if turn.end is not None:
            end = turn.end if nxt is None else min(turn.end, nxt)
            out.append(max(0.0, end - turn.start))
        elif nxt is not None:
            out.append(min(nxt - turn.start, float(SILENCE_CAP_S)))
        else:
            out.append(None)
    return out


def _excerpt(text: str) -> str:
    return " ".join(text.split())[:EXCERPT_CHARS]


def _answers(turns: tuple[Turn, ...], credits: list[float | None], owner: str) -> list[Answer]:
    """The owner's answers: consecutive owner turns merged into one."""
    merged: list[tuple[float, list[str], list[float | None]]] = []
    previous: str | None = None
    for turn, credit in zip(turns, credits, strict=True):
        if turn.speaker == owner and previous == owner:
            merged[-1][1].append(turn.text)
            merged[-1][2].append(credit)
        elif turn.speaker == owner:
            merged.append((turn.start, [turn.text], [credit]))
        previous = turn.speaker
    out = []
    for start, texts, creds in merged:
        known = [c for c in creds if c is not None]
        joined = " ".join(texts)
        out.append(Answer(start, _words(joined), sum(known) if known else None, _excerpt(joined)))
    return out


def _question_like(sentence: str) -> bool:
    if sentence.endswith("?"):
        return True
    first = _FIRST_WORD.match(sentence)
    return first is not None and first.group(0).lower() in INTERROGATIVES


def _closing(turns: tuple[Turn, ...], owner: str, window_start: float) -> tuple[int, list[str]]:
    lines = [
        s.strip()
        for t in turns
        if t.speaker == owner and t.start >= window_start
        for s in _SENTENCE_BREAK.split(t.text)
        if s.strip()
    ]
    asked = [s for s in lines if _question_like(s)]
    return len(asked), [s[:EXCERPT_CHARS] for s in asked[:CLOSING_EXCERPTS]]


def _numbers(turns: tuple[Turn, ...], owner: str, trusted: str | None) -> list[NumberPrompt]:
    known = figure_keys(trusted or "")
    seen: set[str] = set()
    out: list[NumberPrompt] = []
    for turn in turns:
        if turn.speaker != owner:
            continue
        for match in NUMERIC.finditer(turn.text):
            token = match.group(0)
            key = normalize_number(token)
            is_year = _YEAR.fullmatch(token) is not None and int(token) in YEAR_RANGE
            if is_year or key in known or key in seen:
                continue
            seen.add(key)
            out.append(NumberPrompt(token, turn.start))
            if len(out) == MAX_NUMBER_PROMPTS:
                return out
    return out


def compute_debrief(transcript: Transcript, owner: str, trusted_text: str | None) -> Debrief:
    """Figures for `owner`'s side of `transcript`. `trusted_text` is the work history; None or
    blank means the numbers check could not run, which is reported, never guessed."""
    if owner not in transcript.speakers:
        raise ValueError(f"speaker {owner!r} is not in this transcript")
    turns = transcript.turns
    credits = _credits(turns)
    owner_words = sum(_words(t.text) for t in turns if t.speaker == owner)
    other_words = sum(_words(t.text) for t in turns if t.speaker != owner)
    owner_s = sum(c or 0.0 for t, c in zip(turns, credits, strict=True) if t.speaker == owner)
    other_s = sum(c or 0.0 for t, c in zip(turns, credits, strict=True) if t.speaker != owner)
    total_words = owner_words + other_words
    duration = transcript.endpoint - transcript.start
    answers = sorted(_answers(turns, credits, owner), key=lambda a: (-a.words, a.start))
    asked, excerpts = _closing(turns, owner, transcript.start + (1 - CLOSING_FRACTION) * duration)
    available = trusted_text is not None and bool(trusted_text.strip())
    return Debrief(
        owner_label=owner,
        duration_s=duration,
        time_basis="cue" if all(t.end is not None for t in turns) else "estimate",
        owner_words=owner_words,
        other_words=other_words,
        talk_share_words=owner_words / total_words * _PERCENT if total_words else 0.0,
        owner_seconds=owner_s,
        other_seconds=other_s,
        talk_share_time=owner_s / (owner_s + other_s) * _PERCENT if owner_s + other_s else None,
        longest=tuple(answers[:LONGEST_SHOWN]),
        closing_questions=asked,
        closing_excerpts=tuple(excerpts),
        numbers_status="checked" if available else "unavailable",
        numbers=tuple(_numbers(turns, owner, trusted_text)) if available else (),
    )


# ---------------------------------------------------------------------------- storage


def insert_debrief(
    conn: sqlite3.Connection,
    job_id: int,
    ingest_id: str,
    label: str,
    d: Debrief,
    *,
    now: datetime | None = None,
) -> int:
    """Store the figures. `ingest_id` is unique, so a repeated submit cannot add a second row
    (sqlite3.IntegrityError). Holds no transcript text and no file name."""
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M")
    cur = conn.execute(
        "INSERT INTO interview_debriefs (job_id, ingest_id, created_at, label, owner_label,"
        " duration_s, time_basis, owner_words, other_words, talk_share_words, owner_seconds,"
        " other_seconds, talk_share_time, longest_json, closing_questions, closing_json,"
        " numbers_status, numbers_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            job_id,
            ingest_id,
            stamp,
            label.strip()[:MAX_LABEL_CHARS],
            d.owner_label[:MAX_NAME_CHARS],
            d.duration_s,
            d.time_basis,
            d.owner_words,
            d.other_words,
            d.talk_share_words,
            d.owner_seconds,
            d.other_seconds,
            d.talk_share_time,
            json.dumps(
                [
                    {"start": a.start, "words": a.words, "seconds": a.seconds, "excerpt": a.excerpt}
                    for a in d.longest
                ]
            ),
            d.closing_questions,
            json.dumps(list(d.closing_excerpts)),
            d.numbers_status,
            json.dumps([{"token": n.token, "at": n.at} for n in d.numbers]),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _loads(raw: str | None) -> list[dict]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def list_debriefs(conn: sqlite3.Connection, job_id: int) -> list[dict]:
    """A job's debriefs, newest first, with the JSON columns decoded for display."""
    rows = conn.execute(
        "SELECT * FROM interview_debriefs WHERE job_id=? ORDER BY id DESC", (job_id,)
    ).fetchall()
    return [
        {
            **dict(r),
            "longest": _loads(r["longest_json"]),
            "closing": _loads(r["closing_json"]),
            "number_prompts": _loads(r["numbers_json"]),
        }
        for r in rows
    ]


def delete_debrief(conn: sqlite3.Connection, job_id: int, debrief_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM interview_debriefs WHERE id=? AND job_id=?", (debrief_id, job_id)
    )
    conn.commit()
    return cur.rowcount > 0

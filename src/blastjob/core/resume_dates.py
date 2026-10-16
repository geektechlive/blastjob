"""Deterministic employment-date verification for a generated resume.

Why this exists: on 2026-09-10 a generated resume carried seven employment date
ranges and six were invented. The work history had dates for exactly one employer
at the time, so the model filled the gap with a plausible-looking timeline. Two
four-month roles became two-year roles. The LLM groundedness pass scored that
resume 62/100 and flagged thirteen prose claims without noticing a single wrong
date -- it audits adjectives and ignores the one thing on a resume that is
trivially falsifiable against a public LinkedIn profile.

So dates are checked here instead: parsed out of both documents and compared,
with no model in the loop. A date that disagrees with the work history is a
fabrication, and fabricated dates get offers rescinded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from blastjob.core.company import company_key

_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}

# "Jan 2025", "January 2025", "2025", "Present"/"Current"/"Today".
# Named groups are used only when parsing ONE point; the range pattern needs a
# non-capturing twin, since a name cannot appear twice in one expression.
_POINT = r"(?:(?P<mon>[A-Za-z]{3,9})\.?\s+)?(?P<year>(?:19|20)\d{2})"
_POINT_NC = r"(?:[A-Za-z]{3,9}\.?\s+)?(?:19|20)\d{2}"
_PRESENT = r"(?:present|current|today|now)"
# An en/em dash, a hyphen, or the word "to", with optional spaces on either side.
_SEP = r"\s*(?:[\u2013\u2014-]|\bto\b)\s*"
_RANGE_RE = re.compile(
    rf"(?P<start>{_POINT_NC}){_SEP}(?P<end>{_POINT_NC}|{_PRESENT})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DatePoint:
    """One end of a range. `month` is None when only a year was written."""

    year: int
    month: int | None = None
    present: bool = False

    def __str__(self) -> str:
        if self.present:
            return "Present"
        if self.month:
            name = [k for k, v in _MONTHS.items() if v == self.month][0].capitalize()
            return f"{name} {self.year}"
        return str(self.year)

    @property
    def sort_key(self) -> tuple[int, int]:
        """Present sorts after every real date, so it always wins as an end."""
        return (9999, 13) if self.present else (self.year, self.month or 0)

    def agrees(self, other: DatePoint) -> bool:
        if self.present or other.present:
            return self.present and other.present
        if self.year != other.year:
            return False
        if self.month is None or other.month is None:
            return True
        return self.month == other.month


@dataclass(frozen=True)
class DateRange:
    start: DatePoint
    end: DatePoint

    def __str__(self) -> str:
        return f"{self.start} – {self.end}"

    def agrees_with(self, other: DateRange) -> bool:
        """True when this range does not contradict `other`.

        Comparison is at the precision the RESUME used: a resume saying "2006"
        is satisfied by a history saying "Mar 2006", because the year is what it
        claimed. A resume saying "Mar 2006" against a history of "Jul 2006" is a
        contradiction. This deliberately forgives vagueness and catches conflict.
        """
        return self.start.agrees(other.start) and self.end.agrees(other.end)


def _point(raw: str) -> DatePoint | None:
    text = raw.strip()
    if re.fullmatch(_PRESENT, text, re.IGNORECASE):
        return DatePoint(year=0, present=True)
    m = re.fullmatch(_POINT, text, re.IGNORECASE)
    if not m:
        return None
    mon = m.group("mon")
    month = _MONTHS.get(mon[:3].lower()) if mon else None
    if mon and month is None:
        return None  # a word that isn't a month -- not a date
    return DatePoint(year=int(m.group("year")), month=month)


def parse_range(text: str) -> DateRange | None:
    """The full employment span described by `text`, or None.

    A single heading can carry more than one range when the user was promoted
    inside one employer -- "Director of Technology Operations (Nov 2023 - Dec
    2026) / Infrastructure Lead (Jan 2020 - Oct 2023)". The tenure there is
    Jan 2020 to Dec 2026, so the span is earliest start to latest end. Reading
    only the first range reported the resume as wrong when the resume was
    right.
    """
    ranges: list[DateRange] = []
    for m in _RANGE_RE.finditer(text):
        start, end = _point(m.group("start")), _point(m.group("end"))
        if start is not None and end is not None:
            ranges.append(DateRange(start=start, end=end))
    if not ranges:
        return None
    return DateRange(
        start=min((r.start for r in ranges), key=lambda p: p.sort_key),
        end=max((r.end for r in ranges), key=lambda p: p.sort_key),
    )


def _date_line(line: str | None) -> str:
    """`line` when it is a bare date range (`*Jan 2020 - Dec 2023*`), else "".

    Ingest (`core/ingest._serialize_history`) writes a role's dates on the line
    AFTER its `### Title | Company` heading, in italics. Only a line that is
    nothing but a range counts, so a bullet that happens to mention two years
    ("- Migrated between 2001 - 2003") is never read as the role's dates."""
    if line is None or re.match(r"\s*(?:[-+\u2022#]|\*\s)", line):
        return ""
    inner = line.strip().strip("*_ ").strip()
    return inner if inner and _RANGE_RE.fullmatch(inner) else ""


def history_headings(work_history_md: str) -> list[tuple[list[str], str]]:
    """Every `### ` heading in the work history as (`|`-split parts, date text).

    The date text is the heading itself plus, when the very next line is a bare
    date range, that line: a heading with its dates in parentheses and an ingested
    one with the dates underneath both parse. The stored history format is not
    rewritten; the readers adapt to it."""
    lines = work_history_md.splitlines()
    out: list[tuple[list[str], str]] = []
    for i, line in enumerate(lines):
        if not line.startswith("### "):
            continue
        parts = [p.strip() for p in line[4:].split("|")]
        following = lines[i + 1] if i + 1 < len(lines) else None
        # Joined with a pipe: "Sample Corp 2016 - Present" would read "Corp 2016"
        # as a (non-month) month-year start and parse nothing.
        dates = _date_line(following)
        out.append((parts, f"{line} | {dates}" if dates else line))
    return out


def employer_names(work_history_md: str) -> list[str]:
    """Employer names from the work history's `### Title | Company | City` headings."""
    out: list[str] = []
    for line in work_history_md.splitlines():
        if not line.startswith("### "):
            continue
        parts = [p.strip() for p in line[4:].split("|")]
        if len(parts) >= 2 and parts[1]:
            name = re.sub(r"\s*\([^)]*\)\s*$", "", parts[1]).strip()
            if name and name not in out:
                out.append(name)
    return out


def history_dates(work_history_md: str) -> dict[str, DateRange]:
    """company_key -> the authoritative span for that employer, unioned across
    every heading that names it.

    One employer can hold SEVERAL headings: a promotion is stored as two entries
    (Contoso Health Solutions is Infrastructure Lead 2020-2023 then Director of
    Technology Operations 2023-2026, split on 2026-09-16 so each title keeps its own
    dates, which is what Workday's parsing guidance asks for). Keeping only the
    first heading made the truth range narrower than the truth: a correct resume
    spanning Jan 2020 - Dec 2026 read as contradicting a work history that
    "said" Nov 2023 - Dec 2026, so every future packet would have carried a
    fabricated-dates finding. `verify` already unions the resume side and its
    docstring claimed this side did too; now it does.
    """
    ranges: dict[str, list[DateRange]] = {}
    for parts, date_text in history_headings(work_history_md):
        if len(parts) < 2 or not parts[1]:
            continue
        name = re.sub(r"\s*\([^)]*\)\s*$", "", parts[1]).strip()
        rng = parse_range(date_text)
        if name and rng:
            ranges.setdefault(company_key(name), []).append(rng)
    return {
        key: DateRange(
            start=min((r.start for r in rs), key=lambda p: p.sort_key),
            end=max((r.end for r in rs), key=lambda p: p.sort_key),
        )
        for key, rs in ranges.items()
    }


@dataclass(frozen=True)
class DateFinding:
    """One employer whose resume dates disagree with the work history."""

    employer: str
    claimed: str
    actual: str

    @property
    def message(self) -> str:
        return (
            f"Employment dates for {self.employer}: resume says {self.claimed}, "
            f"work history says {self.actual}"
        )


def verify(resume_md: str, work_history_md: str) -> tuple[list[DateFinding], list[str]]:
    """Compare every dated employer line in the resume against the work history.

    An employer can legitimately appear on SEVERAL resume lines -- a promotion is
    best written as two entries, which is also what an ATS expects -- so the
    ranges for one employer are unioned into a span before comparing, exactly as
    the work-history side does. Comparing only the first line reported a
    correctly split promotion as a contradiction.

    Returns (contradictions, undated_employers). A contradiction is a
    fabrication and counts against groundedness. An undated employer is an
    omission: worth surfacing, but not a false statement, so it is reported
    separately rather than scored as one.
    """
    truth = history_dates(work_history_md)
    names = employer_names(work_history_md)
    by_length = sorted(names, key=len, reverse=True)

    seen: set[str] = set()
    found: dict[str, list[DateRange]] = {}
    label: dict[str, str] = {}

    for line in resume_md.splitlines():
        if not line.strip():
            continue
        # Longest name first so "Contoso Health Solutions" wins over a bare "Contoso Health".
        matched = next((n for n in by_length if n.lower() in line.lower()), None)
        if matched is None:
            continue
        key = company_key(matched)
        seen.add(key)
        label.setdefault(key, matched)
        rng = parse_range(line)
        if rng is not None:
            found.setdefault(key, []).append(rng)

    findings: list[DateFinding] = []
    for key, ranges in found.items():
        actual = truth.get(key)
        if actual is None:
            continue
        span = DateRange(
            start=min((r.start for r in ranges), key=lambda p: p.sort_key),
            end=max((r.end for r in ranges), key=lambda p: p.sort_key),
        )
        if not span.agrees_with(actual):
            findings.append(DateFinding(employer=label[key], claimed=str(span), actual=str(actual)))

    undated = [n for n in names if company_key(n) in seen - set(found)]
    return findings, undated

"""What a bullet claims about time or growth: the regexes behind the timeline checks.

All deterministic. A pattern here is a risk signal, never proof: the findings
built on it are MAJOR or MINOR and never change whether a packet is sendable.
See docs/how-it-works/tailoring.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from blastjob.core.resume_dates import DatePoint

# ASCII-only folding: under Unicode IGNORECASE a long s or a dotless i matches a number
# or month word and then misses the lookup tables.
_FLAGS = re.IGNORECASE | re.ASCII
# Every bullet is cut to this before any pattern runs; resumes do not have longer ones.
MAX_BULLET_CHARS = 2000
# Context windows for the look-behind and look-ahead checks around a match.
_WINDOW_BEFORE = 100
_WINDOW_AFTER = 40
_NUMBER_WORDS = (
    "one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty"
).split()
_WORD_VALUE = {w: i for i, w in enumerate(_NUMBER_WORDS, 1)}
_NUM = rf"(?:(?<![\d.])\d{{1,2}}(?![\d]|\.\d)|{'|'.join(_NUMBER_WORDS)})"
_UNIT = r"(?P<unit>year|month)s?"
_PLAN_WORDS = r"roadmap|plan|strategy|budget|forecast|target|goal|vision"
_PLAN_NOUNS = r"roadmaps?|plans?|strateg(?:y|ies)|budgets?|forecasts?|targets?|goals?|visions?"
_NON_ELAPSED = (
    rf"{_PLAN_NOUNS}|contracts?|agreements?|leases?|warrant(?:y|ies)|horizons?|terms?|old"
)
# Up to three words may sit between "N-year" and the noun ("three-year practice plans").
_MAX_WORDS_BEFORE_NOUN = 3
_MONTHS_PER_YEAR = 12

# "within five years", "over my 9 years at", "in 6 months".
_ELAPSED_RE = re.compile(
    rf"\b(?:within|over|in)\s+(?:the\s+|my\s+)?(?:(?:first|next|last|past)\s+)?"
    rf"(?P<n>{_NUM})[\s-]+{_UNIT}\b",
    _FLAGS,
)
# "a 5-year migration" (not "a 5-year plan" or "a 10-year-old system").
_HYPHEN_RE = re.compile(
    rf"(?<![\w.])(?P<n>{_NUM})-{_UNIT}\b"
    rf"(?!\s{{0,4}}-?\s{{0,4}}(?:\w+\s{{1,4}}){{0,{_MAX_WORDS_BEFORE_NOUN}}}(?:{_NON_ELAPSED})\b)",
    _FLAGS,
)
# "5 years of savings" (not "15 years of experience", which describes other people).
_YEARS_OF_RE = re.compile(
    rf"(?<![\w.])(?P<n>{_NUM})\s+{_UNIT}\s+of\b(?!\s+(?:experience|expertise|history|tenure))",
    _FLAGS,
)
# "3- and 5-year", "3 to 5 years", "3-5 years": a range or a plan pair, not one span.
_RANGE_BEFORE_RE = re.compile(
    rf"(?:\d|{'|'.join(_NUMBER_WORDS)})-?\s*(?:-|–|and|to|or|&)\s*$", _FLAGS
)
_SPAN_BOTH_RE = re.compile(r"\bacross\s+both\s+(?:roles|positions|titles)\b", _FLAGS)
_SPAN_CHAIN_RE = re.compile(
    r"\bacross\s+(?:my\s+|all\s+)?(?:roles|positions|titles)\b"
    r"|\bacross\s+my\s+(?:\w+\s+)?years\b"
    r"|\bover\s+my\s+(?:\w+\s+)?years\s+at\b"
    r"|\bover\s+my\s+time\s+at\b"
    r"|\bthroughout\s+my\s+(?:time|tenure)\s+at\b",
    _FLAGS,
)

_MONTH_NAMES = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_MONTH_WORD = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_YEAR_POINT_RE = re.compile(
    rf"(?<![\w$#.,/])(?:(?P<mon>{_MONTH_WORD})\.?\s+)?"
    r"(?P<year>(?:19|20)\d{2})(?![\w%]|,\d)",
    _FLAGS,
)
_PLAN_BEFORE_RE = re.compile(rf"\b(?:{_PLAN_WORDS})s?\b[^.;]{{0,60}}$", _FLAGS)
_PLAN_AFTER_RE = re.compile(rf"^\s*(?:{_PLAN_WORDS}|roadmap)", _FLAGS)
_PLAN_ANYWHERE_RE = re.compile(rf"\b(?:{_PLAN_WORDS})s?\b", _FLAGS)
_BY_BEFORE_RE = re.compile(r"\bby\s+$", _FLAGS)
_VERSION_BEFORE_RE = re.compile(
    r"\b(?:windows|server|office|exchange|sql|sharepoint|excel|visual studio|oracle|azure"
    r"|autocad|access|project|iso|version|release|v)\s+(?:server\s+)?$",
    _FLAGS,
)

_AMOUNT = r"\$\s?\d[\d,]*(?:\.\d+)?\s?(?:K|M|B|thousand|million|billion)?"
_VALUE = (
    rf"(?:{_AMOUNT}|\d[\d,]*(?:\.\d+)?%?|{'|'.join(_NUMBER_WORDS)}|a\s+single|single|an?)(?!\w)"
)
_GROWTH_RE = re.compile(rf"\bfrom\s+{_VALUE}\s+(?:\w+\s+){{0,3}}to\s+{_VALUE}", _FLAGS)
_AMOUNT_RE = re.compile(_AMOUNT, _FLAGS)
_REVENUE_RE = re.compile(r"new\s+(?:business|revenue)|revenue", _FLAGS)


@dataclass(frozen=True)
class StatedDuration:
    """An elapsed span a bullet claims, in months, with the text that said so."""

    months: int
    text: str


@dataclass(frozen=True)
class DatedClaim:
    """A year (and month, when given) a bullet cites as a result."""

    point: DatePoint
    text: str


def _to_int(raw: str) -> int:
    return int(raw) if raw.isdigit() else _WORD_VALUE[raw.lower()]


def stated_durations(bullet: str) -> list[StatedDuration]:
    """Elapsed-time claims in `bullet`; plan horizons, ranges and fractions are skipped."""
    bullet = bullet[:MAX_BULLET_CHARS]
    found: dict[int, StatedDuration] = {}
    for pattern in (_ELAPSED_RE, _HYPHEN_RE, _YEARS_OF_RE):
        for m in pattern.finditer(bullet):
            start = m.start("n")
            if start in found or _RANGE_BEFORE_RE.search(
                bullet[max(0, start - _WINDOW_BEFORE) : start]
            ):
                continue
            n = _to_int(m.group("n"))
            per = _MONTHS_PER_YEAR if m.group("unit").lower() == "year" else 1
            found[start] = StatedDuration(months=n * per, text=m.group(0).strip())
    return sorted(found.values(), key=lambda d: -d.months)


def spanning_wording(bullet: str) -> str:
    """ "chain" (the whole same-employer run), "both" (this role plus one neighbour) or ""."""
    bullet = bullet[:MAX_BULLET_CHARS]
    if _SPAN_CHAIN_RE.search(bullet):
        return "chain"
    if _SPAN_BOTH_RE.search(bullet):
        return "both"
    return ""


def dated_claims(bullet: str, today: date) -> list[DatedClaim]:
    """Years the bullet cites as results; plan, version and future years are exempt."""
    bullet = bullet[:MAX_BULLET_CHARS]
    has_plan_word = bool(_PLAN_ANYWHERE_RE.search(bullet))
    out: list[DatedClaim] = []
    for m in _YEAR_POINT_RE.finditer(bullet):
        year = int(m.group("year"))
        before = bullet[max(0, m.start() - _WINDOW_BEFORE) : m.start()]
        after = bullet[m.end() : m.end() + _WINDOW_AFTER]
        exempt = (
            year > today.year
            or _PLAN_BEFORE_RE.search(before)
            or _PLAN_AFTER_RE.search(after)
            or _VERSION_BEFORE_RE.search(before)
            or (_BY_BEFORE_RE.search(before) and has_plan_word)
        )
        if exempt:
            continue
        mon = m.group("mon")
        month = _MONTH_NAMES.index(mon[:3].lower()) + 1 if mon else None
        out.append(DatedClaim(point=DatePoint(year=year, month=month), text=m.group(0)))
    return out


def has_growth_figure(bullet: str) -> bool:
    """A "from X to Y" growth phrase, or a currency figure beside new business or revenue."""
    bullet = bullet[:MAX_BULLET_CHARS]
    if _GROWTH_RE.search(bullet):
        return True
    return bool(_AMOUNT_RE.search(bullet) and _REVENUE_RE.search(bullet))

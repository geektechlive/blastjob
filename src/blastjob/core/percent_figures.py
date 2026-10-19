"""One definition of a percentage figure, shared by the arithmetic check
(`packet_gate`) and the prep-pack `[confirm]` marking (`prep`).

A model (or the work history) may write the same figure as `30%`, `30 %`,
`30 percent`, `30 per cent`, `30 pct` or `thirty percent`. Every form
normalizes to ONE key (`30%`) on both sides of a comparison, so a reformatted
true figure is verified and an invented one is flagged whichever way it is
spelled. The comparison stays whole-token and exact: `3 percent` is never
verified by `30%` or `13%`.

Spelled-out numbers cover one through ninety-nine plus `a hundred` / `one
hundred`; anything else ("thirty-something") simply does not match.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_UNITS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_HUNDRED = r"(?:a|one)\s+hundred"
_TENS_RE = "|".join(_TENS)
_UNITS_RE = "|".join(sorted(_UNITS, key=len, reverse=True))
_WORD_NUMBER = rf"(?i:{_HUNDRED}|(?:{_TENS_RE})(?:[-\s](?:{_UNITS_RE}))?|{_UNITS_RE})"
_SUFFIX = r"(?:\s?%|\s?(?i:percent|per\s?cent|pct)\b)"

# Digit forms first, then spelled-out; the callers add their own prefix and
# suffix guards (a match must not start or end inside a larger number or word).
PERCENT = rf"(?:\d+(?:\.\d+)?|{_WORD_NUMBER}){_SUFFIX}"

_PERCENT_RE = re.compile(rf"^{PERCENT}$")
_NUMBER_PART = re.compile(r"^(.*?)\s?(?:%|(?i:percent|per\s?cent|pct))$", re.DOTALL)


def _word_value(words: str) -> int | None:
    text = words.strip().lower()
    if text.endswith("hundred"):
        return 100
    total = 0
    for part in re.split(r"[-\s]+", text):
        if part in _TENS:
            total += _TENS[part]
        elif part in _UNITS:
            total += _UNITS[part]
        else:
            return None
    return total


def percent_key(token: str) -> str | None:
    """The one comparable key for a percentage token ('30%'), or None when
    `token` is not a percentage in any recognized form."""
    text = token.strip()
    if not _PERCENT_RE.match(text):
        return None
    head = _NUMBER_PART.match(text)
    if head is None:
        return None
    number = head.group(1).strip()
    try:
        value: Decimal | int | None = Decimal(number)
    except InvalidOperation:
        value = _word_value(number)
    if value is None:
        return None
    return f"{Decimal(value).normalize():f}%"

"""Single values (founded, hq, employees, revenue...) checked against the page
they cite (issue #59).

A model filling a company profile from fetched pages can still write a number
it remembers, or the right number from the wrong page. A value is kept only if
it appears, as a whole figure, in the text of the page it cites; otherwise it
is dropped (None / ""), never guessed. Used by both the packet research
(`core/packet_research.py`) and the cockpit's Research button
(`web/research.py`).

Figures are compared the way `core/packet_gate.check_arithmetic` compares them,
with the same normalizer: notation-insensitive, magnitude-exact, whole-token,
never substring. `$1.8B`, `$1.8 billion` and `$1,800,000,000` are one figure;
`$1,200` is not verified by `$1,200,000`, nor `3%` by `13%`, nor `36` by `360`.

Bare counts (headcount, plants, a founding year) need one shape the shared
patterns do not cover: `prep._NUMERIC` only matches bare numbers of four or more
digits (or comma-grouped), so "450 employees" or "36 locations" would never
verify. `_BARE` below adds plain integers with the same prefix/suffix guards,
and a bare figure inside a money or percent token never counts as a bare one
(`23%` does not verify a headcount of 23).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from blastjob.core.packet_gate import _FIGURE, _SCALE, _normalize
from blastjob.core.percent_figures import percent_key

MONEY_FIELDS = ("revenue_usd", "total_assets_usd", "net_income_usd")
PERCENT_FIELDS = ("revenue_growth_pct",)
COUNT_FIELDS = ("founded", "employees", "customers", "locations", "culture_rating")
PHRASE_FIELDS = ("hq", "ceo")
SINGLE_VALUE_FIELDS = (*MONEY_FIELDS, *PERCENT_FIELDS, *COUNT_FIELDS, *PHRASE_FIELDS)
INT_FIELDS = (*MONEY_FIELDS, "founded", "employees", "customers", "locations")
FLOAT_FIELDS = ("revenue_growth_pct", "culture_rating")

# A bare number: comma-grouped or plain digits, an optional decimal part and an
# optional scale ("1.2 million"). Same guards as the shared figure patterns.
_BARE = re.compile(
    rf"(?<![\w.,$])(?:\d{{1,3}}(?:,\d{{3}})+|\d+)(?:\.\d+)?(?:\s?{_SCALE})?(?!\w|[.,]\d)"
)
_BARE_NS = "#"
_YEAR_NS = "Y"
# A plain four-digit number in this range reads as a year, never a count: "founded
# in 1951" must not verify "employs 1,951", nor "employs 2,000" a founding in 2000.
# The cost: a headcount written "2000 people" (no comma) does not verify 2000.
_YEAR_SHAPE = re.compile(r"(?:1[89]\d\d|20\d\d)")


def _bare_key(token: str) -> str:
    """The money normalizer's magnitude, in a separate namespace: a headcount
    of 2000 is never verified by "$2,000". A plain year-shaped token gets the
    year namespace."""
    if _YEAR_SHAPE.fullmatch(token):
        return _YEAR_NS + token
    return _BARE_NS + _normalize("$" + token)[1:]


def _count_key(value: str) -> str:
    """A count VALUE's key: always the count namespace, never the year one."""
    return _BARE_NS + _normalize("$" + value)[1:]


def year_cited(value: Any, text: str) -> bool:
    """The year's digits literally on the page ("founded in 1951"), never a
    comma-grouped or decimal number that contains them ("1,951", "11951")."""
    year = coerce_int(value)
    if year is None or not _YEAR_SHAPE.fullmatch(str(year)):
        return False
    return re.search(rf"(?<![\d,.]){year}(?!\d|[,.]\d)", text) is not None


def figure_keys(text: str) -> frozenset[str]:
    """Every figure in `text` as a comparable key: money (`$...`), percent
    (`N%`) and bare numbers (`#...`). A bare number inside a money or percent
    token is not also read as a bare one."""
    keys: set[str] = set()
    taken: list[tuple[int, int]] = []
    for m in _FIGURE.finditer(text):
        keys.add(_normalize(m.group(0)))
        taken.append(m.span())
    for m in _BARE.finditer(text):
        if any(a < m.end() and m.start() < b for a, b in taken):
            continue
        keys.add(_bare_key(m.group(0)))
    return frozenset(keys)


def figures_supported(statement: str, span: str) -> bool:
    """True when every figure in `statement` is also a figure in `span`.

    A fact's statement is the model's compression of its span and is never
    trusted on its own (see `research_facts.CompanyFact`); this stops it from
    carrying a number the span does not state into the cover-letter prompt.
    Spelled-out numbers ("two thousand") are not figures here and are not checked.
    """
    return figure_keys(statement) <= figure_keys(span)


def coerce_int(v: Any) -> int | None:
    """A model's number as an int: "1,072" and "$478000000" are fine, junk is None."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        return int(float(str(v).replace(",", "").replace("$", "")))
    except ValueError:
        return None


def coerce_float(v: Any) -> float | None:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except ValueError:
        return None


def _number_text(value: float) -> str:
    """`1800000000` -> "1800000000", `23.0` -> "23", `3.4` -> "3.4"."""
    return f"{value:f}".rstrip("0").rstrip(".")


def _phrase_cited(phrase: str, text: str) -> bool:
    words = phrase.split()
    if not words:
        return False
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + r"(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def value_cited(field: str, value: Any, text: str) -> bool:
    """Whether `value` for `field` appears, as a whole figure or phrase, in `text`.
    A field this module does not know is never cited."""
    if field in PHRASE_FIELDS:
        return isinstance(value, str) and _phrase_cited(value, text)
    if field == "founded":
        return year_cited(value, text)
    number = coerce_float(value)
    if number is None:
        return False
    keys = figure_keys(text)
    if field in MONEY_FIELDS:
        return _normalize(f"${_number_text(number)}") in keys
    if field in PERCENT_FIELDS:
        return percent_key(f"{_number_text(number)}%") in keys
    if field in COUNT_FIELDS:
        return _count_key(_number_text(number)) in keys
    return False


def _coerced(field: str, value: Any) -> Any:
    if field in PHRASE_FIELDS:
        return " ".join(value.split()) if isinstance(value, str) else ""
    if field in INT_FIELDS:
        return coerce_int(value)
    return coerce_float(value)


def _empty(field: str) -> Any:
    return "" if field in PHRASE_FIELDS else None


def keep_cited(
    values: Mapping[str, Any], cites: Mapping[str, Any], pages: Mapping[str, str]
) -> dict[str, Any]:
    """A new mapping of every single-value field in `values`: kept when the URL
    `cites[field]` is a page we read AND the value appears in that page's text,
    else emptied (None, or "" for a phrase). Fields outside
    `SINGLE_VALUE_FIELDS` are not returned; the caller leaves them alone.
    """
    out: dict[str, Any] = {}
    for field in SINGLE_VALUE_FIELDS:
        if field not in values:
            continue
        value = _coerced(field, values[field])
        url = cites.get(field)
        text = pages.get(url) if isinstance(url, str) else None
        if value in (None, "") or not text or not value_cited(field, value, text):
            out[field] = _empty(field)
        else:
            out[field] = value
    return out

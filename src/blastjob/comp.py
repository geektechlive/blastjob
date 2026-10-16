"""Compensation parsing shared by discovery adapters and the matcher.

Turns free-form pay strings ("$211.4K – $290.6K", "$180,000 to $220,000") into
integer dollar bounds. Used both for POSTED comp (from Ashby/Adzuna) and for
ESTIMATED comp inferred from a JD — provenance is decided by the caller, never
here, so verified and estimated figures never get conflated.
"""

from __future__ import annotations

import re

# Plausible annual compensation, in whole currency units. Below the floor a bare
# number is prose (a year, a headcount); above the ceiling it is an id or a typo.
# The scorer applies the same bounds to model-read comp (`sanitize_comp`).
MIN_PLAUSIBLE_ANNUAL_USD = 10_000
MAX_PLAUSIBLE_ANNUAL_USD = 5_000_000

# Currency markers. A marker counts only when it touches an amount: a symbol or code
# just before it ("NZ$150,000", "CAD 150,000") or a code just after ("150,000 CAD",
# "1,200,000 kr"). The same word elsewhere in the text ("our CAD office") says
# nothing about this figure. A bare "$" and no marker at all are both USD.
_PREFIX_MARKS: dict[str, str] = {
    "$": "USD",
    "US$": "USD",
    "CA$": "CAD",
    "C$": "CAD",
    "AU$": "AUD",
    "A$": "AUD",
    "NZ$": "NZD",
    "S$": "SGD",
    "HK$": "HKD",
    "R$": "BRL",
    "£": "GBP",
    "€": "EUR",
    "₹": "INR",
    # Swedish, Norwegian and Danish crowns all write "kr"; a bare one is none of
    # them for certain, but it is certainly not dollars.
    "kr": "KR",
}
_CODES: tuple[str, ...] = (
    "USD", "GBP", "EUR", "CAD", "AUD", "NZD", "SGD", "HKD", "BRL", "CHF", "INR", "SEK", "NOK",
    "DKK", "ZAR",
)  # fmt: skip
_CODE_ALT = "|".join(_CODES)
_PREFIX_ALT = "|".join(re.escape(k) for k in sorted(_PREFIX_MARKS, key=len, reverse=True))

# $211.4K, $290.6K, $180,000, 220000, $1.2M. The k/M suffix must touch the digits
# and must not run into a word: "$180,000 Max" is not "$180,000 M" (1.8e11), and
# "$150k+" still is $150k.
_AMOUNT = re.compile(
    rf"(?:(?<![A-Za-z])(?P<pre>{_PREFIX_ALT}|{_CODE_ALT})[\s:]*)?"
    r"(?P<num>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
    r"(?P<suffix>[kKmM](?![A-Za-z]))?"
    rf"(?:\s*(?P<post>{_CODE_ALT}|kr)\b)?",
)

# "From $150,000", "starting at $150,000", "minimum $150,000" ...
_MIN_BEFORE = re.compile(
    r"(?:\bfrom|\bstarting(?:\s+(?:at|from))?|\bstarts?\s+at|\bminimum(?:\s+of)?"
    r"|\bmin\.?|\bat\s+least)\W*$",
    re.I,
)
# ... "$150,000+", "$150K and up", "$150K or more".
_OPEN_AFTER = re.compile(r"(?:\+|\s*and\s+(?:up|above|over)\b|\s+or\s+more\b)", re.I)


def _to_int(num: str, suffix: str) -> int:
    n = float(num.replace(",", ""))
    if suffix in ("k", "K"):
        n *= 1_000
    elif suffix in ("m", "M"):
        n *= 1_000_000
    return int(round(n))


def _marker_currency(m: re.Match[str]) -> str | None:
    """The currency this amount's own marker names, or None when it has none."""
    for key in (m.group("pre"), m.group("post")):
        if key:
            return _PREFIX_MARKS.get(key, key)
    return None


def _currency_of(matches: list[re.Match[str]]) -> str:
    """First explicit non-USD marker among the amounts wins; else USD."""
    for m in matches:
        code = _marker_currency(m)
        if code is not None and code != "USD":
            return code
    return "USD"


def detect_currency(text: str) -> str:
    """The currency of the figures in `text`, from the markers beside them."""
    return _currency_of(list(_AMOUNT.finditer(text)))


def parse_salary_range(text: str) -> tuple[int, int, str]:
    """Return (min, max, currency). A single figure mirrors itself; an open-ended
    one ("From $150,000", "$150K+") is a MINIMUM only, so max is 0 = unknown and
    the gate cannot hard-reject on it. All-zero when nothing parses. Only
    recognizes plausible salary magnitudes ($10k-$5M) to avoid matching stray
    numbers in prose."""
    if not text:
        return 0, 0, "USD"

    found: list[tuple[int, re.Match[str]]] = []
    all_matches = list(_AMOUNT.finditer(text))
    for m in all_matches:
        val = _to_int(m.group("num"), m.group("suffix") or "")
        # A bare "220" with no k/M is only a salary if it's already large.
        if MIN_PLAUSIBLE_ANNUAL_USD <= val <= MAX_PLAUSIBLE_ANNUAL_USD:
            found.append((val, m))
    if not found:
        return 0, 0, _currency_of(all_matches)

    currency = _currency_of([m for _, m in found])
    amounts = [v for v, _ in found]
    if len(found) == 1 and _is_open_ended(text, found[0][1]):
        return amounts[0], 0, currency
    return min(amounts), max(amounts), currency


def _is_open_ended(text: str, m: re.Match[str]) -> bool:
    return bool(_MIN_BEFORE.search(text[: m.start()]) or _OPEN_AFTER.match(text, m.end()))

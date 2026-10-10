"""Compensation parsing shared by discovery adapters and the matcher.

Turns free-form pay strings ("$211.4K – $290.6K", "$180,000 to $220,000") into
integer dollar bounds. Used both for POSTED comp (from Ashby/Adzuna) and for
ESTIMATED comp inferred from a JD — provenance is decided by the caller, never
here, so verified and estimated figures never get conflated.
"""

from __future__ import annotations

import re

# $211.4K, $290.6K, $180,000, 220000, $1.2M
_AMOUNT = re.compile(
    r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kKmM])?",
)


def _to_int(num: str, suffix: str) -> int:
    n = float(num.replace(",", ""))
    if suffix in ("k", "K"):
        n *= 1_000
    elif suffix in ("m", "M"):
        n *= 1_000_000
    return int(round(n))


def parse_salary_range(text: str) -> tuple[int, int, str]:
    """Return (min, max, currency). Missing bound mirrors the other; all-zero
    when nothing parses. Only recognizes plausible salary magnitudes (>= $10k)
    to avoid matching stray numbers in prose."""
    if not text:
        return 0, 0, "USD"

    currency = "USD"
    amounts: list[int] = []
    for m in _AMOUNT.finditer(text):
        val = _to_int(m.group(1), m.group(2) or "")
        # A bare "220" with no k/M is only a salary if it's already large.
        if val >= 10_000:
            amounts.append(val)
    if not amounts:
        return 0, 0, currency

    lo = min(amounts)
    hi = max(amounts)
    return lo, hi, currency

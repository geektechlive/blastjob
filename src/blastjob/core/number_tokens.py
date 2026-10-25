"""Figure tokens and their comparable keys, shared by the prep pack and the debrief.

One definition of what a "figure" is (money, a percentage, a bare magnitude, a
comma-grouped or four-digit-and-up number) and of when two figures are the same one.
Trust is always a whole-token comparison on `normalize_number` keys, never a substring
test: a truncated figure and a misplaced decimal are the likeliest errors, and a
substring check clears both. The prep pack (core/prep.py) marks unverified figures with
`[confirm]`; the interview debrief (core/debrief.py) applies its own policy on top
(years and the like are skipped there). See docs/truth-guarantees.md.
"""

from __future__ import annotations

import re
from functools import lru_cache

from blastjob.core.packet_gate import _SCALE as SHARED_SCALE
from blastjob.core.packet_gate import _SCALE_WORDS as SHARED_SCALE_WORDS
from blastjob.core.percent_figures import PERCENT, percent_key

# A scale is one letter or one spelled-out word, and the shared \b is what keeps
# the letter branch from eating the first character of an ordinary word: in
# "$250,000 base" the B-branch takes the 'b' of "base", fails the boundary, and
# the whole optional group collapses to empty rather than swallowing the space.
# Shared with packet_gate (one scale vocabulary, `bn`/`mn`/`tn` included), so
# "$2.1bn" is a figure here too and is marked when the trusted text lacks it.
_MONEY = rf"\$\d[\d,]*(?:\.\d+)?(?:\s?{SHARED_SCALE})?"
_MAGNITUDE = rf"\d+(?:\.\d+)?\s?{SHARED_SCALE}"
# Headcount-shaped: comma-grouped, or four digits and up (which also covers
# years). Two- and three-digit bare integers stay unmatched on purpose, so a
# 30/60/90 plan and "3-5 questions" do not fill with markers.
_BIG_BARE = r"(?:\d{1,3}(?:,\d{3})+|\d{4,})(?:\.\d+)?"
# The bare forms carry a prefix guard so a match cannot start inside a larger
# number or a word; the shared suffix guard stops it ending inside one.
NUMERIC = re.compile(
    rf"(?:{_MONEY}|(?<![\w.,$])(?:{PERCENT}|{_MAGNITUDE}|{_BIG_BARE}))(?!\w|[.,]\d)"
)


def normalize_number(token: str) -> str:
    """One comparable key per figure, so `$3.5 billion`, `$3.5B` and `$3.5 B` are
    the same figure while `$240` and `$240,000` stay different ones."""
    percent = percent_key(token)
    if percent is not None:
        return percent
    text = token.strip().lower()
    for word, letter in SHARED_SCALE_WORDS.items():
        text = text.replace(word, letter)
    return text.replace(",", "").replace(" ", "")


@lru_cache(maxsize=8)
def figure_keys(text: str) -> frozenset[str]:
    """Every whole figure in `text`, as comparable keys. Cached because the prep
    stream re-checks the same work history once per completed line."""
    return frozenset(normalize_number(m.group(0)) for m in NUMERIC.finditer(text))

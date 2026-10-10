"""One title normalizer for "is this the same role?" grouping.

Shared by the discovery cluster fingerprint and the same-role rules
(`core.role_clusters`, `core.same_role`), so a title that groups one way on
insert groups the same way in the backfill.

Deliberately NOT used by `core.closure_links`: its employer-link matcher
expands abbreviations the other way and its similarity ratio is calibrated on
that form. Changing it could falsely close a live posting.
"""

from __future__ import annotations

import re

_PARENS = re.compile(r"\([^)]*\)")
_ON_SITE = re.compile(r"\bon[\s-]?site\b")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Multi-word phrases first, folded to one token, so "Vice President" == "VP".
_PHRASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bvice president\b"), "vp"),
    (re.compile(r"\binformation technology\b"), "it"),
)
_WORDS: dict[str, str] = {"senior": "sr"}
# Work-arrangement words and connectives say nothing about which role it is.
_DROP: frozenset[str] = frozenset({"of", "and", "the", "for", "remote", "hybrid", "onsite"})


def normalize_role_title(title: str | None) -> str:
    """'VP of Technology' and 'Vice President of Technology' -> 'vp technology'.

    Lowercased, parentheticals dropped, punctuation-insensitive; folds
    vice president -> vp, senior -> sr, information technology -> it; drops
    of/and/the/for/& and remote/onsite/on-site/hybrid."""
    text = _PARENS.sub(" ", (title or "").lower()).replace("&", " ")
    text = _ON_SITE.sub(" ", text)
    text = _NON_ALNUM.sub(" ", text)
    for pattern, replacement in _PHRASES:
        text = pattern.sub(replacement, text)
    words = (_WORDS.get(w, w) for w in text.split())
    return " ".join(w for w in words if w not in _DROP)

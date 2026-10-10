"""Deterministic scope prefilter — cut the discovered-postings haul down to roles
worth paying the LLM to score, WITHOUT gating on title variants.

Policy, configured by the candidate (`[discovery] title_keep_terms` /
`title_drop_terms` in config.toml): drop job *families* the candidate never
wants, never drop title *variants* of a family they do want. So:
- a title containing a configured keep term  -> KEEP, any wording
- a title containing a configured drop term  -> KEEP only if the JD shows the
                                                 scope the candidate targets
                                                 (people-management language);
                                                 else DROP
- anything else                              -> KEEP (let the LLM judge)
- both lists empty (an unconfigured install) -> KEEP everything; nothing here
  is dropped until the candidate fills in [discovery] title_keep_terms /
  title_drop_terms.

Errs toward KEEP: a false keep just costs one LLM score; a false drop hides a
real role forever. Matching is on whole words to avoid substring false hits.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# JD signals that a dropped-family title actually carries the people-management
# scope the candidate targets — a rescue. Deliberately strict: only unambiguous
# people-leadership phrases, since weak terms ("budget", "team of", "oversee")
# appear in plenty of individual-contributor JDs (especially sales) and would
# rescue roles that are not leadership.
SCOPE_SIGNALS = [
    "direct reports",
    "people management",
    "lead a team of",
    "leads a team of",
    "manage a team of",
    "managing a team of",
    "staff of",
    "build and lead a team",
    "hire and develop a team",
    "reports will include",
]


def _has_term(text: str, terms: Sequence[str]) -> str | None:
    for t in terms:
        t = (t or "").strip().lower()
        if not t:
            continue
        if re.search(rf"\b{re.escape(t)}\b", text):
            return t
    return None


def classify_scope(
    title: str,
    jd: str,
    keep_terms: Sequence[str] = (),
    drop_terms: Sequence[str] = (),
) -> tuple[bool, str]:
    """Return (keep, reason). `keep_terms`/`drop_terms` come from
    `DiscoveryConfig.title_keep_terms`/`title_drop_terms`. Both empty means the
    prefilter is unconfigured and keeps everything for the LLM to judge."""
    t = (title or "").lower()
    body = (jd or "").lower()

    keep_hit = _has_term(t, keep_terms)
    if keep_hit:
        return True, f"configured keep term ('{keep_hit}')"

    drop_hit = _has_term(t, drop_terms)
    if drop_hit:
        sig = _has_term(body, SCOPE_SIGNALS)
        if sig:
            return (
                True,
                f"title matches a dropped family ('{drop_hit}') but the JD "
                f"shows target scope ('{sig}')",
            )
        return False, f"configured drop family ('{drop_hit}')"

    return True, "ambiguous scope — deferred to LLM"

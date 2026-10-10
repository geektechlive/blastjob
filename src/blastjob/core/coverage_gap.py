"""Third bucket: work-history-provable requirements the resume never used.

`core/scoring.py` groundedness catches resume claims the work history cannot
prove. This module catches the mirror-image failure: a job-description
requirement that `analyze_coverage` (`core/coverage.py`) already determined
`work_history.md` genuinely supports -- with a Python-verified,
substring-checked `evidence_quote`, see `coverage.parse_coverage` -- that the
generated resume simply never used. That failure quietly undersells a
qualified candidate, and nothing else in the packet-quality program can see
it, because `analyze_coverage(job_description, ...)` takes no resume argument
and therefore has no way to know what the build actually shipped. This module
is the join `coverage.py` structurally cannot do on its own.

Measured case (2026-09-15, a leadership-role packet): an honest
resume scored 68 on JD coverage. Eight TRUE items already sitting in
`work_history.md` -- including a data-platform scale metric, a change-advisory-
board (CAB) process, M&A integration work, and a vendor-savings percentage -- were
absent from the generated bullets. Adding them (verbatim) raised coverage to 72
while groundedness held at 97: real qualifications that never made it onto
the page.

NO LLM REPAIR HERE, ON PURPOSE. The original design (2026-09-15 plan, W3, its
first draft) fed a detected gap back to the model and let it write a bullet
for it. It was removed after this scenario surfaced in adversarial review:
`work_history.md` says "evaluated a message-queue platform migration"; an
LLM repair pass writes "Led the message-queue platform migration" and attaches
the genuine sentence as its evidence quote. The evidence-quote substring
check (the same discipline `core/coverage.py` and `core/scoring.py` already
apply) passes -- the quote really is in the history -- and groundedness
holds, because groundedness only verifies that quoted text exists verbatim,
never that the claim built on top of it is true. The resume now asserts
something the candidate never did, and every automated check in the pipeline says the
packet is fine. Verbatim insertion -- inserting the exact quote, never a
model's paraphrase of it -- cannot drift the same way: the bullet either is
the sentence the candidate actually wrote, or nothing is inserted at all. Any future
LLM-assisted repair of these gaps must pass a claim-level diff -- every added
or materially changed claim maps to a specific evidence span, and an
unsupported addition is rejected regardless of what happens to any aggregate
score -- before it may touch this module's output. Do not re-add a rewrite
step without that.

DECIDED 2026-09-17 (dead-code review): this module used to also export
`as_insertable_bullet`, a "- quote." formatter with no caller anywhere in
src -- `core.coverage_run.render_gaps_md` (the only consumer of these gaps)
already echoes `gap.history_quote` verbatim as a blockquote, and the cockpit
renders that sidecar directly, so the gap text a human would copy is already
on screen. A copy-to-clipboard affordance for it would be client-side (copy
whatever the page already displays) and would never need a Python function to
emit the "- "-prefixed, period-terminated string -- "Web is the only surface"
(CLAUDE.md) argues for deleting an unrouted formatter, not for building it a
route. Removed rather than kept as an unused export; the verbatim-insertion
argument above survives intact since it was never about this function's
specific formatting, only about never letting a model rewrite the quote.
"""

from __future__ import annotations

import re

from blastjob.core.packet_report import CoverageGap
from blastjob.models.coverage import CoverageReport

# Tokens shorter than this are dropped from matching unless they carry a
# digit or a percent sign (numbers and measurements are exactly the kind of
# short, high-signal token -- "80TB", "25%" -- that would otherwise be
# filtered out for being brief).
_MIN_TOKEN_LEN = 4

# Fraction of a quote's salient tokens that must recur in the resume for the
# quote to be considered "used" rather than a gap. Chosen empirically against
# a real packet: a genuine gap (the CAB-process sentence, whose
# only resume trace is the bare term "CAB" in the Skills line, itself
# excluded -- see `_strip_skills_section`) landed at 0.69 overlap; the
# weakest genuine match found (a vendor-savings bullet, reworded only at its
# tail) landed at 0.77. 0.75 sits in that gap. There is no wider margin
# available without more real examples, so treat findings near the boundary
# as advisory, not certain -- this module's docstring below explains why.
_OVERLAP_THRESHOLD = 0.75

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "for",
        "to",
        "of",
        "in",
        "on",
        "at",
        "with",
        "by",
        "as",
        "is",
        "was",
        "were",
        "be",
        "been",
        "being",
        "that",
        "this",
        "these",
        "those",
        "it",
        "its",
        "our",
        "their",
        "we",
        "from",
        "into",
        "across",
        "over",
        "under",
        "per",
        "up",
        "out",
        "all",
        "new",
        "not",
        "no",
        "also",
        "such",
        "than",
        "then",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+%?")
_SECTION_HEADER_RE = re.compile(r"^##\s+(.+)$")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _salient_tokens(text: str) -> set[str]:
    """Distinctive tokens from `text`: numbers/percentages of any length, plus
    words of at least `_MIN_TOKEN_LEN` characters that aren't stopwords."""
    tokens: set[str] = set()
    for tok in _tokenize(text):
        if any(ch.isdigit() for ch in tok) or tok.endswith("%"):
            tokens.add(tok)
        elif len(tok) >= _MIN_TOKEN_LEN and tok not in _STOPWORDS:
            tokens.add(tok)
    return tokens


def _strip_skills_section(resume_md: str) -> str:
    """Drop the top-level `## Skills` section before matching.

    A term appearing only in the flat, comma-separated skills line is not
    evidence the resume *used* the supporting material as a claim -- it is
    exactly the keyword-without-backing pattern the app already treats as a
    defect elsewhere (every term on the competencies line must also appear in
    a real work-history-backed bullet). Counting a skills-line mention as
    "used" would let a resume earn credit for support it never actually
    demonstrated, which is the same failure this module exists to catch.

    Matches only a top-level `## ` header (two hashes, e.g. "## Skills"),
    never a `### ` role heading, so role bullets are never dropped.
    """
    kept: list[str] = []
    skipping = False
    for line in resume_md.splitlines():
        header = _SECTION_HEADER_RE.match(line)
        if header:
            skipping = header.group(1).strip().lower() == "skills"
            if skipping:
                continue
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


def _quote_is_used(quote: str, normalized_resume_body: str, resume_tokens: set[str]) -> bool:
    normalized_quote = " ".join(quote.lower().split())
    if normalized_quote and normalized_quote in normalized_resume_body:
        return True

    quote_tokens = _salient_tokens(quote)
    if not quote_tokens:
        # Nothing distinctive to check against -- err toward not flagging a
        # gap on a quote too short or generic to judge (see module docstring
        # on false positives).
        return True

    overlap = len(quote_tokens & resume_tokens) / len(quote_tokens)
    return overlap >= _OVERLAP_THRESHOLD


def unused_support(report: CoverageReport, resume_md: str) -> list[CoverageGap]:
    """Requirements the work history supports (`covered=True`) that the
    generated resume never used.

    Matching rule: a requirement's `evidence_quote` counts as "used" if either
    (a) its whitespace-normalized text appears verbatim in the resume (fast
    path -- catches exact and near-exact copies), or (b) at least
    `_OVERLAP_THRESHOLD` of its salient tokens (words >= `_MIN_TOKEN_LEN`
    chars, or any token containing a digit/percent, excluding stopwords) also
    appear somewhere in the resume body. The `## Skills` section is excluded
    from that body first (see `_strip_skills_section`), so a bare keyword in
    the competencies line does not count as the claim having been made.

    This is intentionally a paraphrase-tolerant bag-of-words check, not a
    semantic one, so it has both failure modes:

    - **False negative (a real gap goes unreported):** if a quote's salient
      vocabulary happens to be generic enough that it recurs elsewhere in the
      resume in unrelated bullets ("process", "risk", "system", "change"),
      overlap can cross the threshold even though the specific accomplishment
      was never used. The empirical CAB-process gap in the measured packet
      sits at 0.69 overlap for exactly this reason -- close enough to the
      0.75 threshold that a slightly more generic requirement could be missed.
    - **False positive (used material gets reported as a gap):** a quote the
      resume genuinely used but reworded heavily -- synonym swaps ("cut" for
      "reduce"), restructured clauses, dropped qualifiers -- can lose enough
      salient tokens to fall under the threshold and get flagged even though
      the accomplishment is on the page.

    Given both directions of error, treat the result as a review list for a
    human (or a verbatim-insertion pass), not a verdict: a false positive
    here costs a moment's glance at an already-present bullet; a false
    negative just means the app already accepted what groundedness had
    already forced (the substring-verified evidence_quote must be real), so
    it is not a fabrication risk either way.

    Only `covered=True` requirements are considered: an uncovered requirement
    means the work history cannot prove it, so there is nothing to insert,
    verbatim or otherwise -- inventing support would be fabrication, which is
    exactly what this module (and `core/scoring.py`) exists to prevent.
    """
    resume_body = _strip_skills_section(resume_md)
    normalized_body = " ".join(resume_body.lower().split())
    resume_tokens = _salient_tokens(resume_body)

    gaps: list[CoverageGap] = []
    for req in report.requirements:
        if not req.covered or not req.evidence_quote:
            continue
        if _quote_is_used(req.evidence_quote, normalized_body, resume_tokens):
            continue
        gaps.append(
            CoverageGap(
                requirement=req.text,
                history_quote=req.evidence_quote,
                priority=req.priority,
            )
        )
    return gaps

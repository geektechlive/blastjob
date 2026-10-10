"""Deterministic checks for prose that reads as stale resume-speak or as
machine-generated writing, in a generated resume or cover letter.

Every finding from this module is `Severity.MINOR` (advisory) — see
`core/packet_report.py`'s module docstring for why severity is calibrated to
the user's actual application boards, and why nothing derived from prose style
ever blocks a packet. The sourcing for each rule below is spelled out in
docs/ARCHITECTURE.md ("Evidence base"); nothing
here is an invented threshold.

NO AI-DETECTOR SCORING, EVER. This module does not compute, and must never be
extended to compute, a probability that text is "AI-written". Liang et al.
(arXiv 2304.02819) found GPT detectors falsely flag 61% of non-native English
writing, and GPTZero itself abandoned the perplexity/burstiness metric it
popularized. "Reads as competent human prose" and "beats a detector" are
different targets that sometimes conflict; only the first is in scope here.
Anyone re-adding detector-style scoring to this file is reintroducing a
metric its own inventor walked away from.

Two vocabulary lists are kept deliberately separate because they trace to
different evidence:

- `_LLM_REGISTER_WORDS` (rule: `llm_register_word`) is the vocabulary Kobak
  et al. (arXiv 2406.07016) measured as elevated by LLM usage across >15M
  PubMed abstracts. It is informational, not proof any specific word was
  AI-written.
- `_STALE_CLICHE_*` (rule: `stale_resume_cliche`) is ordinary stale
  resume-speak (career-center guidance, cross-source), unrelated to AI
  authorship. Merging the two lists would make the checker both
  over-inclusive (calling ordinary cliches "AI tells") and under-inclusive
  (hiding the Kobak evidence behind unrelated career advice).

There is no `baseline` parameter and no relative-vs-baseline comparison in
this module. There used to be a `uniform_rhythm` check that compared `text`
against a same-kind `baseline` sample; it has been removed. Its own
threshold (`_UNIFORM_RHYTHM_RELATIVE_FLOOR`) was described, honestly, as
"not a sourced absolute number" — which fails this repo's documented-basis
rule — and callers were passing `work_history.md` (bullet-heavy, not prose)
or, worse, the text under test as its own baseline, which makes a relative
check vacuous by construction. If sentence-rhythm checking is reintroduced,
the only honest form is an optional user-supplied sample at
`<data dir>/prose_baseline.md`, reported in `checks_skipped` when absent —
never an auto-derived baseline.

The em-dash check (`em_dash`) is absolute, not relative: one finding per
occurrence. Its authority is this project's own convention — "No em dashes
in writing output," Source `CLAUDE.md` — which says NONE, not "fewer than
some sample". It makes no ATS-parsing claim: no vendor documents em dashes
breaking a parser.

The adjective-stack check (`adjective_stack`) is also absolute now: one
finding per occurrence of stacking/superlative vocabulary, using the same
"not immediately followed by a figure or named fact" exemption the
stale-cliche rule already uses. The vocabulary list is the documented basis;
no threshold and no baseline are needed.
"""

from __future__ import annotations

import re
from typing import Literal

from blastjob.core.packet_report import Artifact, Finding, Severity

Kind = Literal["resume", "cover_letter"]

# ---------------------------------------------------------------------------
# Rule 1: Kobak et al. arXiv 2406.07016 — LLM-elevated vocabulary.
# ---------------------------------------------------------------------------

_LLM_REGISTER_WORDS = (
    "delve",
    "underscore",
    "meticulous",
    "commendable",
    "showcase",
    "boast",
    "intricate",
    "tapestry",
    "surpass",
)

# ---------------------------------------------------------------------------
# Rule 2: stale resume cliches. Own evidence base (career-center guidance,
# cross-source), not AI-authorship evidence.
# ---------------------------------------------------------------------------

# Phrases/words matched literally (word-boundary, whitespace-flexible).
_STALE_CLICHE_PHRASES = (
    "results-driven",
    "proven track record",
    "detail-oriented",
    "team player",
    "dynamic leader",
    "passionate",
    "cutting-edge",
    "synergy",
)

# Verbs matched with inflections (spearhead/spearheads/spearheaded/spearheading,
# leverage/leverages/leveraged/leveraging). "leverage" is flagged whenever the
# checker sees it used as a verb-form token; distinguishing "leverage" the noun
# from "leverage" the verb would need a POS tagger, which this module
# deliberately does not use (stdlib only) — a documented over-flag, not a bug.
_STALE_CLICHE_VERBS = ("spearhead", "leverage")

# ---------------------------------------------------------------------------
# Rule 3: cover-letter openers/closers.
# ---------------------------------------------------------------------------

_FORMULAIC_OPENERS = (
    "i am writing to express my interest",
    "i am excited to apply",
)

_FORMULAIC_CLOSERS = (
    "thank you for your time and consideration",
    "i look forward to hearing from you",
)

_OPENING_WINDOW = 220
_CLOSING_WINDOW = 220

# ---------------------------------------------------------------------------
# Rule 4/5: antithesis and tricolon caps (fixed caps given directly by the
# plan, not invented here).
# ---------------------------------------------------------------------------

_ANTITHESIS_RE = re.compile(
    r"\bnot\s+just\b[^.!?]{0,80}?\bbut\b" r"|\bit'?s\s+not\s+about\b[^.!?]{0,80}?\bit'?s\s+about\b",
    re.IGNORECASE,
)
_ANTITHESIS_MAX_PER_DOC = 1

# Three-item "A, B, and C" lists, each item capped at 3 words so this does not
# match ordinary longer enumerations (skills lists, six-function org
# descriptions). The negative lookbehind for ", " keeps it from matching items
# 2-4 of a *four*-item list as if they were their own tricolon. It will still
# occasionally miscount a genuinely four-item list that happens not to have
# ", " immediately before what looks like the first of three — a known,
# documented limitation of a regex approach with no real parser behind it.
_ITEM = r"[A-Za-z][\w'-]*(?:\s+[A-Za-z][\w'-]*){0,2}"
_TRICOLON_RE = re.compile(
    rf"(?<!,\s)\b({_ITEM}),\s+({_ITEM}),?\s+and\s+({_ITEM})\b",
)
_TRICOLON_MAX_PER_DOC = 2

_WORD_RE = re.compile(r"[A-Za-z0-9'-]+")

# ---------------------------------------------------------------------------
# Rule 6: em-dash (absolute, one finding per occurrence) and adjective-stack
# vocabulary (absolute, one finding per occurrence).
# ---------------------------------------------------------------------------

_ADJECTIVE_STACK_WORDS = frozenset(
    {
        "robust",
        "scalable",
        "innovative",
        "dynamic",
        "strategic",
        "comprehensive",
        "holistic",
        "seamless",
        "exceptional",
        "cutting-edge",
        "state-of-the-art",
        "world-class",
        "best-in-class",
        "industry-leading",
        "mission-critical",
        "game-changing",
        "transformative",
        "unparalleled",
    }
)

# ---------------------------------------------------------------------------
# Rule 7: job-description phrase echo, exempting job titles / companies /
# certifications / technologies / domain phrases.
# ---------------------------------------------------------------------------

_JD_ECHO_MIN_WORDS = 4

_CONNECTOR_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "and",
        "or",
        "for",
        "in",
        "on",
        "at",
        "to",
        "with",
        "by",
        "from",
        "as",
        "is",
        "are",
        "&",
    }
)


def _inflection_pattern(word: str) -> str:
    """Build a word-boundary regex matching a word and its common inflections
    (plural/-s, -d, -ed, -ing), including the trailing-e-drop case
    (showcase -> showcasing). This is a plain-English heuristic, not a
    morphological analyzer — it will occasionally under- or over-match on an
    irregular form, which is acceptable for an advisory-only checker.
    """
    stem = word[:-1] if word.endswith("e") else word
    return rf"\b{re.escape(stem)}e?(?:es|ing|ed|d|s)?\b"


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _looks_like_proper_noun_phrase(words: list[str]) -> bool:
    """True when every content word (connectors like "of"/"the" excluded)
    starts with an uppercase letter — the signature of a job title, company
    name, certification, or acronym-bearing technology phrase ("Director of
    Information Technology", "SOC 2 Type 2", "Oracle NetSuite ERP").

    Known limits, documented rather than hidden: this will also exempt a
    genuinely mirrored phrase that happens to be capitalized in the posting
    (e.g. an invented internal program name), and it will NOT exempt a
    legitimate technology or domain term the posting wrote in lowercase
    ("kubernetes", "site reliability engineering"). It is a heuristic, not a
    gazetteer of real titles/certs/technologies.
    """
    content = [w for w in words if w.lower() not in _CONNECTOR_WORDS]
    if not content:
        return False
    return all(w[:1].isupper() for w in content)


def _cliche_exempt_tail(text: str, end: int) -> bool:
    """Rule 2's "not immediately followed by a specific figure or named
    fact" exemption. Looks forward from the end of the match to the end of
    the sentence (or a bounded window) for a digit or a capitalized token,
    which stands in for "named fact" without a POS tagger.
    """
    window = text[end : end + 160]
    boundary = re.search(r"[.!?\n]", window)
    tail = window[: boundary.start()] if boundary else window
    if re.search(r"\d", tail):
        return True
    return bool(re.search(r"\b[A-Z][A-Za-z'-]{2,}\b", tail))


def _finding(
    *,
    code: str,
    message: str,
    artifact: Artifact,
    line: int | None = None,
    evidence: str = "",
    source: str,
) -> Finding:
    return Finding(
        severity=Severity.MINOR,
        code=code,
        message=message,
        artifact=artifact,
        line=line,
        evidence=evidence,
        source=source,
    )


def _check_llm_register_words(text: str, artifact: Artifact) -> list[Finding]:
    findings = []
    for word in _LLM_REGISTER_WORDS:
        for match in re.finditer(_inflection_pattern(word), text, re.IGNORECASE):
            findings.append(
                _finding(
                    code="llm_register_word",
                    message=(
                        f'"{match.group(0)}" is in the vocabulary Kobak et al. measured as '
                        "elevated by LLM usage across >15M PubMed abstracts — informational, "
                        "not proof of AI authorship."
                    ),
                    artifact=artifact,
                    line=_line_of(text, match.start()),
                    evidence=match.group(0),
                    source="Kobak et al., arXiv 2406.07016",
                )
            )
    return findings


def _check_stale_cliches(text: str, artifact: Artifact) -> list[Finding]:
    findings = []
    phrase_patterns = [
        (
            re.compile(r"\b" + re.escape(phrase).replace(r"\ ", r"\s+") + r"\b", re.IGNORECASE),
            phrase,
        )
        for phrase in _STALE_CLICHE_PHRASES
    ]
    verb_patterns = [
        (re.compile(_inflection_pattern(verb), re.IGNORECASE), verb) for verb in _STALE_CLICHE_VERBS
    ]
    patterns = phrase_patterns + verb_patterns
    for pattern, _label in patterns:
        for match in pattern.finditer(text):
            if _cliche_exempt_tail(text, match.end()):
                continue
            findings.append(
                _finding(
                    code="stale_resume_cliche",
                    message=(
                        f'"{match.group(0)}" is stale resume-speak with nothing specific '
                        "attached after it — a figure or named fact would exempt it."
                    ),
                    artifact=artifact,
                    line=_line_of(text, match.start()),
                    evidence=match.group(0),
                    source="career-center style guidance, cross-source",
                )
            )
    return findings


def _check_formulaic_opening(text: str, artifact: Artifact) -> list[Finding]:
    head = text.strip()[:_OPENING_WINDOW].lower()
    findings = []
    for phrase in _FORMULAIC_OPENERS:
        if phrase in head:
            findings.append(
                _finding(
                    code="formulaic_opening",
                    message=f'Opens with the stock phrase "{phrase}".',
                    artifact=artifact,
                    evidence=phrase,
                    source="U. Michigan career center + cross-source cover-letter guidance",
                )
            )
    return findings


def _check_formulaic_closing(text: str, artifact: Artifact) -> list[Finding]:
    tail = text.strip()[-_CLOSING_WINDOW:].lower()
    findings = []
    for phrase in _FORMULAIC_CLOSERS:
        if phrase in tail:
            findings.append(
                _finding(
                    code="formulaic_closing",
                    message=f'Closes with the stock phrase "{phrase}".',
                    artifact=artifact,
                    evidence=phrase,
                    source="U. Michigan career center + cross-source cover-letter guidance",
                )
            )
    return findings


def _check_antithesis(text: str, artifact: Artifact) -> list[Finding]:
    matches = list(_ANTITHESIS_RE.finditer(text))
    if len(matches) <= _ANTITHESIS_MAX_PER_DOC:
        return []
    return [
        _finding(
            code="antithesis_overuse",
            message=(
                f'{len(matches)} "not just X, but Y" / "it\'s not about X, it\'s about Y" '
                f"constructions found; more than {_ANTITHESIS_MAX_PER_DOC} reads as a tic."
            ),
            artifact=artifact,
            evidence=matches[0].group(0)[:120],
            source="packet-quality-program plan (fixed document cap)",
        )
    ]


def _check_tricolon(text: str, artifact: Artifact) -> list[Finding]:
    matches = list(_TRICOLON_RE.finditer(text))
    if len(matches) <= _TRICOLON_MAX_PER_DOC:
        return []
    return [
        _finding(
            code="tricolon_overuse",
            message=(
                f"{len(matches)} three-item parallel lists found; more than "
                f"{_TRICOLON_MAX_PER_DOC} per document reads as a rhetorical tic."
            ),
            artifact=artifact,
            evidence=matches[0].group(0)[:120],
            source="packet-quality-program plan (fixed document cap)",
        )
    ]


def _check_em_dash(text: str, artifact: Artifact) -> list[Finding]:
    """One MINOR finding per em dash — absolute, not a density comparison.

    Authority is this project's own convention ("No em dashes in writing
    output," Source CLAUDE.md), which says NONE, not "fewer than some
    sample". No ATS-parsing claim is made: no vendor documents em dashes
    breaking a parser.
    """
    findings = []
    for match in re.finditer("—", text):
        start = match.start()
        evidence = text[max(0, start - 20) : start + 21].strip()
        findings.append(
            _finding(
                code="em_dash",
                message=(
                    "Em dash found. This repo's own convention is no em dashes in writing "
                    "output (Source CLAUDE.md)."
                ),
                artifact=artifact,
                line=_line_of(text, start),
                evidence=evidence,
                source="Source CLAUDE.md convention",
            )
        )
    return findings


def _check_adjective_stack(text: str, artifact: Artifact) -> list[Finding]:
    """One MINOR finding per occurrence of stacking/superlative vocabulary,
    exempt when immediately followed by a figure or named fact (the same
    exemption `stale_resume_cliche` uses).

    This is a vocabulary match, not true adjective-stack detection —
    identifying an actual run of 3+ adjectives before a noun needs a POS
    tagger, which this module deliberately does not use (stdlib only, no
    nltk/spacy). Documented simplification, not a hidden one.
    """
    stack_re = re.compile(
        r"\b(" + "|".join(re.escape(w) for w in _ADJECTIVE_STACK_WORDS) + r")\b",
        re.IGNORECASE,
    )
    findings = []
    for match in stack_re.finditer(text):
        if _cliche_exempt_tail(text, match.end()):
            continue
        findings.append(
            _finding(
                code="adjective_stack",
                message=(
                    f'"{match.group(0)}" is stacking/superlative vocabulary with nothing '
                    "specific attached after it — a figure or named fact would exempt it."
                ),
                artifact=artifact,
                line=_line_of(text, match.start()),
                evidence=match.group(0),
                source="packet-quality-program plan (documented vocabulary list)",
            )
        )
    return findings


def _check_jd_phrase_echo(
    text: str, job_description: str, history: str, artifact: Artifact
) -> list[Finding]:
    if not job_description.strip():
        return []
    n = _JD_ECHO_MIN_WORDS

    def ngram_set(source: str) -> set[tuple[str, ...]]:
        words = _WORD_RE.findall(source.lower())
        return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}

    jd_ngrams = ngram_set(job_description)
    history_ngrams = ngram_set(history)
    text_words = _WORD_RE.findall(text)

    findings = []
    flagged: set[str] = set()
    for i in range(len(text_words) - n + 1):
        window = text_words[i : i + n]
        norm = tuple(w.lower() for w in window)
        if norm not in jd_ngrams:
            continue
        if norm in history_ngrams:
            # Also appears in the candidate's own work history — a reused
            # fact the candidate legitimately uses, not pure JD mirroring.
            continue
        if _looks_like_proper_noun_phrase(window):
            # Job title / company / certification / technology phrase.
            continue
        content_words = [w for w in window if w.lower() not in _CONNECTOR_WORDS]
        if len(content_words) < 2:
            # A run made almost entirely of connector words ("as a member
            # of") is too generic to be meaningful mirroring evidence.
            continue
        phrase = " ".join(window)
        key = phrase.lower()
        if key in flagged:
            continue
        flagged.add(key)
        findings.append(
            _finding(
                code="jd_phrase_echo",
                message=(
                    f'{n}+ consecutive words lifted verbatim from the job description: "{phrase}"'
                ),
                artifact=artifact,
                evidence=phrase,
                source="job_description (this build)",
            )
        )
    return findings


def check_prose(
    text: str,
    *,
    kind: Kind,
    history: str = "",
    job_description: str = "",
) -> list[Finding]:
    """Run every deterministic prose check and return the combined findings.

    All findings are `Severity.MINOR` — advisory only, never blocking (see
    module docstring). There is no baseline parameter: `em_dash` and
    `adjective_stack` are absolute checks now (see module docstring for why
    the old baseline-relative form was removed). `history` is the
    candidate's own work history (e.g. `work_history.md`) and is consumed
    only by `jd_phrase_echo`, as the exemption list of facts/phrases the candidate
    legitimately reuses — never the text under test itself, which would make
    that exemption vacuous.
    """
    artifact = Artifact.RESUME if kind == "resume" else Artifact.COVER_LETTER

    findings: list[Finding] = []
    findings.extend(_check_llm_register_words(text, artifact))
    findings.extend(_check_stale_cliches(text, artifact))
    if kind == "cover_letter":
        findings.extend(_check_formulaic_opening(text, artifact))
        findings.extend(_check_formulaic_closing(text, artifact))
    findings.extend(_check_antithesis(text, artifact))
    findings.extend(_check_tricolon(text, artifact))
    findings.extend(_check_em_dash(text, artifact))
    findings.extend(_check_adjective_stack(text, artifact))
    findings.extend(_check_jd_phrase_echo(text, job_description, history, artifact))
    return findings

"""Who a role reports to, read from the description text (issue #82).

Deterministic and conservative: a phrase that does not clearly say what the ROLE
reports to yields nothing. No model call, no guessing, and it never touches the
fit score. See docs/how-it-works/matching.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_PHRASE_CHARS = 40
_WHO_WINDOW = 90

_ARTICLES = r"(?:(?:the|our|a|an|its)\s+)*"
# What may directly precede the verb. It must make the ROLE the subject, so
# "the network team reports to the CIO" and "team members will report to ..." never match.
_LEAD = (
    r"(?:(?:^|[.!?:;|/•*(])\s*(?:[-–]\s*)?(?:(?:this|the)\s+(?:role|position)\s+)?"
    r"(?:(?:will|would)\s+)?"
    r"|\b(?:you(?:['’]ll)?|candidate|incumbent|hire|role|position|job|opportunity|"
    r"person|individual)\s+(?:(?:will|would|shall)\s+)?)"
)
_TO = r"(?:directly\s+)?(?:in)?to\b[:\s\-–]*"
_WHO = rf"(?P<who>[^.;:\n,()]{{2,{_WHO_WINDOW}}})"
_VERB = re.compile(
    rf"{_LEAD}(?:reports?|reporting)\s+{_TO}{_ARTICLES}{_WHO}",
    re.IGNORECASE | re.MULTILINE,
)
# "..., reporting to the CTO" and "(reporting to the CEO)" are also about the role.
_ING = re.compile(rf"[,(]\s*reporting\s+{_TO}{_ARTICLES}{_WHO}", re.IGNORECASE)
_WHO_STOP = re.compile(
    r"\s+(?:who|which|that|while|with|at|in|for|on|and\s+(?:will|is|are|has|you)|as\s+well)\b.*$",
    re.IGNORECASE,
)
_NOT_A_BOSS = re.compile(
    r"^(?:you|your|this|that|these|those|them|him|her|me|us|it|its|each|all|their|his|"
    r"role|position|incumbent|candidate|person|individual|hire|staff|directly)\b",
    re.IGNORECASE,
)
_EXEC_TEAM = re.compile(
    r"\b(?:member\s+of|sits?\s+on|seat\s+on|join(?:s|ing)?|part\s+of|serves?\s+on|serving\s+on)"
    r"\s+(?:(?:the|our|a|an)\s+)*(?:company['’]s\s+)?"
    r"(?:executive|senior|c-suite|management|leadership)(?:\s+(?:leadership|management))?"
    r"\s+(?:team|committee)\b",
    re.IGNORECASE,
)
_CHIEF = {
    "chief executive officer": "CEO",
    "chief financial officer": "CFO",
    "chief operating officer": "COO",
    "chief information officer": "CIO",
    "chief technology officer": "CTO",
    "chief information security officer": "CISO",
    "ceo": "CEO",
    "cfo": "CFO",
    "coo": "COO",
    "cio": "CIO",
    "cto": "CTO",
    "ciso": "CISO",
    "president": "President",
    "owner": "Owner",
}
_VP = re.compile(
    r"^(?P<rank>senior\s+|executive\s+|s|e)?(?:vice[\s-]president|vp)\b[\s,\-–]*"
    r"(?:of\s+|for\s+)?(?P<area>.*)$",
    re.IGNORECASE,
)
_VP_RANK = {"senior ": "SVP", "s": "SVP", "executive ": "EVP", "e": "EVP"}


@dataclass(frozen=True)
class ReportingLine:
    """`reports_to` is a short label or None (unknown); `exec_team` is True only
    when the text says the role is on the executive team."""

    reports_to: str | None = None
    exec_team: bool = False

    @property
    def label(self) -> str:
        """Display text, "" when nothing was found (rows stay quiet)."""
        parts: list[str] = []
        if self.reports_to:
            parts.append(f"Reports to {self.reports_to}")
        if self.exec_team:
            parts.append("executive team" if parts else "Executive team")
        return ", ".join(parts)


_CHIEF_PREFIX = re.compile(
    r"^(?P<title>" + "|".join(sorted(map(re.escape, _CHIEF), key=len, reverse=True)) + r")\b"
    r"(?P<rest>.*)$",
    re.IGNORECASE,
)
# "President and CEO" / "Owner and the board" name two bosses: keep the words as written.
_SECOND_BOSS = re.compile(
    r"^\s*(?:and|&|or|/)\s*(?:the\s+|our\s+)?"
    r"(?:chief|ceo|cfo|coo|cio|cto|ciso|president|owner|board|vp|vice|svp|evp)\b",
    re.IGNORECASE,
)


def _known_title(text: str) -> str | None:
    """CEO, CFO, ... when the phrase opens with one title; None for two bosses."""
    match = _CHIEF_PREFIX.match(text)
    if match is None:
        return None
    rest = match.group("rest")
    if rest.strip() and not rest.lstrip().lower().startswith(("and ", "or ")):
        return None
    if _SECOND_BOSS.match(rest):
        return None
    return _CHIEF[match.group("title").lower()]


def _normalize_who(phrase: str) -> str | None:
    text = _WHO_STOP.sub("", phrase).strip(" \t-–")
    if not text or _NOT_A_BOSS.match(text):
        return None
    known = _known_title(text)
    if known:
        return known
    vp = _VP.match(text)
    if vp:
        rank = _VP_RANK.get((vp.group("rank") or "").lower(), "VP")
        area = vp.group("area").strip()
        return f"{rank} of {area}" if area else rank
    if len(text) > MAX_PHRASE_CHARS:
        cut = text[:MAX_PHRASE_CHARS].rsplit(" ", 1)[0]
        return f"{cut}..."
    return text


def _find_reports_to(text: str) -> str | None:
    hits = sorted(
        (m for rx in (_VERB, _ING) for m in rx.finditer(text)),
        key=lambda m: m.start(),
    )
    for match in hits:
        who = _normalize_who(match.group("who"))
        if who:
            return who
    return None


def extract_reporting_line(jd: str | None) -> ReportingLine:
    """Read the reporting line and any executive-team membership from `jd`."""
    if not jd or not jd.strip():
        return ReportingLine()
    text = re.sub(r"[ \t]+", " ", jd)
    return ReportingLine(
        reports_to=_find_reports_to(text),
        exec_team=_EXEC_TEAM.search(text) is not None,
    )

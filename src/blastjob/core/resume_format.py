"""Deterministic structure checks on a generated resume.

Following the precedent set by `core.resume_dates` ("the LLM pass ignores the
one thing on a resume that is trivially falsifiable, so dates are checked
here instead"): structure, banned characters, encoding, and skills-term
backing are all mechanically checkable, so they get code, not a prompt.

Severity follows `core.packet_report`'s calibration: FATAL is reserved for
failures with evidence in the user's actual application path (a logged Workday
rejection, mojibake that shipped, an empty role that shipped in three sent
packets, dates that do not reconcile against `work_history.md`); everything
Textkernel-derived is MINOR and advisory only. See that module's docstring
before changing a severity.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from blastjob.core.company import company_key
from blastjob.core.packet_report import Finding, Severity
from blastjob.core.resume_dates import (
    DatePoint,
    DateRange,
    employer_names,
    history_headings,
    parse_range,
)

_EMPLOYER_LINE_RE = re.compile(r"^\*\*(?P<employer>.+?)\*\*\s*\|\s*(?P<rest>.+)$")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_STATE_RE = re.compile(r",\s*[A-Z]{2}\b")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
# The exact set behind the logged Workday rejection ("Contains illegal
# characters", 2026-09-10) -- matches llm/prompts.py:195 and the translation
# table in exporters/ats.py:19-30 exactly: < > [ ] { } " \. No backtick: there
# is no rejection evidence for one, and flagging a character nobody has ever
# seen rejected is the same over-flagging `core.packet_report`'s docstring
# warns against.
_BANNED_CHARS = '<>[]{}"\\'
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}")
_PII_RE = re.compile(
    r"\b(date of birth|dob|marital status|social security|ssn|"
    r"driver'?s licen[cs]e|passport number)\b",
    re.IGNORECASE,
)
_SKILLS_HEADING_RE = re.compile(
    r"^(core competencies|competencies|key skills|technical skills|skills|areas of expertise)$",
    re.IGNORECASE,
)
# 20, not 15: a 25-year senior career runs to two pages (senior-resume
# guidance, Jan 2025; a resume-length study of n=482 favoured two pages 2.9x at
# managerial level), and the 2006-2011 Fabrikam Software role holds M&A
# diligence, three integrations and a $2M valuation increase — exactly the
# differentiators a senior-leadership resume wants. The user's call,
# 2026-09-16. Pre-2006 roles stay title-only.
_EARLY_CAREER_TAIL_YEARS = 20
_WORDS_PER_PAGE = 550


@dataclass(frozen=True)
class _Role:
    """One `### ` role block, as parsed out of the resume markdown."""

    heading_text: str
    heading_line: int
    employer_line_text: str | None
    employer_line_no: int | None
    bullets: tuple[tuple[str, int], ...]


_EXPERIENCE_SECTION_RE = re.compile(r"experience|employment|career history", re.IGNORECASE)
_BULLET_MARKERS = ("- ", "* ", "\u2022 ")


def _experience_spans(lines: list[str]) -> list[tuple[int, int]]:
    """Line ranges of the `## ` experience-ish sections; the whole document if none.

    Only roles inside Experience are checked: `### Selected Projects` or
    `### Technical Skills` under other sections are not jobs. A resume with no
    such section at all keeps the old whole-document behaviour.
    """
    n = len(lines)
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for idx, line in enumerate(lines):
        if not (line.startswith("## ") or line.startswith("# ")):
            continue
        if start is not None:
            spans.append((start, idx))
            start = None
        if line.startswith("## ") and _EXPERIENCE_SECTION_RE.search(line[3:]):
            start = idx + 1
    if start is not None:
        spans.append((start, n))
    return spans or [(0, n)]


def _parse_roles(lines: list[str]) -> list[_Role]:
    """Roles from every experience section (see `_experience_spans`)."""
    roles: list[_Role] = []
    for lo, hi in _experience_spans(lines):
        roles.extend(_parse_role_span(lines, lo, hi))
    return roles


def _parse_role_span(lines: list[str], lo: int, hi: int) -> list[_Role]:
    """Every `### ` heading in `lines[lo:hi]`, its employer/date line (if any), and its bullets.

    A role's block runs from its heading to the next line starting with `#`
    (or end of file). The line right after the heading is treated as the
    employer/date line only when it actually looks like one
    (`**Employer** | City, ST | dates`) -- otherwise the heading is assumed to
    carry that information itself (the pre-fix one-line format), or bullets
    start immediately.
    """
    roles: list[_Role] = []
    i = lo
    n = hi
    while i < n:
        line = lines[i]
        if not line.startswith("### "):
            i += 1
            continue
        heading_text = line[4:].strip()
        heading_line = i + 1

        j = i + 1
        while j < n and not lines[j].strip():
            j += 1

        employer_text: str | None = None
        employer_line_no: int | None = None
        bullets_start = i + 1
        if j < n and not lines[j].startswith("#") and _EMPLOYER_LINE_RE.match(lines[j].strip()):
            employer_text = lines[j].strip()
            employer_line_no = j + 1
            bullets_start = j + 1

        bullets: list[tuple[str, int]] = []
        k = bullets_start
        while k < n and not lines[k].startswith("#"):
            stripped = lines[k].strip()
            if stripped.startswith(_BULLET_MARKERS):
                bullets.append((stripped[2:].strip(), k + 1))
            k += 1

        roles.append(
            _Role(
                heading_text=heading_text,
                heading_line=heading_line,
                employer_line_text=employer_text,
                employer_line_no=employer_line_no,
                bullets=tuple(bullets),
            )
        )
        i = k
    return roles


def _role_date_range(role: _Role) -> DateRange | None:
    if role.employer_line_text:
        rng = parse_range(role.employer_line_text)
        if rng is not None:
            return rng
    return parse_range(role.heading_text)


def _role_employer_field(role: _Role) -> str | None:
    if role.employer_line_text:
        m = _EMPLOYER_LINE_RE.match(role.employer_line_text)
        return m.group("employer").strip() if m else None
    if "|" in role.heading_text:
        parts = [p.strip() for p in role.heading_text.split("|")]
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    return None


def _is_recent(end: DatePoint, today: date, years: int = _EARLY_CAREER_TAIL_YEARS) -> bool:
    if end.present:
        return True
    cutoff = (today.year - years, today.month)
    end_month = end.month or 12  # a year-only end date is treated conservatively (latest possible)
    return (end.year, end_month) >= cutoff


def _union(ranges: list[DateRange]) -> DateRange:
    return DateRange(
        start=min((r.start for r in ranges), key=lambda p: p.sort_key),
        end=max((r.end for r in ranges), key=lambda p: p.sort_key),
    )


def _history_ranges(work_history_md: str) -> dict[str, list[DateRange]]:
    """company_key -> every individual heading's date range for that employer
    in `work_history.md`, kept separate rather than merged.

    `resume_dates.history_dates()` keeps only the first heading it finds per
    company, which is fine for its own purpose but under-covers a split
    promotion: Contoso Health Solutions has two headings (Infrastructure
    Lead, then Director of Technology Operations promoted from it). Comparing a
    resume against only the later heading's range would flag a correctly-
    split resume as inventing five years of tenure; comparing against a
    pre-merged union would instead accept a resume that only ever mentions
    the earlier title with the later title's end date. Keeping every range
    lets the caller check a resume role against any one of them, or against
    their union, as appropriate. Reuses `resume_dates.parse_range` for the
    actual date parsing; only the aggregation differs.
    """
    ranges: dict[str, list[DateRange]] = {}
    for parts, date_text in history_headings(work_history_md):
        if len(parts) < 2 or not parts[1]:
            continue
        name = re.sub(r"\s*\([^)]{0,80}\)\s*$", "", parts[1]).strip()
        rng = parse_range(date_text)
        if name and rng:
            ranges.setdefault(company_key(name), []).append(rng)
    return ranges


def _check_empty_recent_role(roles: list[_Role], today: date) -> list[Finding]:
    findings: list[Finding] = []
    for role in roles:
        if role.bullets:
            continue
        rng = _role_date_range(role)
        recent = True if rng is None else _is_recent(rng.end, today)
        if not recent:
            continue
        evidence = role.heading_text
        if role.employer_line_text:
            evidence = f"{role.heading_text} / {role.employer_line_text}"
        findings.append(
            Finding(
                severity=Severity.FATAL,
                code="empty_recent_role",
                message=(
                    f"'{role.heading_text}' has no bullets and ended within the last "
                    f"{_EARLY_CAREER_TAIL_YEARS} years -- a role in this window with zero "
                    "accomplishment bullets undersells the candidate and shipped in three "
                    "sent packets before this check existed."
                ),
                line=role.heading_line,
                evidence=evidence,
                source="D1: empty role heading shipped in two generated packets (2026-09)",
            )
        )
    return findings


def _check_heading_carries_employer(roles: list[_Role], employers: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for role in roles:
        heading = role.heading_text
        reasons: list[str] = []
        if "|" in heading:
            reasons.append("contains a '|' separator")
        if _YEAR_RE.search(heading):
            reasons.append("contains a year")
        if _STATE_RE.search(heading):
            reasons.append("contains a city/state")
        matched_employer = next((e for e in employers if e.lower() in heading.lower()), None)
        if matched_employer:
            reasons.append(f"contains the employer name '{matched_employer}'")
        if not reasons:
            continue
        findings.append(
            Finding(
                severity=Severity.MAJOR,
                code="heading_carries_employer",
                message=(
                    f"Role heading '{heading}' " + "; ".join(reasons) + " -- it should be "
                    "title-only, with employer/city/dates on the following line."
                ),
                line=role.heading_line,
                evidence=heading,
                source="Workday rejection 2026-09-10 (pre-fix one-line heading format)",
            )
        )
    return findings


def _check_role_not_in_history(work_history_md: str, roles: list[_Role]) -> list[Finding]:
    findings: list[Finding] = []
    known = employer_names(work_history_md)
    known_lower = {e.lower(): e for e in known}
    history_ranges = _history_ranges(work_history_md)

    resume_ranges: dict[str, list[DateRange]] = {}
    resume_labels: dict[str, str] = {}

    for role in roles:
        employer_field = _role_employer_field(role)
        if not employer_field:
            continue
        field_lower = employer_field.lower()
        match = next(
            (full for low, full in known_lower.items() if field_lower in low or low in field_lower),
            None,
        )
        if match is None:
            findings.append(
                Finding(
                    severity=Severity.FATAL,
                    code="role_not_in_history",
                    message=f"'{employer_field}' does not appear as an employer in work_history.md",
                    line=role.employer_line_no or role.heading_line,
                    evidence=employer_field,
                    source="work_history.md",
                )
            )
            continue
        rng = _role_date_range(role)
        if rng is None:
            continue
        key = company_key(match)
        resume_ranges.setdefault(key, []).append(rng)
        resume_labels.setdefault(key, match)

    for key, ranges in resume_ranges.items():
        history = history_ranges.get(key)
        if history is None:
            continue
        resume_union = _union(ranges)
        history_union = _union(history)
        # Two ways a resume's dates for this employer can be honest: its
        # combined span for the employer matches the employer's combined
        # span in history (a resume that shows every position it held
        # there), or every individual role it claims matches SOME individual
        # heading in history (a resume that shows only one of several
        # positions there, e.g. omitting an early one). Only fail both.
        if resume_union.agrees_with(history_union):
            continue
        if all(any(r.agrees_with(h) for h in history) for r in ranges):
            continue
        findings.append(
            Finding(
                severity=Severity.FATAL,
                code="role_not_in_history",
                message=(
                    f"Employment dates for {resume_labels[key]}: resume says {resume_union}, "
                    f"work history says {history_union}"
                ),
                evidence=str(resume_union),
                source="work_history.md",
            )
        )
    return findings


def _find_skills_bullets(lines: list[str]) -> list[str]:
    bullets: list[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = bool(_SKILLS_HEADING_RE.match(stripped[3:].strip()))
            continue
        if in_section and stripped.startswith("- "):
            bullets.append(stripped[2:].strip())
    return bullets


def _split_terms(text: str) -> list[str]:
    """Comma-split, but not inside parens: 'Hybrid Cloud (AWS, Azure)' is one
    term, not the malformed pair 'Hybrid Cloud (AWS' / 'Azure)'."""
    terms: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            terms.append(text[start:i])
            start = i + 1
    terms.append(text[start:])
    return terms


def _skills_terms(skills_bullets: list[str]) -> list[str]:
    terms: list[str] = []
    for bullet in skills_bullets:
        text = re.sub(r"^\*\*[^*]+\*\*:?\s*", "", bullet)  # drop the "**Category:**" label
        for chunk in _split_terms(text):
            term = chunk.strip().strip(".")
            if term:
                terms.append(term)
    return terms


def _backing_text(lines: list[str], skills_bullets: set[str]) -> str:
    pieces: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- ") and stripped[2:].strip() not in skills_bullets:
            pieces.append(stripped[2:])
    return " ".join(pieces).lower()


def _check_skills_backing(lines: list[str]) -> list[Finding]:
    skills_bullets = _find_skills_bullets(lines)
    if not skills_bullets:
        return []
    backing = _backing_text(lines, set(skills_bullets))
    findings: list[Finding] = []
    for term in _skills_terms(skills_bullets):
        if len(term) < 2:
            continue
        candidates = [term]
        paren = re.search(r"\(([^)]+)\)", term)
        if paren:
            candidates.append(paren.group(1))
            candidates.append(re.sub(r"\s*\([^)]*\)", "", term).strip())
        if "/" in term:
            candidates.extend(p.strip() for p in term.split("/"))
        if any(c and c.lower() in backing for c in candidates):
            continue
        findings.append(
            Finding(
                severity=Severity.MINOR,
                code="skills_term_without_backing",
                message=f"'{term}' appears on the competencies line but nowhere in a bullet.",
                evidence=term,
                # MINOR, not MAJOR (demoted 2026-09-17): this is a verbatim
                # substring check, so it cannot see that 'Vendor Management'
                # is backed by prose like 'vendor strategy and contract
                # negotiations'. This is the user's 2026-09-15 preference, a quality
                # standard they want surfaced, not an observed parse failure
                # with Workday/Dayforce/LinkedIn evidence behind it -- see
                # this module's docstring on severity calibration.
                source="The user's decision, 2026-09-15: a keyword must have something behind it "
                "(advisory -- verbatim substring match, demoted from MAJOR 2026-09-17)",
            )
        )
    return findings


def _strip_markdown_syntax(line: str) -> str:
    """What a bare-markdown scan approximates as 'the text the exporters
    would actually ship' -- link syntax resolved to its display text, heading/
    bullet markers dropped, bold/italic markers removed. Only used as the
    cheap pre-pass when no `docx_lines` are available; the real check reads
    the exported artifact directly (see module docstring / V1)."""
    text = _MD_LINK_RE.sub(lambda m: m.group(1), line)
    text = re.sub(r"^#{1,6}\s+", "", text)
    text = re.sub(r"^[-*+]\s+", "", text)
    text = text.replace("**", "")
    text = re.sub(r"(?<!\*)\*(?!\*)", "", text)
    return text


def _check_banned_characters(lines: list[str], *, from_docx: bool) -> list[Finding]:
    """No separate 'literal markdown link' branch: on the markdown path a
    link is resolved to display text by `_strip_markdown_syntax` before this
    scan runs (that is intentional modeling of what the exporters ship, not
    a defect to flag), and on the docx path a link literal that survived
    export already contains '[' and ']' -- banned characters in their own
    right -- so it is caught by `present` with no extra branch needed."""
    findings: list[Finding] = []
    for line in lines:
        scan = line if from_docx else _strip_markdown_syntax(line)
        present = sorted({ch for ch in _BANNED_CHARS if ch in scan})
        if not present:
            continue
        findings.append(
            Finding(
                severity=Severity.FATAL,
                code="banned_characters",
                message="Resume text contains banned characters "
                + " ".join(repr(c) for c in present)
                + " -- Workday rejects these",
                evidence=line.strip(),
                source="Workday rejection 2026-09-10: 'Contains illegal characters'",
            )
        )
    return findings


def _check_encoding_damage(lines: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for line in lines:
        if "�" in line:
            findings.append(
                Finding(
                    severity=Severity.FATAL,
                    code="encoding_damage",
                    message="Resume text contains a Unicode replacement character (U+FFFD)",
                    evidence=line.strip(),
                    source="D9: U+FFFD shipped in a real sent resume",
                )
            )
            continue
        for ch in line:
            cat = unicodedata.category(ch)
            if cat == "Cs" or (cat == "Cc" and ch not in "\t\n\r"):
                findings.append(
                    Finding(
                        severity=Severity.FATAL,
                        code="encoding_damage",
                        message=f"Resume text has an unencodable/control character U+{ord(ch):04X}",
                        evidence=line.strip(),
                        source="D9: encoding damage shipped in a real packet",
                    )
                )
                break
    return findings


def _check_email_phone(text: str) -> list[Finding]:
    findings: list[Finding] = []
    emails = sorted({m.group(0) for m in _EMAIL_RE.finditer(text)})
    if len(emails) > 1:
        findings.append(
            Finding(
                severity=Severity.MINOR,
                code="multiple_email",
                message=f"{len(emails)} email addresses found; an ATS expects exactly one.",
                evidence=", ".join(emails),
                source="Textkernel Resume Quality 132",
            )
        )
    phones = sorted({m.group(0) for m in _PHONE_RE.finditer(text)})
    if len(phones) > 1:
        findings.append(
            Finding(
                severity=Severity.MINOR,
                code="multiple_phone",
                message=f"{len(phones)} phone numbers found; an ATS expects exactly one.",
                evidence=", ".join(phones),
                source="Textkernel Resume Quality 133",
            )
        )
    return findings


def _check_references_and_pii(text: str) -> list[Finding]:
    findings: list[Finding] = []
    if re.search(r"^#*\s*references\b", text, re.IGNORECASE | re.MULTILINE):
        findings.append(
            Finding(
                severity=Severity.MINOR,
                code="references_section",
                message="Resume includes a References section; modern resumes omit it.",
                source="Textkernel Resume Quality 111",
            )
        )
    for m in _PII_RE.finditer(text):
        findings.append(
            Finding(
                severity=Severity.MINOR,
                code="pii_present",
                message=f"Resume appears to include personal information: '{m.group(0)}'",
                evidence=m.group(0),
                source="Textkernel Resume Quality 121-124/312",
            )
        )
    return findings


def _check_page_count(text: str) -> list[Finding]:
    words = len(text.split())
    pages = words / _WORDS_PER_PAGE
    if pages <= 2:
        return []
    return [
        Finding(
            severity=Severity.MINOR,
            code="page_count_estimate",
            message=(
                f"Estimated length is ~{pages:.1f} pages ({words} words); senior-resume "
                "guidance caps a senior resume at two."
            ),
            evidence=f"{words} words",
            source="Senior-resume length guidance (Jan 2025); resume-length study (n=482, 2018)",
        )
    ]


def _check_multiline_dates(roles: list[_Role], lines: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for role in roles:
        if role.employer_line_text is None or role.employer_line_no is None:
            continue
        if parse_range(role.employer_line_text) is not None:
            continue
        next_idx = role.employer_line_no  # 0-indexed index of the line right after it
        if next_idx >= len(lines):
            continue
        combined = f"{role.employer_line_text} {lines[next_idx].strip()}"
        if parse_range(combined) is not None:
            findings.append(
                Finding(
                    severity=Severity.MINOR,
                    code="multiline_date_range",
                    message=f"Date range for '{role.heading_text}' wraps across two lines.",
                    line=role.employer_line_no,
                    evidence=combined,
                    source="Textkernel Resume Quality 418/419",
                )
            )
    return findings


def _check_heading_not_on_own_line(lines: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for idx, line in enumerate(lines, start=1):
        # The first alternative has to exclude '#' itself, or a correct heading
        # ("### Director of IT") matches its own marker and every heading in a
        # clean packet is flagged. Only a marker with real text BEFORE it counts.
        if re.search(r"[^\s#].*#{2,3}\s+\S", line):
            findings.append(
                Finding(
                    severity=Severity.MINOR,
                    code="heading_not_on_own_line",
                    message="A section heading marker appears mid-line rather than alone",
                    line=idx,
                    evidence=line.strip(),
                    source="Textkernel Resume Quality 151",
                )
            )
    return findings


def check_structure(
    resume_md: str,
    *,
    work_history_md: str,
    today: date,
    docx_lines: list[str] | None = None,
) -> list[Finding]:
    """Deterministic structure findings for one generated resume.

    Role/heading/skills structure is only legible in the markdown (the
    exported .docx has no headings, just styled paragraphs), so those checks
    always read `resume_md`. Character-level and text-pattern checks prefer
    `docx_lines` when given -- that is what the ATS actually receives -- and
    fall back to a markdown pre-pass (with known markdown syntax stripped)
    otherwise.
    """
    lines = resume_md.splitlines()
    roles = _parse_roles(lines)
    employers = employer_names(work_history_md)

    findings: list[Finding] = []
    findings.extend(_check_empty_recent_role(roles, today))
    findings.extend(_check_heading_carries_employer(roles, employers))
    findings.extend(_check_role_not_in_history(work_history_md, roles))
    findings.extend(_check_skills_backing(lines))

    char_lines = docx_lines if docx_lines is not None else lines
    findings.extend(_check_banned_characters(char_lines, from_docx=docx_lines is not None))
    findings.extend(_check_encoding_damage(char_lines))

    text_blob = "\n".join(docx_lines) if docx_lines is not None else resume_md
    findings.extend(_check_email_phone(text_blob))
    findings.extend(_check_references_and_pii(text_blob))
    findings.extend(_check_page_count(text_blob))

    findings.extend(_check_multiline_dates(roles, lines))
    findings.extend(_check_heading_not_on_own_line(lines))

    return findings

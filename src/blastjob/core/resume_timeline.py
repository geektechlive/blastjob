"""One role representation for the timeline checks, and three parsers into it.

Why: groundedness proves a resume claim is quoted from the work history, but
nothing asked whether the claim fits the dates of the role it sits under (a
"within five years" build under a three-year role, a "by 2019" result under a
role that ended in 2016). The checks in `core/resume_timeline_checks.py` need
the same shape of role from three places: the work history, a standard
markdown resume and the plain-text ATS resume. Everything here is regex over
text; no model is involved. See docs/how-it-works/tailoring.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from blastjob.core.company import company_key
from blastjob.core.resume_dates import (
    DateRange,
    _date_line,
    employer_names,
    month_indices,
    parse_ranges,
)
from blastjob.core.resume_format import (
    _BULLET_MARKERS,
    _experience_spans,
    _parse_roles,
    _role_employer_field,
)

_PAREN_TAIL_RE = re.compile(r"\s*\([^)]{0,80}\)\s*$")
_ATS_HEADING_RE = re.compile(r"^[A-Z][A-Z &/]*:\s*$")
_ATS_EXPERIENCE_RE = re.compile(r"EXPERIENCE|EMPLOYMENT|CAREER HISTORY")
_MD_EXPERIENCE_RE = re.compile(r"^#{1,2}\s+.*(experience|employment|career history)", re.I | re.M)
_BOLD_SUBTITLE_RE = re.compile(r"^\s*(?:[-*•]\s+)?\*\*[^*]+\*\*")


@dataclass(frozen=True)
class TimelineRole:
    """One role as the timeline checks see it.

    `parsed_ok` False means the checks cannot trust the dates or employer, and
    `reason` says why; such a role is reported as not checked, never skipped
    quietly. `merged` marks a resume role that stacks several titles under one
    heading (more than one range, or bold sub-title lines carrying ranges)."""

    title: str
    employer: str
    key: str
    ranges: tuple[DateRange, ...]
    bullets: tuple[tuple[str, int], ...]
    line: int
    parsed_ok: bool = True
    reason: str = ""
    merged: bool = False

    @property
    def label(self) -> str:
        return f"{self.title} at {self.employer}" if self.employer else self.title

    @property
    def span(self) -> DateRange | None:
        if not self.ranges:
            return None
        return DateRange(
            start=min((r.start for r in self.ranges), key=lambda p: p.sort_key),
            end=max((r.end for r in self.ranges), key=lambda p: p.sort_key),
        )


def _build(
    *,
    title: str,
    employer: str,
    key: str,
    ranges: list[DateRange],
    bullets: list[tuple[str, int]],
    line: int,
    today: date,
    merged: bool = False,
) -> TimelineRole:
    reason = ""
    if not employer:
        reason = "the employer could not be read"
    elif not ranges:
        reason = "no date range could be read"
    elif len(ranges) > 1 and not merged:
        reason = "one heading carries more than one date range"
    elif not merged and month_indices(ranges[0], today) is None:
        reason = "the date range is reversed or in the future"
    return TimelineRole(
        title=title,
        employer=employer,
        key=key,
        ranges=tuple(ranges),
        bullets=tuple(bullets),
        line=line,
        parsed_ok=not reason and not merged,
        reason=reason,
        merged=merged,
    )


def _strip_paren(name: str) -> str:
    return _PAREN_TAIL_RE.sub("", name).strip()


def history_roles(work_history_md: str, today: date) -> list[TimelineRole]:
    """Every role under the work history's experience section(s), in file order."""
    lines = work_history_md.splitlines()
    roles: list[TimelineRole] = []
    for lo, hi in _experience_spans(lines):
        roles.extend(_history_span(lines, lo, hi, today))
    return roles


def _history_span(lines: list[str], lo: int, hi: int, today: date) -> list[TimelineRole]:
    roles: list[TimelineRole] = []
    i = lo
    while i < hi:
        if not lines[i].startswith("### "):
            i += 1
            continue
        heading = lines[i]
        parts = [p.strip() for p in heading[4:].split("|")]
        following = lines[i + 1] if i + 1 < hi else None
        dates = _date_line(following)
        ranges = parse_ranges(f"{heading} | {dates}" if dates else heading)
        bullets: list[tuple[str, int]] = []
        k = i + 1
        while k < hi and not lines[k].startswith("#"):
            stripped = lines[k].strip()
            if stripped.startswith(_BULLET_MARKERS):
                bullets.append((stripped[2:].strip(), k + 1))
            k += 1
        employer = _strip_paren(parts[1]) if len(parts) > 1 else ""
        roles.append(
            _build(
                title=_strip_paren(parts[0]),
                employer=employer,
                key=company_key(employer) if employer else "",
                ranges=ranges,
                bullets=bullets,
                line=i + 1,
                today=today,
            )
        )
        i = k
    return roles


def _match_employer(text: str, names: list[str]) -> str | None:
    lowered = text.lower()
    return next((n for n in sorted(names, key=len, reverse=True) if n.lower() in lowered), None)


def resume_roles(resume_md: str, work_history_md: str, today: date) -> list[TimelineRole]:
    """Roles from a standard markdown resume, else from the plain-text ATS resume."""
    names = employer_names(work_history_md)
    lines = resume_md.splitlines()
    md_roles = _markdown_roles(lines, names, today)
    return md_roles if md_roles else _ats_roles(lines, names, today)


def has_experience_section(resume_md: str) -> bool:
    """True when the resume has a recognisable experience heading in either format."""
    if _MD_EXPERIENCE_RE.search(resume_md):
        return True
    return any(
        _ATS_HEADING_RE.match(line.strip()) and _ATS_EXPERIENCE_RE.search(line)
        for line in resume_md.splitlines()
    )


def _markdown_roles(lines: list[str], names: list[str], today: date) -> list[TimelineRole]:
    out: list[TimelineRole] = []
    for role in _parse_roles(lines):
        field = _role_employer_field(role) or ""
        match = _match_employer(field, names) if field else None
        employer = match or field
        ranges = parse_ranges(role.employer_line_text or "") or parse_ranges(role.heading_text)
        stacked = _has_dated_subtitles(lines, role.heading_line, role.employer_line_no)
        out.append(
            _build(
                title=role.heading_text,
                employer=employer,
                key=company_key(employer) if employer else "",
                ranges=ranges,
                bullets=list(role.bullets),
                line=role.heading_line,
                today=today,
                merged=len(ranges) > 1 or stacked,
            )
        )
    return out


def _has_dated_subtitles(lines: list[str], heading_line: int, employer_line: int | None) -> bool:
    """True when the block under a heading holds bold sub-title lines with ranges."""
    for idx in range(heading_line, len(lines)):
        if lines[idx].startswith("#"):
            break
        if idx + 1 == employer_line:
            continue
        if _BOLD_SUBTITLE_RE.match(lines[idx]) and parse_ranges(lines[idx]):
            return True
    return False


def _next_nonblank(lines: list[str], idx: int) -> str:
    return next((line.strip() for line in lines[idx + 1 :] if line.strip()), "")


def _is_role_title(lines: list[str], idx: int) -> bool:
    """An ALL-CAPS `TITLE:` line whose next line carries the dates is a role, not a section.

    `exporters/ats.py` writes role titles in the same shape as section headings
    ("DIRECTOR OF IT OPERATIONS:"), so only the line that follows tells them apart."""
    if _ATS_EXPERIENCE_RE.search(lines[idx]):
        return False  # the section heading itself, even when a one-line role follows
    following = _next_nonblank(lines, idx)
    return bool(following) and not _is_bullet(following) and bool(parse_ranges(following))


def _ats_section(lines: list[str]) -> list[tuple[int, str]]:
    section: list[tuple[int, str]] = []
    inside = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if _ATS_HEADING_RE.match(stripped) and not _is_role_title(lines, idx):
            inside = bool(_ATS_EXPERIENCE_RE.search(stripped))
            continue
        if inside:
            section.append((idx + 1, stripped))
    return section


def _is_bullet(text: str) -> bool:
    return text.startswith(_BULLET_MARKERS)


def _ats_roles(lines: list[str], names: list[str], today: date) -> list[TimelineRole]:
    """Roles from `EXPERIENCE:` plain text.

    A role is a non-bullet line carrying a date range, optionally preceded by a
    title line, and followed by hyphen bullets. The 80-column wrap can split a
    bullet, so a non-bullet line that is not a role line is a continuation of
    the bullet above it."""
    section = [(n, t) for n, t in _ats_section(lines) if t]
    roles: list[TimelineRole] = []
    pending_title: str | None = None
    open_bullet = False
    for pos, (number, text) in enumerate(section):
        if _is_bullet(text):
            if roles:
                roles[-1] = _with_bullet(roles[-1], text[2:].strip(), number)
                open_bullet = True
            continue
        ranges = parse_ranges(text)
        if ranges:
            roles.append(_ats_role(text, pending_title, ranges, names, number, today))
            pending_title, open_bullet = None, False
            continue
        following = section[pos + 1][1] if pos + 1 < len(section) else ""
        if parse_ranges(following) and not _is_bullet(following):
            pending_title, open_bullet = text.rstrip(": "), False
        elif open_bullet and roles:
            joined = roles[-1].bullets[-1]
            rest = roles[-1].bullets[:-1]
            roles[-1] = _replace_bullets(roles[-1], (*rest, (f"{joined[0]} {text}", joined[1])))
    return roles


def _with_bullet(role: TimelineRole, text: str, number: int) -> TimelineRole:
    return _replace_bullets(role, (*role.bullets, (text, number)))


def _replace_bullets(role: TimelineRole, bullets: tuple[tuple[str, int], ...]) -> TimelineRole:
    return TimelineRole(
        title=role.title,
        employer=role.employer,
        key=role.key,
        ranges=role.ranges,
        bullets=bullets,
        line=role.line,
        parsed_ok=role.parsed_ok,
        reason=role.reason,
        merged=role.merged,
    )


def _ats_role(
    text: str,
    title: str | None,
    ranges: list[DateRange],
    names: list[str],
    number: int,
    today: date,
) -> TimelineRole:
    parts = [p.strip() for p in text.split("|")]
    match = _match_employer(text, names)
    if title is None:
        title = parts[0] if parts and not parse_ranges(parts[0]) else ""
        fallback = parts[1] if len(parts) > 2 else ""
    else:
        fallback = parts[0] if len(parts) > 1 else ""
    employer = match or fallback
    return _build(
        title=title,
        employer=employer,
        key=company_key(employer) if employer else "",
        ranges=ranges,
        bullets=[],
        line=number,
        today=today,
        merged=len(ranges) > 1,
    )

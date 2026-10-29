"""Do a resume's claims fit the dates of the role they sit under? (#105)

Groundedness confirms a claim is quoted from the work history; nothing used to
confirm that it fits the role's dates. On 2026-10-29 one packet passed every
check while a "within five years" build sat under a three-year role, a "by 2019"
result under a role that ended in 2016, and growth claims under a four-month
role. These checks catch that, deterministically (regex over text, no model).

Severity: MAJOR for a contradiction, MINOR for an advisory or a check that did
not run. Never FATAL: `core.packet_report` reserves FATAL for failures with
evidence in the real application path, and regex duration checks will have
false positives. The packet page shows a "Check before sending" banner for
`TIMELINE_CODES` instead; see docs/how-it-works/tailoring.md.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date

from blastjob.core.packet_report import Finding, Severity
from blastjob.core.resume_dates import month_indices
from blastjob.core.resume_timeline import (
    TimelineRole,
    has_experience_section,
    history_roles,
    resume_roles,
)
from blastjob.core.resume_timeline_claims import (
    dated_claims,
    has_growth_figure,
    spanning_wording,
    stated_durations,
)

DURATION_EXCEEDS_ROLE = "duration_exceeds_role"
RESULT_AFTER_ROLE_END = "result_after_role_end"
PROMOTION_LINE_DROPPED = "promotion_line_dropped"
PROMOTION_TITLES_MERGED = "promotion_titles_merged"
SHORT_ROLE_GROWTH = "short_role_growth"
TIMELINE_NOT_CHECKED = "timeline_not_checked"

TIMELINE_CODES = frozenset(
    {
        DURATION_EXCEEDS_ROLE,
        RESULT_AFTER_ROLE_END,
        PROMOTION_LINE_DROPPED,
        PROMOTION_TITLES_MERGED,
        SHORT_ROLE_GROWTH,
        TIMELINE_NOT_CHECKED,
    }
)

# A stated span may run this far past the role before it is called a contradiction.
_TOLERANCE_MONTHS = 3
# A role shorter than this holding a growth figure is worth a second look.
_SHORT_ROLE_MONTHS = 6
# Two spans this close (or less) count as one continuous stretch.
_CONTIGUOUS_GAP_MONTHS = 1
_SOURCE = "issue #105: claims against role dates"
_PROMOTED_RE = re.compile(r"promoted\s+(?:from|to)\b", re.IGNORECASE)
# The title promoted TO is whatever follows the first " to " of the line.
_PROMOTED_TARGET_RE = re.compile(r"^promoted\s+(?:from\s+.+?\s+)?to\s+(?P<tail>.+)", re.IGNORECASE)
# A promotion line states the date of the move, which is the new role's start: it is
# not a result and must never be read against the role it was written under.
_PROMOTED_START_RE = re.compile(r"promoted\b", re.IGNORECASE)

Interval = tuple[int, int]
Peers = Mapping[str, Sequence[Interval]]


def _finding(
    severity: Severity, code: str, message: str, *, line: int | None, evidence: str = ""
) -> Finding:
    return Finding(
        severity=severity,
        code=code,
        message=message,
        line=line,
        evidence=evidence,
        source=_SOURCE,
    )


# ----- intervals ---------------------------------------------------------------


def _intervals(roles: Iterable[TimelineRole], today: date) -> dict[str, list[Interval]]:
    """key -> the distinct month intervals of every role that parsed, oldest first."""
    out: dict[str, set[Interval]] = {}
    for role in roles:
        if not role.parsed_ok or not role.key:
            continue
        idx = month_indices(role.ranges[0], today)
        if idx is not None:
            out.setdefault(role.key, set()).add(idx)
    return {key: sorted(ivs) for key, ivs in out.items()}


def _months_covered(intervals: Iterable[Interval]) -> int:
    """Months covered by the union of `intervals`; overlaps count once, gaps not at all."""
    total = 0
    cur: Interval | None = None
    for first, last in sorted(intervals):
        if cur is not None and first <= cur[1] + 1:
            cur = (cur[0], max(cur[1], last))
            continue
        if cur is not None:
            total += cur[1] - cur[0] + 1
        cur = (first, last)
    return total + (cur[1] - cur[0] + 1 if cur is not None else 0)


def _chain(own: Interval, peers: Sequence[Interval]) -> list[Interval]:
    """The run of same-employer intervals around `own`, joined across gaps of one month or less."""
    ordered = sorted({*peers, own})
    groups: list[list[Interval]] = []
    for iv in ordered:
        if groups and iv[0] <= max(last for _, last in groups[-1]) + 1 + _CONTIGUOUS_GAP_MONTHS:
            groups[-1].append(iv)
        else:
            groups.append([iv])
    return next(g for g in groups if own in g)


def _allowed_months(own: Interval, peers: Sequence[Interval], wording: str) -> int:
    """How many months a bullet may claim, given how it words its reach."""
    span = own[1] - own[0] + 1
    if wording == "chain":
        return max(span, _months_covered(_chain(own, peers)))
    if wording == "both":
        ordered = sorted({*peers, own})
        pos = ordered.index(own)
        neighbours = [ordered[p] for p in (pos - 1, pos + 1) if 0 <= p < len(ordered)]
        return max([span, *(_months_covered([own, nb]) for nb in neighbours)])
    return span


# ----- per-role checks ----------------------------------------------------------


def _not_checked(role: TimelineRole, where: str) -> Finding:
    advice = (
        " Split it into one heading per title so each role's dates can be checked."
        if role.reason.startswith("one heading")
        else ""
    )
    return _finding(
        Severity.MINOR,
        TIMELINE_NOT_CHECKED,
        f"Dates not checked for '{role.label}' in {where}: {role.reason}.{advice} "
        "A role that was not checked is not a role that passed.",
        line=role.line,
        evidence=role.title,
    )


def _check_duration(
    role: TimelineRole, own: Interval, peers: Peers, text: str, line: int
) -> Finding | None:
    durations = stated_durations(text)
    if not durations:
        return None
    top = durations[0]
    allowed = _allowed_months(own, peers.get(role.key, ()), spanning_wording(text))
    if top.months <= allowed + _TOLERANCE_MONTHS:
        return None
    return _finding(
        Severity.MAJOR,
        DURATION_EXCEEDS_ROLE,
        f"'{role.label}' ran about {own[1] - own[0] + 1} months, but this says '{top.text}'. "
        "If the claim covers more than one title, say so (for example 'Across both roles'); "
        "otherwise cut the span or fix the work history.",
        line=line,
        evidence=text,
    )


def _check_result_year(role: TimelineRole, text: str, line: int, today: date) -> Finding | None:
    end = role.ranges[0].end
    if end.present or _PROMOTED_START_RE.match(text):
        return None
    for claim in dated_claims(text, today):
        later_year = claim.point.year > end.year
        later_month = (
            claim.point.year == end.year
            and claim.point.month is not None
            and end.month is not None
            and claim.point.month > end.month
        )
        if later_year or later_month:
            return _finding(
                Severity.MAJOR,
                RESULT_AFTER_ROLE_END,
                f"Says {claim.text}, after this role ended in {end}. Was it a plan or a "
                "measured result? Fix the work history wording, or cut it.",
                line=line,
                evidence=text,
            )
    return None


def _check_role(role: TimelineRole, peers: Peers, today: date) -> list[Finding]:
    own = month_indices(role.ranges[0], today)
    if own is None:
        return []
    findings: list[Finding] = []
    for text, line in role.bullets:
        for found in (
            _check_duration(role, own, peers, text, line),
            _check_result_year(role, text, line, today),
        ):
            if found is not None:
                findings.append(found)
    findings.extend(_check_short_growth(role, own))
    return findings


def _check_short_growth(role: TimelineRole, own: Interval) -> list[Finding]:
    if own[1] - own[0] + 1 >= _SHORT_ROLE_MONTHS:
        return []
    for text, line in role.bullets:
        if has_growth_figure(text):
            return [
                _finding(
                    Severity.MINOR,
                    SHORT_ROLE_GROWTH,
                    f"A growth or revenue figure sits under a role of about "
                    f"{own[1] - own[0] + 1} months ('{role.label}'). If the dates are right, "
                    "expect the question.",
                    line=line,
                    evidence=text,
                )
            ]
    return []


def check_roles(
    roles: Sequence[TimelineRole], peers: Peers, today: date, where: str
) -> list[Finding]:
    """Duration, result-year, short-role-growth and not-checked findings for `roles`."""
    findings: list[Finding] = []
    for role in roles:
        if role.merged or not role.bullets:
            continue
        if not role.parsed_ok:
            findings.append(_not_checked(role, where))
            continue
        findings.extend(_check_role(role, peers, today))
    return findings


# ----- promotion checks ---------------------------------------------------------


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _has_phrase(text: str, phrase: str) -> bool:
    needle = _norm(phrase)
    return bool(needle) and re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", _norm(text)) is not None


def _earlier_title(later: TimelineRole, source: Sequence[TimelineRole], today: date) -> str:
    """Title of the same-employer role that ended up in `later` by promotion, or ""."""
    later_idx = month_indices(later.ranges[0], today) if later.parsed_ok else None
    if later_idx is None:
        return ""
    best: tuple[int, str] | None = None
    for other in source:
        if other is later or other.key != later.key or not other.parsed_ok:
            continue
        idx = month_indices(other.ranges[0], today)
        if idx is not None and idx[0] < later_idx[0] and (best is None or idx[0] > best[0]):
            best = (idx[0], other.title)
    return best[1] if best else ""


def _line_names_both(role: TimelineRole, titles: Sequence[str]) -> bool:
    if not role.bullets:
        return False
    first = role.bullets[0][0]
    return first.lower().startswith("promoted") and all(_has_phrase(first, t) for t in titles)


def _promoted_into(source: Sequence[TimelineRole]) -> list[TimelineRole]:
    """Source roles that some `Promoted ...` bullet promotes INTO, found by the title after "to".

    The bullet may sit under either heading of the pair (the later role says "Promoted
    from X to Y", the earlier one "Promoted to Y"), so the heading it sits under proves
    nothing. The target is the same-employer role whose title is the longest whole-phrase
    match in the text after "to"."""
    targets: list[TimelineRole] = []
    for host in source:
        same_employer = [r for r in source if r.key == host.key]
        if not host.key or len(same_employer) < 2:
            continue
        for text, _ in host.bullets:
            m = _PROMOTED_TARGET_RE.match(text)
            hits = [r for r in same_employer if m and _has_phrase(m.group("tail"), r.title)]
            if hits:
                target = max(hits, key=lambda r: len(r.title))
                if target not in targets:
                    targets.append(target)
    return targets


def check_promotion_lines(
    source: Sequence[TimelineRole], resume: Sequence[TimelineRole], today: date
) -> list[Finding]:
    """A source promotion must lead the later role's resume bullets and name both titles."""
    findings: list[Finding] = []
    for src in _promoted_into(source):
        candidates = [r for r in resume if r.key == src.key and _has_phrase(r.title, src.title)]
        if not candidates:
            continue
        titles = [t for t in (src.title, _earlier_title(src, source, today)) if t]
        if any(_line_names_both(c, titles) for c in candidates):
            continue
        findings.append(
            _finding(
                Severity.MAJOR,
                PROMOTION_LINE_DROPPED,
                f"The work history records a promotion into '{src.label}', but the first "
                "bullet of that role on this resume is not a 'Promoted' line naming "
                f"{' and '.join(repr(t) for t in titles)}.",
                line=candidates[0].line,
                evidence=candidates[0].title,
            )
        )
    return findings


def check_titles_merged(
    source: Sequence[TimelineRole], resume: Sequence[TimelineRole], today: date
) -> list[Finding]:
    """Two titles at one employer must keep separate headings and their own dates."""
    findings = [
        _finding(
            Severity.MAJOR,
            PROMOTION_TITLES_MERGED,
            f"'{role.label}' stacks more than one title and date range under one heading. "
            "Give each title its own heading and dates; an ATS reads a stack as one job.",
            line=role.line,
            evidence=role.title,
        )
        for role in resume
        if role.merged
    ]
    flagged = {f.line for f in findings}
    for key, ivs in _intervals(source, today).items():
        mine = [r for r in resume if r.key == key]
        if len(ivs) < 2 or len(mine) != 1 or mine[0].merged or mine[0].line in flagged:
            continue
        idx = month_indices(mine[0].ranges[0], today) if mine[0].parsed_ok else None
        if idx and idx[0] <= ivs[0][0] and idx[1] >= max(last for _, last in ivs):
            findings.append(
                _finding(
                    Severity.MAJOR,
                    PROMOTION_TITLES_MERGED,
                    f"The work history holds {len(ivs)} titles at {mine[0].employer}, but this "
                    f"resume shows one role ('{mine[0].title}') covering all of them. Show each "
                    "title under its own heading with its own dates.",
                    line=mine[0].line,
                    evidence=mine[0].title,
                )
            )
    return findings


# ----- entry points -------------------------------------------------------------


def check_history(work_history_md: str, today: date) -> list[Finding]:
    """The same date checks on `work_history.md` itself (read-only; nothing is rewritten)."""
    roles = history_roles(work_history_md, today)
    return check_roles(roles, _intervals(roles, today), today, "work_history.md")


def check_resume(resume_md: str, work_history_md: str, today: date) -> list[Finding]:
    """Timeline findings for one generated resume (markdown or ATS plain text)."""
    source = history_roles(work_history_md, today)
    resume = resume_roles(resume_md, work_history_md, today)
    if not resume:
        if not has_experience_section(resume_md):
            return []
        return [
            _finding(
                Severity.MINOR,
                TIMELINE_NOT_CHECKED,
                "No role could be read from this resume's experience section, so no claim "
                "was checked against role dates. That is not an all-clear.",
                line=None,
            )
        ]
    peers = _intervals([*source, *resume], today)
    findings = check_roles(resume, peers, today, "the resume")
    findings.extend(check_promotion_lines(source, resume, today))
    findings.extend(check_titles_merged(source, resume, today))
    return findings

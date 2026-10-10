"""Read a tailored packet's `{role_slug}.packet_report.json` back into the cockpit.

Every build already runs the deterministic checks in `core/build.py`
(`_verify_packet` -> `packet_gate.gate`) and writes the verdict beside the
documents. Nothing in the app has ever read it back: the sidecar exists, the
gate decides `ready` vs `quarantined`, and none of that reaches the browser.
By this project's own rule (see CLAUDE.md, "Web is the only surface") a
capability with no route does not exist — so this module is the read side of
that file, following the exact pattern `web/fitscore.py` uses for
`fit_score.json`: never raise, degrade to "nothing to show" on anything
unreadable, and let the caller resolve the packet directory and role slug
(two output layouts exist on disk; this module never recomputes either).

SEVERITY VOCABULARY comes straight from `core/packet_report.py` — read that
module's docstring before changing how findings are grouped here. FATAL is
reserved for failures with evidence in the user's actual application path
(a logged Workday rejection, shipped encoding damage, an unreconciled total);
MINOR is everything Textkernel-derived, which is advisory because Textkernel's
own parser sits behind none of the boards the user applies through.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from blastjob.web.sidecars import find_sidecars, mismatch_note

# Human labels for `Artifact` codes, as they should read in the cockpit.
_ARTIFACT_LABELS: dict[str, str] = {
    "resume": "resume",
    "cover_letter": "cover letter",
}


@dataclass(frozen=True)
class FindingView:
    """One `Finding` from the sidecar, shaped for display. All fields are
    plain strings — Jinja autoescapes them, so nothing here is trusted."""

    severity: str = ""
    code: str = ""
    message: str = ""
    artifact: str = ""
    line: int | None = None
    evidence: str = ""
    source: str = ""


@dataclass(frozen=True)
class CoverageGapView:
    """One `CoverageGap` from the sidecar: true material work_history.md
    proves that the generated resume did not use. `history_quote` must be
    rendered verbatim, marked as the user's own words, never a model's
    paraphrase — see `core/packet_report.CoverageGap`."""

    requirement: str = ""
    history_quote: str = ""
    priority: str = "must"


@dataclass(frozen=True)
class PacketReportView:
    """What the cockpit shows for one packet's verification report.

    `groundedness`, `jd_phrasing`, and `cover_letter_groundedness` are the
    same uncalibrated scores `fitscore.GroundednessSummary` carries — display
    them with the same caveat, never as a pass/fail gate. `jd_phrasing` in
    particular is NOT a fit score: it measures how far the resume's wording
    echoes the posting's own wording, and presenting it as fit has already
    misled a reader once (see `core/packet_report.py`).
    """

    state: str = ""
    sendable: bool = False
    reasons: tuple[str, ...] = ()
    groundedness: int | None = None
    jd_phrasing: int | None = None
    cover_letter_groundedness: int | None = None
    fatal: tuple[FindingView, ...] = ()
    major: tuple[FindingView, ...] = ()
    minor: tuple[FindingView, ...] = ()
    fatal_count: int = 0
    major_count: int = 0
    minor_count: int = 0
    source_path: str = ""
    artifact_dir: str = ""
    # Which checks ran, which crashed, which sat out. A packet whose checks
    # did not run is NOT a clean packet -- see `core/packet_report.PacketReport`.
    checks_run: tuple[str, ...] = ()
    checks_failed: tuple[str, ...] = ()
    checks_skipped: tuple[str, ...] = ()
    # Human-readable labels ("resume", "cover letter"), already mapped from
    # the raw Artifact codes so the template never has to know the mapping.
    quarantined_artifacts: tuple[str, ...] = ()
    coverage_gaps: tuple[CoverageGapView, ...] = ()


def _candidates(directory: Path, role_slug: str) -> list[Path]:
    """Report files in the packet, best guess first.

    `core.build` always writes `{role_slug}.packet_report.json` — but a
    shared company-day folder can hold more than one role's sidecars, and the
    caller's role-slug resolution can fail (e.g. a title edited after
    tailoring), so this falls back to any report in the folder rather than
    showing nothing. Thin wrapper over the shared `web.sidecars.find_sidecars`
    (consolidated 2026-09-17, see that module's docstring) — callers here
    still want a plain path list, `load_packet_report` is the one that needs
    the role each pick actually belongs to.
    """
    return [p.path for p in find_sidecars(directory, ".packet_report.json", role_slug)]


def _int_or_none(value: object) -> int | None:
    # bool is an int subclass in Python; a stray `true`/`false` in the JSON
    # must not be mistaken for a score.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _finding_view(raw: object) -> FindingView | None:
    if not isinstance(raw, dict):
        return None
    return FindingView(
        severity=str(raw.get("severity") or ""),
        code=str(raw.get("code") or ""),
        message=str(raw.get("message") or ""),
        artifact=str(raw.get("artifact") or ""),
        line=_int_or_none(raw.get("line")),
        evidence=str(raw.get("evidence") or ""),
        source=str(raw.get("source") or ""),
    )


def _coverage_gap_view(raw: object) -> CoverageGapView | None:
    if not isinstance(raw, dict):
        return None
    return CoverageGapView(
        requirement=str(raw.get("requirement") or ""),
        history_quote=str(raw.get("history_quote") or ""),
        priority=str(raw.get("priority") or "must"),
    )


def _str_tuple(raw: object) -> tuple[str, ...]:
    return tuple(str(x) for x in raw) if isinstance(raw, list) else ()


def load_packet_report(
    packet_dir: str | None, *, role_slug: str | None = ""
) -> PacketReportView | None:
    """Load and shape the packet report in `packet_dir`, or None.

    None covers every case where there is nothing honest to show: no
    directory, no sidecar (a packet built before reports existed, or one
    whose role slug this call couldn't resolve), a truncated or malformed
    file, or a file that isn't the shape this module expects. This function
    never raises — a broken checker output must not cost the user the page that
    displays it.
    """
    if not packet_dir:
        return None
    try:
        directory = Path(packet_dir)
        if not directory.is_dir():
            return None
        picks = find_sidecars(directory, ".packet_report.json", role_slug)
        if not picks:
            return None
        path = picks[0].path
        warning = mismatch_note(picks, role_slug)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None

        raw_findings = data.get("findings")
        findings = raw_findings if isinstance(raw_findings, list) else []

        fatal: list[FindingView] = []
        major: list[FindingView] = []
        minor: list[FindingView] = []
        for raw in findings:
            finding = _finding_view(raw)
            if finding is None:
                continue
            if finding.severity == "fatal":
                fatal.append(finding)
            elif finding.severity == "major":
                major.append(finding)
            else:
                # Anything else (including an unrecognized severity string) is
                # treated as advisory rather than dropped — losing a finding
                # silently is worse than misfiling it as minor.
                minor.append(finding)

        raw_reasons = data.get("reasons")
        reasons = tuple(str(r) for r in raw_reasons) if isinstance(raw_reasons, list) else ()
        if warning:
            reasons = (warning, *reasons)

        gaps_field = data.get("coverage_gaps")
        raw_gaps: list[object] = gaps_field if isinstance(gaps_field, list) else []
        coverage_gaps = tuple(g for raw in raw_gaps if (g := _coverage_gap_view(raw)) is not None)

        raw_qa = data.get("quarantined_artifacts")
        quarantined_artifacts = tuple(
            _ARTIFACT_LABELS.get(str(a), str(a))
            for a in (raw_qa if isinstance(raw_qa, list) else [])
        )

        return PacketReportView(
            state=str(data.get("state") or ""),
            sendable=bool(data.get("sendable", False)),
            reasons=reasons,
            groundedness=_int_or_none(data.get("groundedness")),
            jd_phrasing=_int_or_none(data.get("jd_phrasing")),
            cover_letter_groundedness=_int_or_none(data.get("cover_letter_groundedness")),
            fatal=tuple(fatal),
            major=tuple(major),
            minor=tuple(minor),
            fatal_count=len(fatal),
            major_count=len(major),
            minor_count=len(minor),
            source_path=str(path),
            artifact_dir=str(data.get("artifact_dir") or ""),
            checks_run=_str_tuple(data.get("checks_run")),
            checks_failed=_str_tuple(data.get("checks_failed")),
            checks_skipped=_str_tuple(data.get("checks_skipped")),
            quarantined_artifacts=quarantined_artifacts,
            coverage_gaps=coverage_gaps,
        )
    except Exception:  # noqa: BLE001 — malformed/missing sidecar is "nothing to show"
        return None


def record_override(
    packet_dir: str | Path,
    *,
    reason: str,
    role_slug: str = "",
    now: datetime | None = None,
) -> bool:
    """Write the user's acknowledged override into the packet's sidecar.

    Reuses `_candidates()` so the write lands on the file the reader would
    read — a company-day folder can hold several roles' sidecars. Refuses
    (returns False, never raises) unless the stored `state` is exactly
    "quarantined": an override is only meaningful against a packet the gate
    actually held, and record-keeping must never silently invent a state
    transition the gate never made.

    On success, sets `state: acknowledged_override`, `sendable: true`, and
    an `override: {reason, at}` block, while leaving `reasons` untouched —
    why the packet was held stays on the record even after it is overridden.

    This does not move any files: `core/quarantine.release()` is the file
    move, this is the sidecar's record of the decision. The two must be
    called together (see this module's caller in `web/app.py`).

    The write is atomic: a tmp file in the same directory, then
    `os.replace()`, so a crash mid-write can never leave a truncated sidecar.
    """
    directory = Path(packet_dir)
    if not directory.is_dir():
        return False
    candidates = _candidates(directory, role_slug)
    if not candidates:
        return False
    path = candidates[0]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("state") != "quarantined":
        return False

    when = now or datetime.now()
    data["state"] = "acknowledged_override"
    data["sendable"] = True
    data["override"] = {"reason": reason, "at": when.isoformat()}

    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True

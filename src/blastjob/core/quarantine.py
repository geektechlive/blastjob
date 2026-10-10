"""Make QUARANTINED mean something on disk.

`core/packet_gate.gate()` can return `PacketState.QUARANTINED`, but a verdict
recorded only in `{role-slug}.packet_report.json` changes nothing about what
sits in the packet folder: the resume and cover letter stay right where they
were, next to a working download link. A badge beside a sendable file is the
exact defect the packet-quality program (see CLAUDE.md's "Packet verification
(2026-09-16)") was built to end.

This module is the enforcement half. `quarantine()` physically moves the
GENERATED documents (not the sidecars — the job description, research,
scores and packet report are the evidence the user needs to decide, and they
stay put) into a `quarantine/` subfolder and writes down why. `release()` is
the user's acknowledged override (`PacketState.ACKNOWLEDGED_OVERRIDE` in
`core/packet_report.py`): a manual decision always beats automation in this
codebase, but here it gets written to `RELEASED.txt` so the override is an
audit trail, not a silent bypass.

Deliberately dumb about *why* a packet was quarantined — `reasons` is just
text handed in by the caller (typically `verdict.reasons` from
`packet_gate.gate()`). This module only knows how to move files safely and
record what happened.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# The generated-document extensions a build can produce for one stem. Deliberately
# NOT .pdf: PDF export (core/build.py) is already fail-soft/best-effort and, per
# this workstream's brief, out of scope here — flagged as an open issue in this
# module's integration report rather than silently included or silently dropped.
# .pdf included deliberately: build.py can export one (fail-soft), and a working
# PDF download sitting beside the moved .md/.docx would be exactly the defect
# quarantine exists to close.
DOCUMENT_EXTENSIONS: tuple[str, ...] = (".md", ".docx", ".ats.txt", ".pdf")

_QUARANTINE_DIRNAME = "quarantine"
_WHY_FILENAME = "WHY.txt"
_RELEASED_FILENAME = "RELEASED.txt"


class UnsafeStemError(ValueError):
    """Raised when a stem could escape `packet_dir` — refused, never followed.

    This module moves files on the user's real disk; a stem is a bare filename
    component (e.g. "Alexander_Rivera_Resume_-_Acme_VP_IT"), never a path.
    """


def _validate_stem(stem: str) -> None:
    if not stem or not stem.strip():
        raise UnsafeStemError("quarantine: stem must not be empty")
    candidate = Path(stem)
    if candidate.is_absolute():
        raise UnsafeStemError(f"quarantine: refusing absolute stem {stem!r}")
    if ".." in candidate.parts:
        raise UnsafeStemError(f"quarantine: refusing path-traversal stem {stem!r}")
    if candidate.name != stem:
        raise UnsafeStemError(f"quarantine: refusing stem with a path separator {stem!r}")


def _quarantine_dir(packet_dir: Path) -> Path:
    return packet_dir / _QUARANTINE_DIRNAME


def quarantined_files(packet_dir: Path) -> list[Path]:
    """Documents currently sitting in `packet_dir`'s quarantine folder.

    Excludes the `WHY.txt` marker itself, which may legitimately remain after
    a `release()` as the historical record of why the packet was quarantined
    in the first place.
    """
    qdir = _quarantine_dir(packet_dir)
    if not qdir.is_dir():
        return []
    return sorted(p for p in qdir.iterdir() if p.is_file() and p.name != _WHY_FILENAME)


def is_quarantined(packet_dir: Path) -> bool:
    """True while at least one generated document is held in quarantine.

    Deliberately keyed on the documents themselves, not on `WHY.txt`'s mere
    existence: after `release()` moves the documents back, `WHY.txt` may
    still be there as history, and the packet is no longer quarantined.
    """
    return bool(quarantined_files(packet_dir))


def _format_why(reasons: list[str], now: datetime) -> str:
    lines = [
        f"Quarantined at {now.isoformat()}",
        "",
        "The generated resume and cover letter were moved out of this folder",
        "because this packet is not sendable as-is. The job description,",
        "research, scores and packet report stay here — they are the evidence",
        "for deciding what to do next.",
        "",
        "Reasons:",
    ]
    if reasons:
        lines.extend(f"- {reason}" for reason in reasons)
    else:
        lines.append("- (no reasons given)")
    lines.append("")
    return "\n".join(lines)


def quarantine(
    packet_dir: Path,
    *,
    stems: list[str],
    reasons: list[str],
    now: datetime | None = None,
) -> Path:
    """Move the generated documents named by `stems` into `packet_dir/quarantine`.

    For each stem, every extension in `DOCUMENT_EXTENSIONS` is tried; a file
    that does not exist is skipped, not an error (a failed DOCX export, or an
    ATS stem that was never generated, must not block quarantining the rest
    of the packet). A destination file that already exists is left alone —
    never silently overwritten — and the source stays where it was.

    Idempotent: if the packet is already quarantined (`is_quarantined`),
    this returns the existing quarantine directory untouched — no re-move,
    no rewritten `WHY.txt` — so calling it twice (e.g. a retried build) is a
    true no-op rather than clobbering the original timestamp and reasons.

    Every stem is validated first (see `_validate_stem`); an unsafe stem
    raises `UnsafeStemError` before anything on disk is touched, whether or
    not it is the first or the fifth stem in the list.
    """
    for stem in stems:
        _validate_stem(stem)

    if is_quarantined(packet_dir):
        return _quarantine_dir(packet_dir)

    when = now or datetime.now()
    qdir = _quarantine_dir(packet_dir)
    qdir.mkdir(parents=True, exist_ok=True)

    for stem in stems:
        for ext in DOCUMENT_EXTENSIONS:
            src = packet_dir / f"{stem}{ext}"
            if not src.is_file():
                continue
            dest = qdir / src.name
            if dest.exists():
                log.warning(
                    "quarantine: destination already exists, leaving source in place: %s", dest
                )
                continue
            src.rename(dest)

    (qdir / _WHY_FILENAME).write_text(_format_why(list(reasons), when), encoding="utf-8")
    return qdir


def release(packet_dir: Path, *, reason: str, now: datetime | None = None) -> list[Path]:
    """The user's acknowledged override: move every quarantined document back
    into `packet_dir` and append a dated, reasoned line to `RELEASED.txt`.

    A manual decision always beats automation in this codebase (see
    CLAUDE.md's "Truth guarantees"), but it is written down — `RELEASED.txt`
    is append-only, so a packet quarantined and released more than once
    keeps its whole history rather than the latest line only.

    Returns the list of documents' new (packet_dir) paths, in the order they
    were moved. If nothing is quarantined, this is a no-op that returns an
    empty list — there is nothing to release and nothing to record.
    """
    files = quarantined_files(packet_dir)
    if not files:
        return []

    when = now or datetime.now()
    moved: list[Path] = []
    for src in files:
        dest = packet_dir / src.name
        if dest.exists():
            log.warning(
                "release: destination already exists, leaving quarantined copy in place: %s",
                dest,
            )
            continue
        src.rename(dest)
        moved.append(dest)

    with (packet_dir / _RELEASED_FILENAME).open("a", encoding="utf-8") as f:
        f.write(f"{when.isoformat()} — released: {reason}\n")

    return moved

"""Make QUARANTINED mean something on disk.

`core/packet_gate.gate()` can return `PacketState.QUARANTINED`, but a verdict
recorded only in `{role-slug}.packet_report.json` changes nothing about what
sits in the packet folder: the resume and cover letter stay right where they
were, next to a working download link. A badge beside a sendable file is the
exact defect the packet-quality program (see CLAUDE.md's "Packet verification
(2026-09-16)") was built to end.

This module is the enforcement half. `quarantine()` physically moves the
GENERATED documents (not the sidecars -- the job description, research,
scores and packet report are the evidence the user needs to decide, and they
stay put) into a `quarantine/` subfolder and writes down why. `release()` is
the user's acknowledged override (`PacketState.ACKNOWLEDGED_OVERRIDE` in
`core/packet_report.py`): a manual decision always beats automation in this
codebase, but here it gets written to `RELEASED.txt` so the override is an
audit trail, not a silent bypass.

The packet folder is `{company}-{date}` and is SHARED by every role tailored
for that company that day, so everything here works per STEM (one role's
resume or letter), never per folder: a second failing role must be held even
when the first already is, and releasing one role must not free another's.

FAIL-CLOSED. After `quarantine()` returns, every targeted document is absent
from the public folder; if one could not be moved it raises
`QuarantineIncompleteError` naming it, so the caller can refuse to call the
packet done. As a second line, `held_names()` lists the files a QUARANTINED
`packet_report.json` names, and the download resolver refuses those wherever
they sit.

Deliberately dumb about *why* a packet was quarantined -- `reasons` is just
text handed in by the caller (typically `verdict.reasons` from
`packet_gate.gate()`). This module only knows how to move files safely and
record what happened.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from blastjob.core.paths import document_tokens_regex

log = logging.getLogger(__name__)

# The generated-document extensions a build can produce for one stem. .pdf is
# included deliberately: build.py can export one (fail-soft), and a working PDF
# download sitting beside the moved .md/.docx would be exactly the defect
# quarantine exists to close. The ATS export is `{stem}_ATS.ats.txt`
# (`exporters/ats.py`), i.e. stem `X_ATS` + `.ats.txt`.
DOCUMENT_EXTENSIONS: tuple[str, ...] = (".md", ".docx", ".ats.txt", ".pdf")

_QUARANTINE_DIRNAME = "quarantine"
# A held document replaced by a newer one of the same name is kept here, out of
# `quarantined_files()` (which lists the top level only) so `release()` never
# returns a superseded version to the public folder.
_SUPERSEDED_DIRNAME = "superseded"
_WHY_FILENAME = "WHY.txt"
RELEASED_FILENAME = "RELEASED.txt"
_REPORT_SUFFIX = ".packet_report.json"
_LEGACY_REPORT = "packet_report.json"
_QUARANTINED_STATE = "quarantined"


class UnsafeStemError(ValueError):
    """Raised when a stem could escape `packet_dir` -- refused, never followed.

    This module moves files on the user's real disk; a stem is a bare filename
    component (e.g. "Alexander_Rivera_Resume_-_Acme_VP_IT"), never a path.
    """


class QuarantineIncompleteError(RuntimeError):
    """A targeted document is still in the public folder after quarantine().

    The packet must NOT be treated as done: a held document next to a working
    download link is the defect quarantine exists to end."""

    def __init__(self, leftovers: list[str]) -> None:
        self.leftovers = leftovers
        super().__init__("could not quarantine: " + ", ".join(leftovers))


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


def targeted_files(packet_dir: Path, stems: list[str]) -> list[str]:
    """Names of the documents of `stems` currently in the PUBLIC folder."""
    for stem in stems:
        _validate_stem(stem)
    return [
        f"{stem}{ext}"
        for stem in stems
        for ext in DOCUMENT_EXTENSIONS
        if (packet_dir / f"{stem}{ext}").is_file()
    ]


def _format_why(stems: list[str], reasons: list[str], now: datetime) -> str:
    lines = [
        f"Quarantined at {now.isoformat()}",
        "",
        f"Held: {', '.join(stems)}",
        "",
        "The generated documents named above were moved out of this folder",
        "because this packet is not sendable as-is. The job description,",
        "research, scores and packet report stay here -- they are the evidence",
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


def _supersede(qdir: Path, existing: Path, when: datetime) -> None:
    """Park an already-held file under `superseded/` so a newer one can take its
    name. Never overwrites and never deletes: both are evidence."""
    parked = qdir / _SUPERSEDED_DIRNAME
    parked.mkdir(exist_ok=True)
    stamp = when.strftime("%Y%m%d-%H%M%S")
    target = parked / f"{existing.name}.{stamp}"
    n = 1
    while target.exists():
        n += 1
        target = parked / f"{existing.name}.{stamp}-{n}"
    os.replace(existing, target)


def _move_one(src: Path, qdir: Path, when: datetime) -> bool:
    """Move `src` into `qdir`. True when it moved; False (and logged) when not."""
    dest = qdir / src.name
    try:
        if dest.exists():
            _supersede(qdir, dest, when)
        os.replace(src, dest)
    except OSError:
        log.exception("quarantine: could not move %s", src)
        return False
    return True


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
    of the packet). A document already held under the same name is parked in
    `quarantine/superseded/` (never overwritten, never deleted) so the newer
    one can take its place: a same-day re-tailor of a held role must end held,
    not public.

    Idempotent PER STEM: a stem whose documents are already out of the public
    folder moves nothing and writes nothing, so a retried build neither
    clobbers the original timestamp nor re-opens a role; a different stem in
    the same folder is still held. Every stem that does move appends its own
    entry to `WHY.txt`.

    Postcondition: no targeted document remains in `packet_dir`. If one does
    (a failed rename), `QuarantineIncompleteError` is raised after the others
    were moved and recorded.

    Every stem is validated first (see `_validate_stem`); an unsafe stem
    raises `UnsafeStemError` before anything on disk is touched.
    """
    for stem in stems:
        _validate_stem(stem)

    when = now or datetime.now()
    qdir = _quarantine_dir(packet_dir)
    moved_stems: list[str] = []
    for stem in stems:
        sources = [
            packet_dir / f"{stem}{ext}"
            for ext in DOCUMENT_EXTENSIONS
            if (packet_dir / f"{stem}{ext}").is_file()
        ]
        if not sources:
            continue
        qdir.mkdir(parents=True, exist_ok=True)
        # A list, not a generator: any() would stop at the first move.
        if any([_move_one(src, qdir, when) for src in sources]):
            moved_stems.append(stem)

    if moved_stems:
        with (qdir / _WHY_FILENAME).open("a", encoding="utf-8") as f:
            f.write(_format_why(moved_stems, list(reasons), when) + "\n")

    leftovers = targeted_files(packet_dir, stems)
    if leftovers:
        raise QuarantineIncompleteError(leftovers)
    return qdir


def _belongs_to(name: str, stems: list[str]) -> bool:
    """True for a stem's documents, incl. its `_ATS` export and `.vN.md` rotations."""
    return any(name.startswith((f"{s}.", f"{s}_ATS.")) for s in stems)


def release(
    packet_dir: Path,
    *,
    reason: str,
    stems: list[str] | None = None,
    now: datetime | None = None,
) -> list[Path]:
    """The user's acknowledged override: move quarantined documents back into
    `packet_dir` and append a dated, reasoned line to `RELEASED.txt`.

    With `stems`, only those roles' documents return (the packet folder is
    shared, so one role's override must not free another role's held letter);
    without, every held document does.

    A manual decision always beats automation in this codebase (see
    CLAUDE.md's "Truth guarantees"), but it is written down -- `RELEASED.txt`
    is append-only, so a packet quarantined and released more than once
    keeps its whole history rather than the latest line only.

    Returns the list of documents' new (packet_dir) paths, in the order they
    were moved. If nothing is quarantined, this is a no-op that returns an
    empty list -- there is nothing to release and nothing to record.
    """
    if stems is not None:
        for stem in stems:
            _validate_stem(stem)
    files = [
        f for f in quarantined_files(packet_dir) if stems is None or _belongs_to(f.name, stems)
    ]
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

    with (packet_dir / RELEASED_FILENAME).open("a", encoding="utf-8") as f:
        names = ", ".join(p.name for p in moved)
        f.write(f"{when.isoformat()} -- released: {reason} [{names}]\n")

    return moved


def undo_release(packet_dir: Path, moved: list[Path], *, why: str) -> list[Path]:
    """Put just-released documents back into `quarantine/` (a release whose override
    could not be recorded), and note it in `RELEASED.txt` so the log stays true.

    Returns the documents that could NOT be put back (still public)."""
    qdir = _quarantine_dir(packet_dir)
    qdir.mkdir(exist_ok=True)
    stuck: list[Path] = []
    restored: list[str] = []
    for path in moved:
        try:
            os.replace(path, qdir / path.name)
            restored.append(path.name)
        except OSError:
            log.exception("undo_release: could not re-hold %s", path)
            stuck.append(path)
    with (packet_dir / RELEASED_FILENAME).open("a", encoding="utf-8") as f:
        f.write(
            f"{datetime.now().isoformat()} -- release rolled back: {why} [{', '.join(restored)}]\n"
        )
    return stuck


# --- which files a QUARANTINED report says are unsafe ---------------------------


class _ReportHold(BaseModel):
    """The slice of `{slug}.packet_report.json` the download guard reads."""

    model_config = ConfigDict(extra="ignore")

    state: str = ""
    quarantined_artifacts: list[str] = Field(default_factory=list)
    quarantined_files: list[str] | None = None


def _legacy_names(packet_dir: Path, slug: str, artifacts: list[str]) -> set[str]:
    """Public documents a report from before `quarantined_files` existed held.

    Derived from the role's `{slug}.metadata.json` company/role and the file
    naming rules, filtered by the artifact kinds the report quarantined. This
    closes the existing cases: a second failing role used to be a silent
    no-op and stayed public under a QUARANTINED report."""
    meta = packet_dir / (f"{slug}.metadata.json" if slug else "metadata.json")
    try:
        data = _read_json(meta)
        company, role = str(data.get("company") or ""), str(data.get("role") or "")
    except (OSError, ValueError):
        return set()
    if not company or not role:
        return set()
    kinds = {"resume": "Resume", "cover_letter": "Cover_Letter"}
    wanted = [kinds[a] for a in artifacts if a in kinds]
    if not wanted:
        return set()
    tokens = document_tokens_regex(company, role)
    pattern = re.compile(
        rf"_(?:{'|'.join(wanted)})_-_{tokens}(?:_ATS)?(?:\.v\d+)?(?:\.ats)?\.[A-Za-z]+$"
    )
    return {p.name for p in packet_dir.iterdir() if p.is_file() and pattern.search(p.name)}


def _read_json(path: Path) -> dict:
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return data


def held_names(packet_dir: Path) -> frozenset[str]:
    """Names of public files that a QUARANTINED packet report says are unsafe.

    Scans every `*.packet_report.json` (the folder is shared by roles) plus a
    bare legacy `packet_report.json`. A report whose state is anything but
    `quarantined` (ready, or the user's acknowledged override) holds nothing.
    An unreadable or malformed report names nothing: guessing would let a
    corrupt sidecar lock every document in the folder, and the `quarantine/`
    folder itself still keeps held documents out of the public listing."""
    if not packet_dir.is_dir():
        return frozenset()
    held: set[str] = set()
    reports = sorted(packet_dir.glob(f"*{_REPORT_SUFFIX}"))
    if (packet_dir / _LEGACY_REPORT).is_file():
        reports.append(packet_dir / _LEGACY_REPORT)
    for path in reports:
        slug = path.name[: -len(_REPORT_SUFFIX)] if path.name != _LEGACY_REPORT else ""
        try:
            hold = _ReportHold.model_validate(_read_json(path))
        except (OSError, ValueError, ValidationError):
            log.warning("held_names: unreadable packet report %s", path)
            continue
        if hold.state != _QUARANTINED_STATE:
            continue
        if hold.quarantined_files is not None:
            held.update(hold.quarantined_files)
        else:
            held.update(_legacy_names(packet_dir, slug, hold.quarantined_artifacts))
    return frozenset(held)

"""Shared discovery of packet sidecar files (`*.fit_score.json`, `*.packet_report.json`).

Consolidated 2026-09-17: `web/fitscore.py` and `web/packet_view.py` each carried
an identical "sort candidates, preferred role first" helper differing only in
glob pattern and whether a bare (role-less) file was also accepted, and
`web/stage_moves.py` did its own ad hoc glob-and-load with no role preference
at all. Do not re-fork this — add a suffix/bare-file argument here if a new
sidecar type needs one.

**Bug fixed while consolidating:** the old fallback silently returned ANY
matching sidecar in the directory when the preferred `{role_slug}<suffix>`
file did not exist. A company-day folder holds several roles' sidecars, so a
caller could show one role's verdict while a page labeled for another role —
a report can now say "showing the report for <other role>" instead of lying
by omission. `find_sidecars` returns `SidecarPick`s carrying the role slug
parsed off each filename and whether it is the one the caller actually asked
for; `mismatch_note` turns that into the one sentence callers need.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SidecarPick:
    """One sidecar file resolved out of a packet directory."""

    path: Path
    # Role slug parsed off the filename ("" for a bare file with no role prefix,
    # e.g. the refine layout's bare `fit_score.json`).
    role_slug: str
    # True iff this is the file the caller's requested role_slug actually names.
    is_preferred: bool


def find_sidecars(
    directory: Path,
    suffix: str,
    role_slug: str | None,
    *,
    bare_name: str | None = None,
) -> list[SidecarPick]:
    """All sidecars in `directory` matching `*{suffix}`, preferred role first.

    `suffix` is the filename tail after the role slug, e.g. ".fit_score.json"
    or ".packet_report.json". `bare_name`, when given, also accepts a
    role-less file (e.g. "fit_score.json" for the refine layout), sorted
    after any role-prefixed match. Results are otherwise sorted by filename so
    a directory with several sidecars resolves the same way on every request.
    """
    found = sorted(directory.glob(f"*{suffix}"))
    picks = [
        SidecarPick(path=p, role_slug=p.name[: -len(suffix)], is_preferred=False) for p in found
    ]
    if bare_name:
        bare = directory / bare_name
        if bare.is_file():
            picks.append(SidecarPick(path=bare, role_slug="", is_preferred=False))
    if not role_slug:
        return picks
    preferred_name = f"{role_slug}{suffix}"
    picks = [
        SidecarPick(path=p.path, role_slug=p.role_slug, is_preferred=p.path.name == preferred_name)
        for p in picks
    ]
    picks.sort(key=lambda p: not p.is_preferred)
    return picks


def mismatch_note(picks: list[SidecarPick], role_slug: str | None) -> str:
    """One sentence when the top pick is NOT the role the caller asked for.

    Empty string when there is nothing to warn about: no picks, no requested
    role, or the top pick is exactly the requested role's file.
    """
    if not picks or not role_slug or picks[0].is_preferred:
        return ""
    other = picks[0].role_slug or "an unlabeled packet"
    return f"Showing the report for {other}, not {role_slug} — no sidecar was found for it."

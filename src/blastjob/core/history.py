from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from blastjob.core.tracking import load_tracking


@dataclass
class HistoryEntry:
    path: Path
    date: str
    company: str
    role: str
    cost_usd: float | None
    total_tokens: int
    cache_hit_ratio: float | None
    timestamp: str
    formats: list[str]
    ats_mode: bool
    status: str = "drafted"
    applied_at: str | None = None
    next_action: str = ""
    next_action_due: str | None = None
    notes: str = ""


# Sort priority: live applications first, then drafted, then closed.
_STATUS_RANK = {
    "interview": 0,
    "screen": 1,
    "offer": 2,
    "applied": 3,
    "drafted": 4,
    "rejected": 5,
    "ghosted": 6,
    "withdrawn": 7,
}


def _find_metadata_files(output_root: Path) -> list[Path]:
    """Match both filename conventions on disk.

    Legacy packets (nested {date}/{company}/{role}/ dirs) write a bare
    `metadata.json`. core/build.py's current convention (a flat
    {company-slug}-{date}/ folder shared by every role tailored for that
    company that day) writes it role-slug-prefixed as `{role_slug}.metadata.json`
    instead, so multiple builds in one folder don't collide. The `*.metadata.json`
    glob cannot also match the bare legacy name (no leading dot to satisfy `*`
    matching zero characters against nothing), so the two sets never overlap.
    """
    legacy = output_root.rglob("metadata.json")
    current = output_root.rglob("*.metadata.json")
    return sorted({*legacy, *current})


def _cost_or_unknown(data: dict) -> float | None:
    """Cost from a packet's metadata, or None when it was never really known.

    A legacy `cost_usd` of exactly 0.0 is treated as unknown rather than free.
    Until this session the claude-CLI provider -- the default, and what the user
    actually runs -- hardcoded zero usage, so every packet it wrote recorded
    0.0 regardless of what it cost. Rendering that as "$0.00" would keep
    presenting a fabricated number as a measurement, which is the whole defect.
    A build that genuinely cost nothing does not exist.
    """
    if "build_cost_usd" in data:
        return data["build_cost_usd"]
    legacy = data.get("cost_usd")
    return None if not legacy else legacy


def _ratio_or_unknown(data: dict) -> float | None:
    """Cache hit ratio, with the same legacy-zero caveat as `_cost_or_unknown`."""
    if "build_cache_hit_ratio" in data:
        return data["build_cache_hit_ratio"]
    legacy = data.get("cache_hit_ratio")
    return None if not legacy else legacy


def scan_history(output_root: Path) -> list[HistoryEntry]:
    entries: list[HistoryEntry] = []
    if not output_root.exists():
        return entries

    for meta_file in _find_metadata_files(output_root):
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            run_dir = meta_file.parent
            tracking = load_tracking(run_dir)
            entries.append(
                HistoryEntry(
                    path=run_dir,
                    date=data.get("timestamp", "")[:10],
                    company=data.get("company", run_dir.parent.name),
                    role=data.get("role", run_dir.name),
                    # Prefer the whole-build figure; fall back to the legacy key
                    # so packets written before the split still read. None means
                    # the provider reported no usage -- NOT that it was free.
                    cost_usd=_cost_or_unknown(data),
                    total_tokens=(
                        data.get("build_input_tokens", data.get("input_tokens", 0))
                        + data.get("build_output_tokens", data.get("output_tokens", 0))
                    ),
                    cache_hit_ratio=_ratio_or_unknown(data),
                    timestamp=data.get("timestamp", ""),
                    formats=data.get("formats", []),
                    ats_mode=data.get("ats_mode", False),
                    status=tracking.status,
                    applied_at=tracking.applied_at,
                    next_action=tracking.next_action,
                    next_action_due=tracking.next_action_due,
                    notes=tracking.notes,
                )
            )
        except Exception:
            continue

    entries.sort(key=lambda e: (_STATUS_RANK.get(e.status, 99), _neg_ts(e.timestamp)))
    return entries


def _neg_ts(ts: str) -> str:
    # Cheap reverse-sort within a status bucket: newer timestamps come first.
    # Pad to keep lexicographic ordering stable across odd-length strings.
    return "".join(chr(255 - ord(c)) for c in ts.ljust(32))

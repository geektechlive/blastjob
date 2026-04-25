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
    cost_usd: float
    total_tokens: int
    cache_hit_ratio: float
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


def scan_history(output_root: Path) -> list[HistoryEntry]:
    entries = []
    if not output_root.exists():
        return entries

    for meta_file in output_root.rglob("metadata.json"):
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
                    cost_usd=data.get("cost_usd", 0.0),
                    total_tokens=data.get("input_tokens", 0) + data.get("output_tokens", 0),
                    cache_hit_ratio=data.get("cache_hit_ratio", 0.0),
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

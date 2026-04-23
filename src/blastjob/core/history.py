from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


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


def scan_history(output_root: Path) -> list[HistoryEntry]:
    entries = []
    if not output_root.exists():
        return entries

    for meta_file in output_root.rglob("metadata.json"):
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            entries.append(
                HistoryEntry(
                    path=meta_file.parent,
                    date=data.get("timestamp", "")[:10],
                    company=data.get("company", meta_file.parent.parent.name),
                    role=data.get("role", meta_file.parent.name),
                    cost_usd=data.get("cost_usd", 0.0),
                    total_tokens=data.get("input_tokens", 0) + data.get("output_tokens", 0),
                    cache_hit_ratio=data.get("cache_hit_ratio", 0.0),
                    timestamp=data.get("timestamp", ""),
                    formats=data.get("formats", []),
                    ats_mode=data.get("ats_mode", False),
                )
            )
        except Exception:
            continue

    entries.sort(key=lambda e: e.timestamp, reverse=True)
    return entries

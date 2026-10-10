from __future__ import annotations

import json
import os
from pathlib import Path

from blastjob.models.tracking import TrackingRecord

TRACKING_FILENAME = "tracking.json"


def tracking_path(run_dir: Path) -> Path:
    return run_dir / TRACKING_FILENAME


def load_tracking(run_dir: Path) -> TrackingRecord:
    path = tracking_path(run_dir)
    if not path.exists():
        return TrackingRecord()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return TrackingRecord()
    try:
        return TrackingRecord.model_validate(data)
    except Exception:
        return TrackingRecord()


def save_tracking(run_dir: Path, record: TrackingRecord) -> None:
    path = tracking_path(run_dir)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)

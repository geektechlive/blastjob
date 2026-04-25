"""Unit tests for models/tracking.py and core/tracking.py."""

import json

import pytest
from pydantic import ValidationError

from blastjob.core.tracking import load_tracking, save_tracking, tracking_path
from blastjob.models.tracking import STATUSES, TrackingRecord

# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


def test_default_record_is_drafted():
    rec = TrackingRecord()
    assert rec.status == "drafted"
    assert rec.applied_at is None
    assert rec.next_action == ""
    assert rec.next_action_due is None
    assert rec.notes == ""


def test_all_canonical_statuses_accepted():
    for status in STATUSES:
        TrackingRecord(status=status)


def test_invalid_status_rejected():
    with pytest.raises(ValidationError):
        TrackingRecord(status="bogus")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_load_returns_default_when_file_missing(tmp_path):
    rec = load_tracking(tmp_path)
    assert rec.status == "drafted"
    assert rec.applied_at is None


def test_load_returns_default_when_file_corrupt(tmp_path):
    tracking_path(tmp_path).write_text("not json {{{", encoding="utf-8")
    rec = load_tracking(tmp_path)
    assert rec.status == "drafted"


def test_load_returns_default_when_status_invalid(tmp_path):
    tracking_path(tmp_path).write_text(json.dumps({"status": "imaginary"}), encoding="utf-8")
    rec = load_tracking(tmp_path)
    assert rec.status == "drafted"


def test_round_trip(tmp_path):
    original = TrackingRecord(
        status="interview",
        applied_at="2026-04-20",
        next_action="Send thank-you note",
        next_action_due="2026-04-22",
        notes="Talked to Jane in eng.",
    )
    save_tracking(tmp_path, original)
    loaded = load_tracking(tmp_path)
    assert loaded.status == original.status
    assert loaded.applied_at == original.applied_at
    assert loaded.next_action == original.next_action
    assert loaded.next_action_due == original.next_action_due
    assert loaded.notes == original.notes


def test_save_overwrites_atomically(tmp_path):
    save_tracking(tmp_path, TrackingRecord(status="applied", applied_at="2026-04-01"))
    save_tracking(tmp_path, TrackingRecord(status="rejected"))
    loaded = load_tracking(tmp_path)
    assert loaded.status == "rejected"
    # No leftover .tmp file
    assert not (tmp_path / "tracking.json.tmp").exists()

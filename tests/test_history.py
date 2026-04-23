"""Unit tests for core/history.py scan_history()."""

import json
from pathlib import Path

import pytest

from blastjob.core.history import scan_history


def _write_metadata(path: Path, data: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "metadata.json").write_text(json.dumps(data), encoding="utf-8")


def _valid_meta(
    company: str = "Acme", role: str = "Engineer", timestamp: str = "2026-04-01T10:00:00"
) -> dict:
    return {
        "company": company,
        "role": role,
        "timestamp": timestamp,
        "cost_usd": 0.05,
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_hit_ratio": 0.75,
        "formats": ["md", "pdf"],
        "ats_mode": False,
    }


# ---------------------------------------------------------------------------
# Basic cases
# ---------------------------------------------------------------------------


def test_scan_history_missing_dir(tmp_path):
    result = scan_history(tmp_path / "nonexistent")
    assert result == []


def test_scan_history_empty_dir(tmp_path):
    result = scan_history(tmp_path)
    assert result == []


def test_scan_history_single_valid_entry(tmp_path):
    folder = tmp_path / "2026-04-01" / "acme" / "engineer"
    _write_metadata(folder, _valid_meta())
    result = scan_history(tmp_path)
    assert len(result) == 1
    assert result[0].company == "Acme"
    assert result[0].role == "Engineer"
    assert result[0].cost_usd == pytest.approx(0.05)
    assert result[0].formats == ["md", "pdf"]
    assert result[0].ats_mode is False


def test_scan_history_total_tokens(tmp_path):
    folder = tmp_path / "2026-04-01" / "acme" / "engineer"
    _write_metadata(folder, _valid_meta())
    result = scan_history(tmp_path)
    assert result[0].total_tokens == 1500


def test_scan_history_date_extracted(tmp_path):
    folder = tmp_path / "2026-04-01" / "acme" / "engineer"
    _write_metadata(folder, _valid_meta(timestamp="2026-04-15T14:30:00"))
    result = scan_history(tmp_path)
    assert result[0].date == "2026-04-15"


# ---------------------------------------------------------------------------
# Sort order
# ---------------------------------------------------------------------------


def test_scan_history_sorted_newest_first(tmp_path):
    for i, (company, ts) in enumerate(
        [
            ("OldCo", "2025-01-01T00:00:00"),
            ("NewCo", "2026-04-20T00:00:00"),
            ("MidCo", "2025-06-15T00:00:00"),
        ]
    ):
        folder = tmp_path / f"run-{i}" / company.lower() / "engineer"
        _write_metadata(folder, _valid_meta(company=company, timestamp=ts))

    result = scan_history(tmp_path)
    assert [e.company for e in result] == ["NewCo", "MidCo", "OldCo"]


# ---------------------------------------------------------------------------
# Corrupt / missing fields
# ---------------------------------------------------------------------------


def test_scan_history_skips_corrupt_metadata(tmp_path):
    good = tmp_path / "2026-04-01" / "acme" / "engineer"
    _write_metadata(good, _valid_meta())

    bad = tmp_path / "2026-04-01" / "bad" / "role"
    bad.mkdir(parents=True)
    (bad / "metadata.json").write_text("not valid json at all !!!", encoding="utf-8")

    result = scan_history(tmp_path)
    assert len(result) == 1
    assert result[0].company == "Acme"


def test_scan_history_missing_optional_fields(tmp_path):
    folder = tmp_path / "run" / "minimal" / "role"
    _write_metadata(folder, {"company": "MinCo", "role": "Dev", "timestamp": "2026-03-01T00:00:00"})
    result = scan_history(tmp_path)
    assert len(result) == 1
    assert result[0].cost_usd == 0.0
    assert result[0].total_tokens == 0
    assert result[0].cache_hit_ratio == 0.0


def test_scan_history_multiple_entries(tmp_path):
    for i in range(5):
        folder = tmp_path / f"run-{i}" / f"company-{i}" / "engineer"
        _write_metadata(
            folder, _valid_meta(company=f"Company{i}", timestamp=f"2026-04-0{i + 1}T00:00:00")
        )
    result = scan_history(tmp_path)
    assert len(result) == 5

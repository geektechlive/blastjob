"""Unit tests for core/history.py scan_history() matching both metadata filename
conventions found on disk:

  - legacy: bare `metadata.json` (one build per nested {date}/{company}/{role}/ dir)
  - current (core/build.py): `{role_slug}.metadata.json` (role-prefixed sidecar in a
    flat {company-slug}-{date}/ folder, since multiple roles/builds can share it)

core/history.py:47 originally globbed only for the legacy bare filename, so
scan_history returned nothing for any packet built by current code.
"""

import json
from pathlib import Path

from blastjob.core.history import scan_history


def _write_meta(path: Path, filename: str, data: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / filename).write_text(json.dumps(data), encoding="utf-8")


def _meta(company: str, role: str, timestamp: str = "2026-09-01T10:00:00") -> dict:
    return {
        "company": company,
        "role": role,
        "timestamp": timestamp,
        "cost_usd": 0.05,
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_hit_ratio": 0.75,
        "formats": ["md"],
        "ats_mode": False,
    }


def test_scan_history_finds_role_prefixed_metadata(tmp_path):
    """The convention core/build.py actually writes: {role_slug}.metadata.json
    in a flat {company-slug}-{date}/ folder."""
    folder = tmp_path / "fabrikam-equipment-2026-09-01"
    meta = _meta("Fabrikam Equipment", "AIOps Engineer")
    _write_meta(folder, "aiops-engineer.metadata.json", meta)

    result = scan_history(tmp_path)

    assert len(result) == 1
    assert result[0].company == "Fabrikam Equipment"
    assert result[0].role == "AIOps Engineer"
    assert result[0].path == folder


def test_scan_history_finds_both_conventions_in_same_tree(tmp_path):
    """Real disk state right now: legacy AND current-convention packets coexist."""
    legacy = tmp_path / "2026-08-25" / "fabrikam-equipment" / "chief-information-officer"
    legacy_meta = _meta("Fabrikam Equipment", "Chief Operations Officer", "2026-08-25T09:00:00")
    _write_meta(legacy, "metadata.json", legacy_meta)

    current = tmp_path / "globex-2026-09-12"
    current_meta = _meta("Globex", "AIOps Engineer", "2026-09-12T09:00:00")
    _write_meta(current, "aiops-engineer.metadata.json", current_meta)

    result = scan_history(tmp_path)

    companies = {e.company for e in result}
    assert companies == {"Fabrikam Equipment", "Globex"}
    assert len(result) == 2


def test_scan_history_multiple_role_prefixed_packets_share_one_folder(tmp_path):
    """A single company-day folder can hold sidecars for more than one role."""
    folder = tmp_path / "globex-2026-09-12"
    _write_meta(folder, "aiops-engineer.metadata.json", _meta("Globex", "AIOps Engineer"))
    _write_meta(folder, "platform-lead.metadata.json", _meta("Globex", "Platform Lead"))

    result = scan_history(tmp_path)

    roles = {e.role for e in result}
    assert roles == {"AIOps Engineer", "Platform Lead"}
    assert len(result) == 2


def test_scan_history_does_not_double_count_legacy_bare_file(tmp_path):
    """Sanity check: the new glob for role-prefixed files must not also match
    (and thus double-count) the legacy bare metadata.json."""
    legacy = tmp_path / "2026-08-25" / "acme" / "engineer"
    _write_meta(legacy, "metadata.json", _meta("Acme", "Engineer"))

    result = scan_history(tmp_path)

    assert len(result) == 1

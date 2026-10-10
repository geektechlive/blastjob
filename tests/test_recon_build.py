"""Regressions for the ATS exporter and template ingestion.

Both defects shared a shape: the code produced output that looked fine and was
quietly wrong -- deleted characters in one case, an empty template reported as
saved in the other.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from blastjob.core.ingest import _split_templates
from blastjob.exporters import ats


def _out() -> Path:
    return Path(tempfile.mkdtemp())


def test_em_dash_becomes_a_dash_not_a_hole():
    """errors="ignore" deleted the character outright, closing up the words:
    "Senior Director - Ops" came out as "Senior Director  Ops"."""
    dest = ats.write("Senior Director — Ops", _out())
    text = dest.read_text(encoding="ascii")
    assert "-" in text
    assert "Director  Ops" not in text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("• Led the team", "*"),
        ("Cut cost by 30°", "deg"),
        ("Fabrikam® automation", "(R)"),
        ("Copilot™ rollout", "(TM)"),
        # An arrow used to become "->" and a curly quote a straight '"'. Both
        # emit a character Workday rejects outright ("Contains illegal
        # characters", logged 2026-09-10 on a field holding a quoted program
        # name), so the banned set now wins: the character is still
        # transliterated rather than silently dropped, just to a safe glyph.
        ("revenue → growth", "-"),
        ("don’t", "'"),
        ("“Applied Technology”", "'Applied Technology'"),
        ("André managed it", "Andre"),
    ],
)
def test_typography_is_transliterated_not_dropped(raw, expected):
    dest = ats.write(raw, _out())
    assert expected in dest.read_text(encoding="ascii")


def test_output_is_pure_ascii_without_silent_deletion():
    dest = ats.write("—•“é… Director", _out())
    text = dest.read_text(encoding="ascii")
    assert text.isascii()
    assert "Director" in text


def test_split_templates_still_splits_a_well_formed_response():
    standard, atx = _split_templates("===STANDARD===\nA body\n===ATS===\nB body")
    assert standard == "A body"
    assert atx == "B body"


def test_split_templates_reports_a_missing_delimiter_as_an_empty_ats():
    """The split itself stays honest; refusing to WRITE the empty file is the
    caller's job (run_ingestion), which is what actually prevented the silent
    "Templates saved" on a failed generation."""
    standard, atx = _split_templates("just one blob, no delimiter")
    assert standard == "just one blob, no delimiter"
    assert atx == ""

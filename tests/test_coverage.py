"""Unit tests for core/coverage.py and models/coverage.py."""

from __future__ import annotations

import asyncio
import json

import pytest

from blastjob.core.coverage import analyze_coverage, parse_coverage
from blastjob.llm.cost import CostTracker
from blastjob.models.config import BlastJobConfig, PathsConfig
from blastjob.models.coverage import CoverageReport, Requirement

WORK_HISTORY = (
    "## Acme Corp — Senior Engineer\n\n"
    "- Led team of 8 engineers to ship the payments platform\n"
    "- Reduced p99 latency by 40%\n"
    "- Migrated services from Python 2 to Python 3\n"
)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def test_must_have_coverage_pct_no_musts():
    report = CoverageReport(requirements=[Requirement(text="x", priority="nice")])
    assert report.must_have_coverage_pct == 0


def test_must_have_coverage_pct_partial():
    report = CoverageReport(
        requirements=[
            Requirement(text="a", priority="must", covered=True),
            Requirement(text="b", priority="must", covered=False),
            Requirement(text="c", priority="nice", covered=True),
        ]
    )
    assert report.must_have_coverage_pct == 50


def test_gap_count_counts_uncovered():
    report = CoverageReport(
        requirements=[
            Requirement(text="a", covered=True),
            Requirement(text="b", covered=False),
            Requirement(text="c", covered=False),
        ]
    )
    assert report.gap_count == 2


# ---------------------------------------------------------------------------
# parse_coverage
# ---------------------------------------------------------------------------


def _payload(**overrides) -> str:
    data = {
        "coverage_score": 50,  # will be recomputed
        "summary": "Decent fit.",
        "requirements": [],
    }
    data.update(overrides)
    return json.dumps(data)


def test_parse_coverage_no_json_raises():
    with pytest.raises(ValueError, match="No JSON"):
        parse_coverage("nothing here", WORK_HISTORY)


def test_parse_coverage_grounded_quote_preserved():
    text = _payload(
        requirements=[
            {
                "text": "Lead engineering teams",
                "priority": "must",
                "covered": True,
                "evidence_quote": "Led team of 8 engineers",
                "gap_note": "",
            }
        ]
    )
    report = parse_coverage(text, WORK_HISTORY)
    assert report.requirements[0].covered is True
    assert report.requirements[0].evidence_quote == "Led team of 8 engineers"


def test_parse_coverage_fabricated_quote_downgraded():
    text = _payload(
        requirements=[
            {
                "text": "Built rocket ships",
                "priority": "must",
                "covered": True,
                "evidence_quote": "This text is not in the work history at all",
                "gap_note": "",
            }
        ]
    )
    report = parse_coverage(text, WORK_HISTORY)
    assert report.requirements[0].covered is False
    assert "not found" in report.requirements[0].gap_note.lower()


def test_parse_coverage_recomputes_score_must_only():
    text = _payload(
        coverage_score=99,  # bogus value the model tried to inflate
        requirements=[
            {
                "text": "a",
                "priority": "must",
                "covered": True,
                "evidence_quote": "Led team of 8 engineers",
                "gap_note": "",
            },
            {
                "text": "b",
                "priority": "must",
                "covered": False,
                "evidence_quote": "",
                "gap_note": "missing",
            },
        ],
    )
    report = parse_coverage(text, WORK_HISTORY)
    # 1/2 musts = 50%; pure-must score = 50
    assert report.coverage_score == 50


def test_parse_coverage_recomputes_score_weighted():
    text = _payload(
        coverage_score=99,
        requirements=[
            {
                "text": "must1",
                "priority": "must",
                "covered": True,
                "evidence_quote": "Led team of 8 engineers",
                "gap_note": "",
            },
            {
                "text": "must2",
                "priority": "must",
                "covered": False,
                "evidence_quote": "",
                "gap_note": "missing",
            },
            {
                "text": "nice1",
                "priority": "nice",
                "covered": True,
                "evidence_quote": "Reduced p99 latency by 40%",
                "gap_note": "",
            },
            {
                "text": "nice2",
                "priority": "nice",
                "covered": True,
                "evidence_quote": "Migrated services from Python 2 to Python 3",
                "gap_note": "",
            },
        ],
    )
    report = parse_coverage(text, WORK_HISTORY)
    # must_pct = 50, nice_pct = 100; weighted = (2*50 + 100) / 3 = 66
    assert report.coverage_score == 66


def test_parse_coverage_no_requirements_score_zero():
    text = _payload(coverage_score=80)
    report = parse_coverage(text, WORK_HISTORY)
    assert report.coverage_score == 0


# ---------------------------------------------------------------------------
# analyze_coverage (with mocked stream)
# ---------------------------------------------------------------------------


class _FakeUsage:
    input_tokens = 50
    output_tokens = 20
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 30


class _FakeFinal:
    usage = _FakeUsage()


class _FakeStream:
    def __init__(self, payload: str):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    @property
    def text_stream(self):
        async def _iter():
            yield self._payload

        return _iter()

    async def get_final_message(self):
        return _FakeFinal()


def _seed_work_history(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "work_history.md").write_text(WORK_HISTORY, encoding="utf-8")
    cfg = BlastJobConfig(
        paths=PathsConfig(data_dir=str(data_dir), output_dir=str(tmp_path / "out"))
    )
    return cfg


def test_analyze_coverage_end_to_end(tmp_path, monkeypatch):
    cfg = _seed_work_history(tmp_path)
    payload = _payload(
        requirements=[
            {
                "text": "Engineering leadership",
                "priority": "must",
                "covered": True,
                "evidence_quote": "Led team of 8 engineers",
                "gap_note": "",
            }
        ]
    )

    async def factory(*_a, **_kw):
        return _FakeStream(payload)

    monkeypatch.setattr("blastjob.core.coverage.make_stream", factory)
    tracker = CostTracker()

    report = asyncio.run(analyze_coverage("Need a leader.", cfg, tracker))

    assert report.coverage_score == 100
    assert len(report.requirements) == 1
    assert report.requirements[0].covered is True
    assert len(tracker.calls) == 1


def test_analyze_coverage_raises_without_work_history(tmp_path, monkeypatch):
    cfg = BlastJobConfig(
        paths=PathsConfig(data_dir=str(tmp_path / "empty"), output_dir=str(tmp_path / "out"))
    )

    async def factory(*_a, **_kw):
        return _FakeStream("{}")

    monkeypatch.setattr("blastjob.core.coverage.make_stream", factory)
    with pytest.raises(FileNotFoundError):
        asyncio.run(analyze_coverage("a JD", cfg, CostTracker()))


def test_analyze_coverage_raises_on_empty_jd(tmp_path):
    cfg = _seed_work_history(tmp_path)
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(analyze_coverage("   ", cfg, CostTracker()))

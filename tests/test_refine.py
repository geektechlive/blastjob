"""Unit tests for core/refine.py."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from blastjob.core.refine import _highest_archived_version, _next_version_number, run_refine
from blastjob.llm.cost import CostTracker
from blastjob.models.config import BlastJobConfig, PathsConfig

# ---------------------------------------------------------------------------
# Fake stream infrastructure (mirrors test_build_logic.py)
# ---------------------------------------------------------------------------


class _FakeUsage:
    input_tokens = 200
    output_tokens = 80
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 150


class _FakeFinal:
    usage = _FakeUsage()


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    @property
    def text_stream(self):
        async def _iter():
            for c in self._chunks:
                yield c

        return _iter()

    async def get_final_message(self):
        return _FakeFinal()


def _make_factory(*sequences):
    """Return a make_stream factory that hands out one stream per call."""
    state = {"i": 0}

    async def factory(*_a, **_kw):
        seq = sequences[state["i"] % len(sequences)]
        state["i"] += 1
        return _FakeStream(seq)

    return factory


def _patch_streams(monkeypatch, factory):
    """Patch make_stream in both refine and scoring namespaces.

    score_resume imports make_stream into its own module, so patching only
    refine misses the scoring call and leaves the factory state misaligned.
    """
    monkeypatch.setattr("blastjob.core.refine.make_stream", factory)
    monkeypatch.setattr("blastjob.core.scoring.make_stream", factory)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_run(tmp_path: Path) -> tuple[Path, Path, BlastJobConfig]:
    """Lay out a minimal data dir + run dir. Returns (data_dir, run_dir, config)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "work_history.md").write_text(
        "## Acme — Engineer\n\n- Led team of 8 engineers\n", encoding="utf-8"
    )

    run_dir = tmp_path / "out" / "2026-04-20" / "acme" / "engineer"
    run_dir.mkdir(parents=True)
    (run_dir / "resume.md").write_text("# v1 resume\n\n- old bullet\n", encoding="utf-8")
    (run_dir / "job_description.md").write_text(
        "# Job Description\n\n**Company:** Acme\n\nJD body here.\n", encoding="utf-8"
    )
    (run_dir / "metadata.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-04-20T10:00:00",
                "company": "Acme",
                "role": "Engineer",
                "formats": ["md"],
            }
        ),
        encoding="utf-8",
    )

    cfg = BlastJobConfig(
        paths=PathsConfig(data_dir=str(data_dir), output_dir=str(tmp_path / "out"))
    )
    return data_dir, run_dir, cfg


# ---------------------------------------------------------------------------
# _next_version_number
# ---------------------------------------------------------------------------


def test_next_version_is_2_when_no_archives(tmp_path):
    (tmp_path / "resume.md").write_text("v1")
    assert _highest_archived_version(tmp_path) == 0
    assert _next_version_number(tmp_path) == 2


def test_next_version_climbs_with_archives(tmp_path):
    (tmp_path / "resume.md").write_text("current — implicitly v3")
    (tmp_path / "resume.v1.md").write_text("v1")
    (tmp_path / "resume.v2.md").write_text("v2")
    assert _highest_archived_version(tmp_path) == 2
    # 2 archives means current is v3, so the next refine produces v4
    assert _next_version_number(tmp_path) == 4


# ---------------------------------------------------------------------------
# run_refine
# ---------------------------------------------------------------------------


def test_run_refine_rotates_resume(tmp_path, monkeypatch):
    _, run_dir, cfg = _seed_run(tmp_path)
    _patch_streams(monkeypatch, _make_factory(["# v2 resume\n\n- new bullet\n"], ["{}"]))
    tracker = CostTracker()

    new_path, _ = asyncio.run(run_refine(run_dir, "make it shorter", cfg, tracker, on_text=None))

    assert new_path == run_dir / "resume.md"
    assert "v2 resume" in new_path.read_text()
    archived = run_dir / "resume.v1.md"
    assert archived.exists()
    assert "v1 resume" in archived.read_text()


def test_run_refine_appends_to_versions_metadata(tmp_path, monkeypatch):
    _, run_dir, cfg = _seed_run(tmp_path)
    _patch_streams(monkeypatch, _make_factory(["# revised\n"], ["{}"]))
    tracker = CostTracker()

    asyncio.run(run_refine(run_dir, "tighten it", cfg, tracker, on_text=None))

    meta = json.loads((run_dir / "metadata.json").read_text())
    assert "versions" in meta
    assert len(meta["versions"]) == 1
    v = meta["versions"][0]
    assert v["n"] == 2
    assert v["feedback"] == "tighten it"
    assert v["cost_usd"] > 0
    assert v["input_tokens"] == 200


def test_run_refine_records_cost(tmp_path, monkeypatch):
    _, run_dir, cfg = _seed_run(tmp_path)
    _patch_streams(monkeypatch, _make_factory(["# revised\n"], ["{}"]))
    tracker = CostTracker()

    asyncio.run(run_refine(run_dir, "feedback", cfg, tracker, on_text=None))

    # One refine call + one score call
    assert len(tracker.calls) == 2


def test_run_refine_two_revisions_creates_v1_and_v2_archives(tmp_path, monkeypatch):
    _, run_dir, cfg = _seed_run(tmp_path)
    _patch_streams(
        monkeypatch,
        _make_factory(
            ["# v2\n"],
            ["{}"],
            ["# v3\n"],
            ["{}"],
        ),
    )
    tracker = CostTracker()

    asyncio.run(run_refine(run_dir, "first edit", cfg, tracker))
    asyncio.run(run_refine(run_dir, "second edit", cfg, tracker))

    assert (run_dir / "resume.md").read_text().startswith("# v3")
    assert (run_dir / "resume.v1.md").read_text().startswith("# v1")
    assert (run_dir / "resume.v2.md").read_text().startswith("# v2")
    meta = json.loads((run_dir / "metadata.json").read_text())
    assert [v["n"] for v in meta["versions"]] == [2, 3]


def test_run_refine_raises_when_no_resume(tmp_path, monkeypatch):
    _, run_dir, cfg = _seed_run(tmp_path)
    (run_dir / "resume.md").unlink()
    _patch_streams(monkeypatch, _make_factory(["should not be called"]))
    with pytest.raises(FileNotFoundError):
        asyncio.run(run_refine(run_dir, "feedback", cfg, CostTracker()))


def test_run_refine_raises_on_empty_feedback(tmp_path, monkeypatch):
    _, run_dir, cfg = _seed_run(tmp_path)
    _patch_streams(monkeypatch, _make_factory(["x"]))
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(run_refine(run_dir, "   ", cfg, CostTracker()))


def test_run_refine_raises_on_empty_response(tmp_path, monkeypatch):
    _, run_dir, cfg = _seed_run(tmp_path)
    _patch_streams(monkeypatch, _make_factory([""]))
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(run_refine(run_dir, "feedback", cfg, CostTracker()))

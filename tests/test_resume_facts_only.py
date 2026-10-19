"""The resume prompt gets the verified facts block, never the raw research (#36).

Research is a trust boundary (docs/truth-guarantees.md): recalled or scraped text about an
employer reaches a generating call only as span-verified facts. The cover letter already
worked that way; the resume prompt used to inline the whole sidecar.
"""

from __future__ import annotations

import json
from pathlib import Path

from blastjob.core.build import run_build
from blastjob.core.packet_research import ResearchResult
from blastjob.core.research_facts import CompanyFact, Polarity, Provenance
from blastjob.llm.cost import CostTracker
from blastjob.models.config import BlastJobConfig

UNVERIFIED = "Northwind Robotics sells to the aerospace market in forty countries."
VERIFIED_SPAN = "Northwind Robotics was founded in a garage in Racine."


class _Usage:
    input_tokens = 10
    output_tokens = 5
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _Final:
    usage = _Usage()


class _Stream:
    def __init__(self, text: str) -> None:
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    @property
    def text_stream(self):
        async def _iter():
            yield self._text

        return _iter()

    async def get_final_message(self):
        return _Final()


def _config(tmp_path: Path) -> BlastJobConfig:
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.paths.output_dir = str(tmp_path / "out")
    data = Path(cfg.paths.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    (data / "work_history.md").write_text("---\nname: Jane Doe\n---\n\nSenior IT exec.\n")
    return cfg


async def _resume_messages(tmp_path, monkeypatch, facts: list[CompanyFact]) -> str:
    captured: list[str] = []

    async def fake_research(_cfg, _company, _tracker, _max=3):
        return f"{UNVERIFIED}\n{VERIFIED_SPAN}\n"

    async def fake_distill(_md, **_kw):
        return facts

    async def fake_stream(_cfg, system, messages, max_tokens=4096):
        captured.append(json.dumps([system, messages]))
        return _Stream("# Resume\n\nDid stuff.")

    async def no_pages(_cfg, _context, _tracker):
        return ResearchResult(None)

    monkeypatch.setattr("blastjob.core.build.make_research_call", fake_research)
    monkeypatch.setattr("blastjob.core.build.distill_research", fake_distill)
    monkeypatch.setattr("blastjob.core.build.packet_research.research_from_pages", no_pages)
    monkeypatch.setattr("blastjob.core.build.make_stream", fake_stream)

    await run_build(
        company="Northwind Robotics",
        role="VP Engineering",
        job_description="Lead engineering.",
        formats={"md"},
        use_ats=False,
        app_config=_config(tmp_path),
        cost_tracker=CostTracker(),
        include_cover_letter=False,
    )
    return captured[0]


def _fact() -> CompanyFact:
    return CompanyFact(
        statement="founded in Racine",
        source_span=VERIFIED_SPAN,
        polarity=Polarity.AFFIRMED,
        provenance=Provenance.WEB_SEARCH,
    )


async def test_resume_prompt_never_carries_raw_research(tmp_path, monkeypatch):
    sent = await _resume_messages(tmp_path, monkeypatch, [_fact()])

    assert UNVERIFIED not in sent


async def test_resume_prompt_carries_the_verified_fact(tmp_path, monkeypatch):
    sent = await _resume_messages(tmp_path, monkeypatch, [_fact()])

    assert VERIFIED_SPAN in sent


async def test_resume_prompt_has_no_research_when_no_fact_verified(tmp_path, monkeypatch):
    sent = await _resume_messages(tmp_path, monkeypatch, [])

    assert UNVERIFIED not in sent
    assert VERIFIED_SPAN not in sent
    assert "No verified company facts" in sent

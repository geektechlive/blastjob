"""Unit tests for pure functions in core/build.py."""

import asyncio
import json

import pytest

from blastjob.core.build import _generate_cover_letter, _parse_score, _render_score_md
from blastjob.llm.cost import CostTracker
from blastjob.models.config import BlastJobConfig
from blastjob.models.fit_score import ClaimCheck, FitScore

# ---------------------------------------------------------------------------
# _parse_score
# ---------------------------------------------------------------------------


def _make_score_json(**overrides) -> str:
    data = {
        "overall_score": 80,
        "jd_alignment_score": 75,
        "groundedness_score": 90,
        "unsupported_count": 1,
        "summary": "Strong candidate.",
        "claims": [],
    }
    data.update(overrides)
    return json.dumps(data)


WORK_HISTORY = "Led team of 8 engineers to deliver the payments platform."


def test_parse_score_basic():
    text = _make_score_json()
    score = _parse_score(text, WORK_HISTORY)
    assert score.overall_score >= 0
    assert score.jd_alignment_score == 75
    assert score.summary == "Strong candidate."


def test_parse_score_no_json_raises():
    with pytest.raises(ValueError, match="No JSON"):
        _parse_score("no JSON here", WORK_HISTORY)


def test_parse_score_grounded_claim_verified():
    claims = [
        {
            "claim_text": "Led team",
            "grounded": True,
            "evidence_quote": "Led team of 8 engineers",
            "source_section": "Experience",
        }
    ]
    text = _make_score_json(claims=claims)
    score = _parse_score(text, WORK_HISTORY)
    assert score.claims[0].grounded is True


def test_parse_score_fabricated_quote_downgraded():
    claims = [
        {
            "claim_text": "Built rocket ships",
            "grounded": True,
            "evidence_quote": "This quote does not appear in work history at all",
            "source_section": "Experience",
        }
    ]
    text = _make_score_json(claims=claims)
    score = _parse_score(text, WORK_HISTORY)
    assert score.claims[0].grounded is False


def test_parse_score_recalculates_unsupported_count():
    claims = [
        {
            "claim_text": "Claim A",
            "grounded": True,
            "evidence_quote": "Led team of 8 engineers",
            "source_section": "",
        },
        {
            "claim_text": "Claim B",
            "grounded": True,
            "evidence_quote": "This is a fabricated quote that is not in history",
            "source_section": "",
        },
    ]
    text = _make_score_json(claims=claims)
    score = _parse_score(text, WORK_HISTORY)
    # Claim B has a fabricated quote so it gets downgraded — unsupported_count must be 1
    assert score.unsupported_count == 1


def test_parse_score_recalculates_overall_from_claims():
    claims = [
        {
            "claim_text": "Grounded claim",
            "grounded": True,
            "evidence_quote": "Led team of 8 engineers",
            "source_section": "",
        },
        {
            "claim_text": "Ungrounded claim",
            "grounded": False,
            "evidence_quote": "",
            "source_section": "",
        },
    ]
    text = _make_score_json(jd_alignment_score=60, claims=claims)
    score = _parse_score(text, WORK_HISTORY)
    # 1/2 grounded = 50% groundedness; overall = int(50 * 0.7 + 60 * 0.3) = int(35+18) = 53
    assert score.groundedness_score == 50
    assert score.overall_score == 53


def test_parse_score_with_surrounding_text():
    json_blob = _make_score_json(summary="Excellent fit.")
    text = f"Here is my analysis:\n\n{json_blob}\n\nEnd of analysis."
    score = _parse_score(text, WORK_HISTORY)
    assert score.summary == "Excellent fit."


# ---------------------------------------------------------------------------
# _render_score_md
# ---------------------------------------------------------------------------


def test_render_score_md_header():
    score = FitScore(
        overall_score=80,
        jd_alignment_score=75,
        groundedness_score=90,
        unsupported_count=1,
        summary="Good match.",
    )
    md = _render_score_md(score)
    assert "# Fit Score Report" in md
    assert "**Overall:** 80/100" in md
    assert "**JD Alignment:** 75/100" in md
    assert "**Groundedness:** 90/100" in md
    assert "Good match." in md


def test_render_score_md_grounded_claim():
    claim = ClaimCheck(
        claim_text="Led payments platform",
        grounded=True,
        evidence_quote="Led team of 8",
        source_section="Experience",
    )
    score = FitScore(
        overall_score=85,
        jd_alignment_score=80,
        groundedness_score=100,
        unsupported_count=0,
        summary="Strong.",
        claims=[claim],
    )
    md = _render_score_md(score)
    assert "Led payments platform" in md
    assert 'Evidence: "Led team of 8"' in md
    assert "Experience" in md


def test_render_score_md_ungrounded_claim():
    claim = ClaimCheck(
        claim_text="Built a rocket",
        grounded=False,
        evidence_quote="",
        source_section="",
    )
    score = FitScore(
        overall_score=40,
        jd_alignment_score=50,
        groundedness_score=0,
        unsupported_count=1,
        summary="Weak.",
        claims=[claim],
    )
    md = _render_score_md(score)
    assert "Built a rocket" in md
    assert "No supporting evidence" in md


def test_render_score_md_no_claims():
    score = FitScore(
        overall_score=70,
        jd_alignment_score=70,
        groundedness_score=70,
        unsupported_count=0,
        summary="Decent.",
    )
    md = _render_score_md(score)
    assert "## Claim Analysis" in md


# ---------------------------------------------------------------------------
# _generate_cover_letter
# ---------------------------------------------------------------------------


class _FakeUsage:
    input_tokens = 100
    output_tokens = 50
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 80


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
            for chunk in self._chunks:
                yield chunk

        return _iter()

    async def get_final_message(self):
        return _FakeFinal()


async def _fake_stream_factory(*_args, **_kwargs):
    return _FakeStream(["Dear team,\n\n", "I am excited about this role.\n"])


async def _empty_stream_factory(*_args, **_kwargs):
    return _FakeStream([""])


def test_cover_letter_writes_md_and_records_cost(tmp_path, monkeypatch):
    monkeypatch.setattr("blastjob.core.build.make_stream", _fake_stream_factory)
    cfg = BlastJobConfig()
    tracker = CostTracker()

    asyncio.run(
        _generate_cover_letter(
            resume_md="# Resume\n\nBullets.",
            work_history_md="History.",
            job_description="JD.",
            company_facts_md="",
            formats={"md"},
            out_dir=tmp_path,
            app_config=cfg,
            cost_tracker=tracker,
            on_text=None,
        )
    )

    cover_md = tmp_path / "cover_letter.md"
    assert cover_md.exists()
    assert "Dear team" in cover_md.read_text()
    # No PDF/DOCX requested
    assert not (tmp_path / "cover_letter.pdf").exists()
    assert not (tmp_path / "cover_letter.docx").exists()
    # Cost recorded
    assert len(tracker.calls) == 1
    assert tracker.calls[0].input_tokens == 100
    assert tracker.calls[0].output_tokens == 50


def test_cover_letter_skipped_when_response_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("blastjob.core.build.make_stream", _empty_stream_factory)
    cfg = BlastJobConfig()
    tracker = CostTracker()

    result = asyncio.run(
        _generate_cover_letter(
            resume_md="resume",
            work_history_md="history",
            job_description="jd",
            company_facts_md="",
            formats={"md"},
            out_dir=tmp_path,
            app_config=cfg,
            cost_tracker=tracker,
            on_text=None,
        )
    )

    assert result == (None, "")
    assert not (tmp_path / "cover_letter.md").exists()
    assert tracker.calls == []


def test_cover_letter_failure_is_silent(tmp_path, monkeypatch):
    async def _boom(*_a, **_kw):
        raise RuntimeError("network down")

    monkeypatch.setattr("blastjob.core.build.make_stream", _boom)
    cfg = BlastJobConfig()
    tracker = CostTracker()

    # Should not raise — fail-soft
    result = asyncio.run(
        _generate_cover_letter(
            resume_md="resume",
            work_history_md="history",
            job_description="jd",
            company_facts_md="",
            formats={"md"},
            out_dir=tmp_path,
            app_config=cfg,
            cost_tracker=tracker,
            on_text=None,
        )
    )

    assert result == (None, "")
    assert not (tmp_path / "cover_letter.md").exists()


def test_resume_prompt_forbids_ats_illegal_characters():
    """Workday rejects a pasted field containing < > [ ] " { } or a backslash.
    Verified live on 2026-09-10: quotation marks around a program name blocked
    a Role Description field mid-application."""
    from blastjob.llm.prompts import RESUME_SYSTEM

    assert "illegal characters" in RESUME_SYSTEM
    assert "never use double quotation marks" in RESUME_SYSTEM.lower()
    assert "markdown links" in RESUME_SYSTEM


def test_resume_prompt_requires_an_ats_parseable_layout():
    """Title alone on the heading line, employer on the next. A one-line
    heading made Workday read a section heading as the company name."""
    from blastjob.llm.prompts import RESUME_SYSTEM

    assert "ATS PARSEABILITY" in RESUME_SYSTEM
    assert "### Job Title" in RESUME_SYSTEM
    assert "**Employer Name** | City, ST" in RESUME_SYSTEM
    assert "Never group several jobs" in RESUME_SYSTEM


def test_ats_export_accepts_a_stem_like_every_other_exporter(tmp_path):
    """core.build calls ats.write(..., stem=...). Without the parameter that is
    a TypeError the first time the "ats" output format is switched on, and every
    role in one folder would overwrite the same resume.ats.txt."""
    from blastjob.exporters import ats

    out = ats.write("## Name\n\nSome resume text.\n", tmp_path, stem="acme_cio_ATS")
    assert out.name == "acme_cio_ATS.ats.txt"
    assert out.exists()
    assert "Some resume text." in out.read_text()

    assert ats.write("## Name\n\ntext\n", tmp_path).name == "resume.ats.txt"


def test_cover_letter_prompt_bans_the_ai_tells():
    """A generated letter read like AI wrote it: five em dashes, two 'X, not Y'
    snaps, 'quietly become', 'boringly reliable', 'for its own sake', and an
    announced First/Second enumeration."""
    from blastjob.llm.prompts import COVER_LETTER_SYSTEM

    low = COVER_LETTER_SYSTEM.lower()
    assert "em dashes" in low
    assert "metric stuffing" in low
    assert "contrast snap" in low
    assert "225-275 words" in COVER_LETTER_SYSTEM


def test_cover_letter_prompt_requires_salutation_and_signoff():
    from blastjob.llm.prompts import COVER_LETTER_SYSTEM

    assert "Dear Hiring Team," in COVER_LETTER_SYSTEM
    assert "Sincerely," in COVER_LETTER_SYSTEM


def test_docx_export_unwraps_soft_wrapped_paragraphs():
    """Markdown treats a single newline inside a paragraph as a space. The
    exporter emitted one Word paragraph per SOURCE line, so a body paragraph
    hard-wrapped at 100 characters broke every 100 characters instead of
    wrapping at the page margin. Seen in a real cover letter export."""
    from blastjob.exporters.docx import _unwrap_paragraphs

    src = (
        "I have run operations for regulated companies in this market,\nand my office is nearby.\n"
    )
    assert _unwrap_paragraphs(src) == (
        "I have run operations for regulated companies in this market, and my office is nearby."
    )


def test_docx_export_keeps_markdown_constructs_on_their_own_lines():
    """Resume headings and bullets must not be swallowed into a paragraph."""
    from blastjob.exporters.docx import _unwrap_paragraphs

    src = (
        "## SUMMARY\n\n### Director of Operations\n**Acme** | Fairhaven, OH\n"
        "- Did a thing\n- Did another\n"
    )
    out = _unwrap_paragraphs(src).splitlines()
    assert "## SUMMARY" in out
    assert "### Director of Operations" in out
    assert "- Did a thing" in out
    assert "- Did another" in out
    # The employer line is plain text, so it stays its own paragraph here.
    assert "**Acme** | Fairhaven, OH" in out


def test_docx_export_separates_paragraphs_on_blank_lines():
    from blastjob.exporters.docx import _unwrap_paragraphs

    assert _unwrap_paragraphs("One\ntwo.\n\nThree.\n").splitlines() == ["One two.", "", "Three."]

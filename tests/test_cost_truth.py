"""RED-first tests for the cost-truth fix (workstream W31).

Two independent defects, both about *fabricating* a number instead of admitting
it isn't known:

Defect 1 — `providers.py`'s claude-cli path (the app's DEFAULT, no-API-key
provider) hardcoded zero usage on every call, so every cost and cache-hit
ratio ever recorded for the user's actual day-to-day usage was a fabricated
$0.00, not an honest "unknown". `claude -p --output-format stream-json
--include-partial-messages --verbose` genuinely reports token usage on its
terminal `result` line, so the fix parses it — and only falls back to
`usage=None` (never a fake zero) when that line truly can't be found or
parsed.

Defect 2 — `core/build.py` wrote only the resume call's cost into
`metadata.json`, even though one `run_build` makes up to four LLM calls
(company research, resume, cover letter, fit score). The fix aggregates all
four into the metadata, under field names that say what they cover.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from blastjob.core.build import run_build
from blastjob.llm import providers
from blastjob.llm.cost import CallCost, CostTracker, aggregate_calls, cost_from_usage
from blastjob.models.config import BlastJobConfig, PricingConfig

# ---------------------------------------------------------------------------
# Defect 1a — cost.py: unknown usage must read as unknown, never as zero
# ---------------------------------------------------------------------------


class _RealUsage:
    input_tokens = 10
    output_tokens = 5
    cache_creation_input_tokens = 2
    cache_read_input_tokens = 3


def test_cost_from_usage_none_is_unknown_not_a_fabricated_zero():
    cost = cost_from_usage(None, PricingConfig())
    assert cost.known is False
    # Numeric fields stay at their dataclass defaults, but callers MUST check
    # `known` before treating them as real — this is not "the call cost $0".
    assert cost.cost_usd == 0.0


def test_cost_from_usage_real_usage_is_known():
    cost = cost_from_usage(_RealUsage(), PricingConfig())
    assert cost.known is True
    assert cost.input_tokens == 10
    assert cost.output_tokens == 5


def test_call_cost_defaults_to_known_for_backward_compatibility():
    # Every existing call site constructs CallCost without `known=`; it must
    # keep behaving as "known" so nothing upstream silently goes honest-unknown.
    assert CallCost(cost_usd=1.0).known is True


# ---------------------------------------------------------------------------
# Defect 2 (cost.py half) — combining several calls into one honest total
# ---------------------------------------------------------------------------


def test_aggregate_calls_sums_known_calls():
    calls = [
        CallCost(cost_usd=1.0, input_tokens=10, output_tokens=1),
        CallCost(cost_usd=2.0, input_tokens=20, output_tokens=2),
    ]
    agg = aggregate_calls(calls)
    assert agg.cost_usd == 3.0
    assert agg.input_tokens == 30
    assert agg.output_tokens == 3
    assert agg.known is True


def test_aggregate_calls_is_unknown_if_any_call_is_unknown():
    calls = [CallCost(cost_usd=1.0, known=True), CallCost(known=False)]
    agg = aggregate_calls(calls)
    assert agg.known is False


def test_aggregate_calls_empty_list_is_known_zero():
    agg = aggregate_calls([])
    assert agg.cost_usd == 0.0
    assert agg.known is True


def test_tracker_total_call_cost_matches_aggregate():
    tracker = CostTracker()
    tracker.record(CallCost(cost_usd=1.0, input_tokens=100))
    tracker.record(CallCost(cost_usd=2.0, input_tokens=200))
    total = tracker.total_call_cost
    assert total.cost_usd == 3.0
    assert total.input_tokens == 300
    assert total.known is True


def test_tracker_total_call_cost_unknown_if_one_call_unknown():
    tracker = CostTracker()
    tracker.record(CallCost(cost_usd=1.0))
    tracker.record(CallCost(known=False))
    assert tracker.total_call_cost.known is False


# ---------------------------------------------------------------------------
# Defect 1b — providers.py: parsing `claude -p --output-format stream-json`
# ---------------------------------------------------------------------------


def test_extract_text_delta_from_content_block_delta():
    event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}
    assert providers._extract_text_delta(event) == "hi"


def test_extract_text_delta_ignores_other_event_types():
    assert providers._extract_text_delta({"type": "message_start"}) is None
    assert providers._extract_text_delta({"type": "content_block_stop"}) is None


def test_usage_from_result_parses_real_usage():
    payload = {
        "type": "result",
        "usage": {
            "input_tokens": 12,
            "output_tokens": 34,
            "cache_creation_input_tokens": 56,
            "cache_read_input_tokens": 78,
        },
    }
    usage = providers._usage_from_result(payload)
    assert usage is not None
    assert usage.input_tokens == 12
    assert usage.output_tokens == 34
    assert usage.cache_creation_input_tokens == 56
    assert usage.cache_read_input_tokens == 78


def test_usage_from_result_missing_usage_key_is_none():
    assert providers._usage_from_result({"type": "result"}) is None


def test_usage_from_result_malformed_usage_is_none():
    payload = {"type": "result", "usage": {"input_tokens": "oops"}}
    assert providers._usage_from_result(payload) is None


def test_handle_cli_line_text_delta_yields_text_without_touching_usage():
    box = providers._CLIUsageBox()
    line = json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hello"},
            },
        }
    )
    assert providers._handle_cli_line(line, box) == "hello"
    assert box.usage is None


def test_handle_cli_line_result_records_usage_and_yields_nothing():
    box = providers._CLIUsageBox()
    line = json.dumps(
        {
            "type": "result",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
    )
    assert providers._handle_cli_line(line, box) is None
    assert box.usage is not None
    assert box.usage.input_tokens == 1


def test_handle_cli_line_noise_is_skipped_not_crashed():
    box = providers._CLIUsageBox()
    noisy = "Client.listTools() called but server does not advertise"
    assert providers._handle_cli_line(noisy, box) is None
    assert box.usage is None


def test_handle_cli_line_blank_is_skipped():
    box = providers._CLIUsageBox()
    assert providers._handle_cli_line("   ", box) is None
    assert providers._handle_cli_line("", box) is None


# ---------------------------------------------------------------------------
# Defect 1b, end-to-end — _CLIStream against a mocked subprocess
# ---------------------------------------------------------------------------


class _FakeStdout:
    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _FakeStdin:
    def write(self, _data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeProc:
    def __init__(self, chunks: list[bytes]):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(chunks)

    async def wait(self) -> int:
        return 0


def _patch_subprocess(monkeypatch, chunks: list[bytes]) -> None:
    fake_proc = _FakeProc(chunks)

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)


async def test_cli_stream_yields_text_and_captures_real_usage(monkeypatch):
    lines = [
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hello "},
                },
            }
        ),
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "world"},
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 10,
                    "cache_read_input_tokens": 5,
                },
            }
        ),
    ]
    _patch_subprocess(monkeypatch, [(line + "\n").encode() for line in lines])

    stream = providers._CLIStream("system prompt", "user prompt")
    text = ""
    async with stream:
        async for chunk in stream.text_stream:
            text += chunk
        final = await stream.get_final_message()

    assert text == "Hello world"
    assert final.content[0].text == "Hello world"
    assert final.usage is not None
    assert final.usage.input_tokens == 100
    assert final.usage.cache_read_input_tokens == 5

    # And feeding that real usage through cost_from_usage produces a known cost —
    # the app's default provider can finally report a true, non-zero number.
    cost = cost_from_usage(final.usage, PricingConfig())
    assert cost.known is True
    assert cost.cost_usd > 0


async def test_cli_stream_missing_result_line_leaves_usage_honestly_unknown(monkeypatch):
    lines = [
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "partial output"},
                },
            }
        ),
    ]
    _patch_subprocess(monkeypatch, [(line + "\n").encode() for line in lines])

    stream = providers._CLIStream("sys", "user")
    async with stream:
        async for _chunk in stream.text_stream:
            pass
        final = await stream.get_final_message()

    assert final.content[0].text == "partial output"
    assert final.usage is None  # never a fabricated zero-usage object

    cost = cost_from_usage(final.usage, PricingConfig())
    assert cost.known is False
    assert cost.cost_usd == 0.0


# ---------------------------------------------------------------------------
# Defect 2, end-to-end — core/build.py: a build's metadata reflects ALL of
# its LLM calls (research + resume + cover letter + fit score), not just the
# resume call, and honestly reports "unknown" rather than a partial number
# when any of them couldn't be costed.
# ---------------------------------------------------------------------------


class _FakeUsage2:
    def __init__(self, inp: int, out: int, cache_write: int = 0, cache_read: int = 0):
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_creation_input_tokens = cache_write
        self.cache_read_input_tokens = cache_read


class _FakeFinal2:
    def __init__(self, text: str, usage):
        self.content = [type("Block", (), {"text": text})]
        self.usage = usage


class _FakeBuildStream:
    def __init__(self, text: str, usage):
        self._text = text
        self._usage = usage

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    @property
    def text_stream(self):
        async def _iter():
            yield self._text

        return _iter()

    async def get_final_message(self):
        return _FakeFinal2(self._text, self._usage)


def _build_config(tmp_path: Path) -> BlastJobConfig:
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path / "data")
    cfg.paths.output_dir = str(tmp_path / "out")
    data_dir = Path(cfg.paths.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "work_history.md").write_text(
        "---\nname: Jane Doe\n---\n\nSenior IT exec.\n", encoding="utf-8"
    )
    return cfg


_FIT_SCORE_JSON = (
    '{"overall_score": 80, "jd_alignment_score": 80, "groundedness_score": 90, '
    '"claims": [], "summary": "ok"}'
)

# `run_build` makes FIVE provider calls, not four. Step 1b distills the research
# sidecar into span-verified facts (`core/research_facts.distill_research`), and
# that module imports its OWN `make_stream` -- so patching
# `blastjob.core.build.make_stream` does not cover it.
#
# Until this fake existed every `run_build` test in this file made a REAL,
# billable provider call. It recorded nothing only because `distill_research` is
# fail-soft, which is exactly why the full suite stayed green while
# `test_run_build_preserves_partial_cost_when_resume_generation_fails` failed
# when run alone (3 CallCosts instead of 2, the extra one carrying real cache
# tokens and a real dollar cost).
_DISTILL_USAGE = _FakeUsage2(400, 50)
_LETTER_SCORE_USAGE = _FakeUsage2(600, 80)
_COVERAGE_USAGE = _FakeUsage2(700, 90)


def _patch_side_calls(monkeypatch):
    """Mock the two provider calls `run_build` makes through modules that bind
    their OWN `make_stream`, so neither reaches the network:

    - Step 1b research distillation (`core/research_facts`)
    - Step 7a coverage-gap analysis (`core/coverage`)
    - cover-letter scoring (`core/cover_letter_score`), only when the build
      includes a letter

    Both record into the caller's CostTracker, so both belong in any expected
    total. Each fake returns the fail-soft-shaped payload for its caller; these
    tests are about cost accounting, not extraction or scoring quality.
    """

    async def fake_distill_stream(_cfg, _system, _messages, max_tokens=4096):
        return _FakeBuildStream("[]", _DISTILL_USAGE)

    async def fake_letter_score_stream(_cfg, _system, _messages, max_tokens=4096):
        return _FakeBuildStream("{}", _LETTER_SCORE_USAGE)

    async def fake_coverage_stream(_cfg, _system, _messages, max_tokens=4096):
        return _FakeBuildStream("[]", _COVERAGE_USAGE)

    monkeypatch.setattr("blastjob.core.research_facts.make_stream", fake_distill_stream)
    monkeypatch.setattr("blastjob.core.cover_letter_score.make_stream", fake_letter_score_stream)
    monkeypatch.setattr("blastjob.core.coverage.make_stream", fake_coverage_stream)


async def test_run_build_metadata_reflects_every_call(tmp_path, monkeypatch):
    cfg = _build_config(tmp_path)

    async def fake_research(cfg_, _company, cost_tracker, _max_searches=3):
        cost_tracker.record(cost_from_usage(_FakeUsage2(1000, 100), cfg_.pricing))
        return "Some research.\n"

    monkeypatch.setattr("blastjob.core.build.make_research_call", fake_research)
    _patch_side_calls(monkeypatch)

    call_order = [
        _FakeBuildStream("# Resume\n\nDid stuff.", _FakeUsage2(2000, 500)),
        _FakeBuildStream(
            "Dear Hiring Manager,\n\nI am excited.\n\nSincerely,\nJane Doe",
            _FakeUsage2(500, 200),
        ),
        _FakeBuildStream(_FIT_SCORE_JSON, _FakeUsage2(300, 100)),
    ]

    async def fake_make_stream(_cfg, _system, _messages, max_tokens=4096):
        return call_order.pop(0)

    monkeypatch.setattr("blastjob.core.build.make_stream", fake_make_stream)
    monkeypatch.setattr("blastjob.core.scoring.make_stream", fake_make_stream)

    tracker = CostTracker()
    out_dir, _fit_score = await run_build(
        company="Acme",
        role="VP Engineering",
        job_description="Lead engineering.",
        formats={"md"},
        use_ats=False,
        app_config=cfg,
        cost_tracker=tracker,
        include_cover_letter=True,
    )

    meta_files = list(out_dir.glob("*.metadata.json"))
    assert len(meta_files) == 1
    data = json.loads(meta_files[0].read_text(encoding="utf-8"))

    expected_total = sum(
        cost_from_usage(u, cfg.pricing).cost_usd
        for u in (
            _FakeUsage2(1000, 100),  # company research
            _DISTILL_USAGE,  # research distillation (Step 1b)
            _FakeUsage2(2000, 500),  # resume
            _FakeUsage2(500, 200),  # cover letter
            _LETTER_SCORE_USAGE,  # cover-letter scoring
            _COVERAGE_USAGE,  # coverage-gap analysis (Step 7a)
            _FakeUsage2(300, 100),  # fit score
        )
    )

    # Defect 2: the build's reported cost is the SUM of every call.
    assert data["build_cost_usd"] == pytest.approx(round(expected_total, 6))
    assert data["build_cost_usd"] > data["resume_cost_usd"]

    # The legacy `cost_usd` key (still read by the history reader) must also
    # reflect the WHOLE build now, not resume-only.
    assert data["cost_usd"] == pytest.approx(round(expected_total, 6))

    # The caller's tracker accumulates the same total (nothing lost).
    assert tracker.total_cost == pytest.approx(round(expected_total, 6))


async def test_run_build_metadata_not_polluted_by_prior_session_calls(tmp_path, monkeypatch):
    """A long-lived tracker may already hold unrelated cost
    from earlier operations. This build's own metadata must reflect only this
    build's calls — never the whole session's running total."""
    cfg = _build_config(tmp_path)

    async def fake_research(cfg_, _company, cost_tracker, _max_searches=3):
        cost_tracker.record(cost_from_usage(_FakeUsage2(100, 10), cfg_.pricing))
        return "research\n"

    monkeypatch.setattr("blastjob.core.build.make_research_call", fake_research)
    _patch_side_calls(monkeypatch)

    async def fake_make_stream(_cfg, _system, _messages, max_tokens=4096):
        return _FakeBuildStream("some text", _FakeUsage2(100, 10))

    monkeypatch.setattr("blastjob.core.build.make_stream", fake_make_stream)
    monkeypatch.setattr("blastjob.core.scoring.make_stream", fake_make_stream)

    tracker = CostTracker()
    tracker.record(CallCost(cost_usd=99.0))  # unrelated prior session cost

    out_dir, _fit_score = await run_build(
        company="Acme",
        role="Role",
        job_description="JD",
        formats={"md"},
        use_ats=False,
        app_config=cfg,
        cost_tracker=tracker,
    )

    meta = json.loads(next(out_dir.glob("*.metadata.json")).read_text(encoding="utf-8"))
    assert meta["build_cost_usd"] < 1.0  # not 99 + this build's small cost
    assert tracker.total_cost > 99.0  # but the session tracker still accumulated it


async def test_run_build_preserves_partial_cost_when_resume_generation_fails(tmp_path, monkeypatch):
    """A build that fails partway (e.g. an empty resume response) must not
    lose the cost of the calls it DID make from the caller's tracker — each
    call is still recorded immediately, not batched up for a happy-path-only
    flush at the end."""
    cfg = _build_config(tmp_path)

    async def fake_research(cfg_, _company, cost_tracker, _max_searches=3):
        cost_tracker.record(cost_from_usage(_FakeUsage2(1000, 100), cfg_.pricing))
        return "research\n"

    monkeypatch.setattr("blastjob.core.build.make_research_call", fake_research)
    _patch_side_calls(monkeypatch)

    async def fake_make_stream(_cfg, _system, _messages, max_tokens=4096):
        return _FakeBuildStream("", _FakeUsage2(50, 5))  # empty -> run_build raises

    monkeypatch.setattr("blastjob.core.build.make_stream", fake_make_stream)

    tracker = CostTracker()
    with pytest.raises(ValueError, match="empty content"):
        await run_build(
            company="Acme",
            role="Role",
            job_description="JD",
            formats={"md"},
            use_ats=False,
            app_config=cfg,
            cost_tracker=tracker,
        )

    # No metadata.json was ever written (the build failed), but the caller's
    # tracker must still show the real spend of the calls that DID complete.
    assert tracker.total_cost > 0
    # research + research-distillation + the resume call that returned empty
    assert len(tracker.calls) == 3


async def test_run_build_metadata_cost_is_null_when_any_call_is_unknown(tmp_path, monkeypatch):
    cfg = _build_config(tmp_path)

    async def fake_research(cfg_, _company, cost_tracker, _max_searches=3):
        # Simulate a claude-cli usage-parse failure on this one call.
        cost_tracker.record(cost_from_usage(None, cfg_.pricing))
        return "research\n"

    monkeypatch.setattr("blastjob.core.build.make_research_call", fake_research)
    _patch_side_calls(monkeypatch)

    async def fake_make_stream(_cfg, _system, _messages, max_tokens=4096):
        return _FakeBuildStream("some text", _FakeUsage2(100, 10))

    monkeypatch.setattr("blastjob.core.build.make_stream", fake_make_stream)
    monkeypatch.setattr("blastjob.core.scoring.make_stream", fake_make_stream)

    tracker = CostTracker()
    out_dir, _fit_score = await run_build(
        company="Acme",
        role="Role",
        job_description="JD",
        formats={"md"},
        use_ats=False,
        app_config=cfg,
        cost_tracker=tracker,
    )

    meta = json.loads(next(out_dir.glob("*.metadata.json")).read_text(encoding="utf-8"))
    # The new, unambiguous field is the honest one: never a fabricated
    # partial number passed off as the whole build's cost.
    assert meta["build_cost_usd"] is None
    assert meta["build_cache_hit_ratio"] is None
    # The resume call itself WAS known, and is reported under its own name.
    assert meta["resume_cost_usd"] is not None
    # The legacy `cost_usd`/`cache_hit_ratio` keys stay numeric (0.0), never
    # `null`: an older history reader formats them straight into
    # an f-string with no None-guard (`f"${entry.cost_usd:.4f}"`), so writing
    # `null` there would crash it outright. 0.0 is an imperfect "unknown" on
    # that legacy surface — no worse than this app's pre-existing bug class —
    # but the WEB surface (the one docs/how-it-works/llm.md calls authoritative)
    # already treats a falsy `cost_usd` as "show the unknown dash", so this
    # reads correctly there today with zero integration patch.
    assert meta["cost_usd"] == 0.0
    assert meta["cache_hit_ratio"] == 0.0

    # And feeding the real (unmodified) core/history.py reader this exact
    # file must not crash, whichever key it happens to read.
    from blastjob.core.history import scan_history

    entries = scan_history(out_dir.parent)
    assert len(entries) == 1
    # Unknown, not free -- scan_history prefers build_cost_usd.
    assert entries[0].cost_usd is None


# ---------------------------------------------------------------------------
# Old metadata files on disk (pre-dating this fix) must still read cleanly.
# `core/history.py` is not owned by this workstream — this test exercises its
# real, unmodified reader against both an old-shape file and a new-shape one,
# to lock in "no crash" as the contract this fix must not break.
# ---------------------------------------------------------------------------


def test_legacy_metadata_shape_still_reads_without_crashing(tmp_path):
    from blastjob.core.history import scan_history

    out_root = tmp_path / "out"
    run_dir = out_root / "northcastle-2026-10-03"
    run_dir.mkdir(parents=True)
    # A real pre-fix metadata.json shape: flat `cost_usd`/`cache_hit_ratio`,
    # both fabricated 0.0 for a claude-cli build (this is the exact bug).
    legacy = {
        "timestamp": "2026-10-03T11:22:58.417903",
        "company": "Northcastle Benefits",
        "role": "Director, Business Infrastructure",
        "confidential": False,
        "provider": "claude-cli",
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "cache_hit_ratio": 0.0,
        "formats": ["docx", "md"],
        "ats_mode": False,
        "fit_score": 82,
        "groundedness_score": 84,
        "unsupported_claims": 6,
        "include_cover_letter": True,
        "cover_letter_cost_usd": 0.0,
        "cover_letter_input_tokens": 0,
        "cover_letter_output_tokens": 0,
    }
    (run_dir / "director-business-infrastructure.metadata.json").write_text(
        json.dumps(legacy), encoding="utf-8"
    )

    entries = scan_history(out_root)  # must not raise
    assert len(entries) == 1
    assert entries[0].company == "Northcastle Benefits"


def test_new_build_only_fields_being_null_does_not_confuse_the_legacy_reader(tmp_path):
    """`core/history.py` now prefers the `build_*` fields and falls back to the
    legacy keys, so a file written in the new shape reads cleanly either way.

    When `build_cost_usd` is null the reader reports None -- unknown -- rather
    than the legacy 0.0 sitting beside it. That is the point: the claude-CLI
    path used to hardcode zero usage, so a 0.0 could never be distinguished from
    a genuinely free call. The legacy keys stay numeric only so an older,
    None-unsafe reader cannot crash on them."""
    from blastjob.core.history import scan_history

    out_root = tmp_path / "out"
    run_dir = out_root / "acme-2026-09-12"
    run_dir.mkdir(parents=True)
    new_shape = {
        "timestamp": "2026-09-12T00:00:00",
        "company": "Acme",
        "role": "VP Eng",
        "confidential": False,
        "provider": "claude-cli",
        "build_cost_usd": None,
        "build_input_tokens": 0,
        "build_output_tokens": 0,
        "build_cache_creation_tokens": 0,
        "build_cache_read_tokens": 0,
        "build_cache_hit_ratio": None,
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "cache_hit_ratio": 0.0,
        "resume_cost_usd": None,
        "formats": ["md"],
        "ats_mode": False,
        "fit_score": None,
        "groundedness_score": None,
        "unsupported_claims": None,
        "include_cover_letter": False,
        "cover_letter_cost_usd": None,
        "cover_letter_input_tokens": None,
        "cover_letter_output_tokens": None,
    }
    (run_dir / "vp-eng.metadata.json").write_text(json.dumps(new_shape), encoding="utf-8")

    entries = scan_history(out_root)  # must not raise
    assert len(entries) == 1
    # Unknown, not free -- scan_history prefers build_cost_usd.
    assert entries[0].cost_usd is None
    assert entries[0].cache_hit_ratio is None


# ---------- #30: OpenAI with no usage block is unknown, not a known $0 ----------


class _FakeOpenAICompletionStream:
    """Stands in for `client.chat.completions.stream(...)`; `usage` is what the
    final completion carries (None = the provider sent no usage block)."""

    def __init__(self, usage):
        self._usage = usage

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def __aiter__(self):
        async def gen():
            delta = type("D", (), {"content": "hi"})
            yield type("Chunk", (), {"choices": [type("C", (), {"delta": delta})]})

        return gen()

    async def get_final_completion(self):
        return type("Final", (), {"usage": self._usage})


class _FakeOpenAIClient:
    def __init__(self, usage):
        completions = type(
            "Comp", (), {"stream": lambda _s, **kw: _FakeOpenAICompletionStream(usage)}
        )
        self.chat = type("Chat", (), {"completions": completions()})()


async def _openai_final(usage):
    stream = providers._OpenAIStream(_FakeOpenAIClient(usage), "m", "sys", "user")
    async with stream:
        async for _ in stream.text_stream:
            pass
        return await stream.get_final_message()


async def test_openai_stream_without_usage_block_costs_unknown_not_zero():
    final = await _openai_final(None)

    cost = cost_from_usage(final.usage, PricingConfig())

    assert final.usage is None
    assert cost.known is False


async def test_openai_stream_with_usage_block_costs_known():
    usage = type("U", (), {"prompt_tokens": 100, "completion_tokens": 20})()

    final = await _openai_final(usage)

    cost = cost_from_usage(final.usage, PricingConfig())
    assert cost.known is True
    assert cost.input_tokens == 100
    assert cost.output_tokens == 20


def test_describe_cost_known_and_unknown():
    from blastjob.llm.cost import describe_cost

    assert describe_cost(CallCost(cost_usd=0.1234)) == "$0.1234"
    assert describe_cost(CallCost(cost_usd=0.5, known=False)) == "unknown"

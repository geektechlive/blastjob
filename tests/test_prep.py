"""Interview prep pack: cache layout, deterministic header, role-prefixed file
placement, the `[confirm]` groundedness marker, and the background Run wrapper.
The LLM is stubbed."""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

import pytest

from blastjob.core import company as co
from blastjob.core import prep
from blastjob.core.paths import slugify
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.prep import make_prep_work
from blastjob.web.runtime import Run

WORK_HISTORY = """---
name: Test Candidate
---

## Experience

Managed a $4.2M budget and 40 staff at Acme.
"""

STREAM_CHUNKS = [
    "## Role at a glance\n",
    "Led a $4.2M budget; grew revenue 35%; ran a $9M",
    " program; floor $175,000; staff 845.\n",
]


@pytest.fixture
def cfg() -> BlastJobConfig:
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    (Path(c.paths.data_dir) / "work_history.md").write_text(WORK_HISTORY, encoding="utf-8")
    return c


@pytest.fixture
def kept(cfg: BlastJobConfig) -> tuple[BlastJobConfig, int]:
    conn = dbc.init_db(cfg)
    jid = dbc.upsert_job(
        conn,
        source="t",
        external_id="1",
        company="Northshore Credit Union",
        title="Generic VP Role",
        # Long enough to be a real description: a snippet is refused (test_jd_snippet).
        raw_jd="Own SRE and observability. " * 30,
        url="https://x/1",
    )
    conn.execute(
        "UPDATE jobs SET review_state='kept', match_gaps='No credit-union experience', "
        "app_notes='screen done' WHERE id=?",
        (jid,),
    )
    conn.commit()
    co.add_comp_signal(
        conn,
        company="Northshore Credit Union",
        job_id=jid,
        kind="asked_minimum",
        low_usd=175_000,
    )
    co.upsert_profile(conn, co.CompanyProfile(company="Northshore Credit Union", employees=845))
    conn.close()
    return cfg, jid


class FakeStream:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def text_stream(self):
        async def gen():
            for chunk in STREAM_CHUNKS:
                yield chunk

        return gen()


@pytest.fixture
def stub_stream(monkeypatch):
    captured: dict = {}

    async def fake_make_stream(cfg_, system, messages, max_tokens=0):
        captured["system"] = system
        captured["user"] = messages[0]["content"]
        captured["max_tokens"] = max_tokens
        return FakeStream()

    monkeypatch.setattr(prep, "make_stream", fake_make_stream)
    return captured


# --- pure units -----------------------------------------------------------


def test_prep_messages_cache_layout_and_material():
    job = {
        "title": "Generic VP Role",
        "company": "Northshore Credit Union",
        "url": "https://x/1",
        "raw_jd": "Own SRE and observability.",
        "match_gaps": "No credit-union experience",
        "match_strengths": "Ran platform teams",
        "rationale": "Strong scope match.",
        "app_notes": "screen done",
    }
    system, user = prep.build_prep_messages(WORK_HISTORY, job, None, [], None)

    assert "cache_control" in system[0]
    assert "cache_control" in user[0] and "$4.2M budget" in user[0]["text"]
    assert "cache_control" not in user[1]
    material = user[1]["text"]
    assert "Own SRE and observability." in material
    assert "No credit-union experience" in material
    assert "<MATERIAL>" in material and "</MATERIAL>" in material


def test_prep_messages_include_profile_and_comp_signals():
    job = {"title": "VP", "company": "Acme", "url": "", "raw_jd": "jd"}
    profile = {
        "summary": "A credit union.",
        "strategy": "Core modernization.",
        "risks": "Vendor lock-in.",
        "questions": "Who owns the roadmap?",
        "it_leadership": [
            {"name": "Jane Doe", "title": "Chief Operating Officer", "role": "executive"}
        ],
        "recent_events": [{"date": "2026-01", "event": "New core selected"}],
        "tech_stack": [{"area": "core", "vendor": "CoreForge"}],
    }
    signals = [{"kind": "asked_minimum", "low_usd": 250000, "source": "recruiter", "note": "ok"}]
    _, user = prep.build_prep_messages(WORK_HISTORY, job, profile, signals, 250000)
    material = user[1]["text"]
    for expected in ("Core modernization.", "Jane Doe", "CoreForge", "New core selected"):
        assert expected in material
    assert "asked_minimum" in material and "$250,000" in material


def test_render_header_is_deterministic():
    job = {
        "title": "Generic VP Role",
        "company": "Northshore Credit Union",
        "url": "https://x/1",
    }
    head = prep.render_header(job, {"workup_path": "/tmp/workup.md"}, date(2026, 9, 5))
    assert head.startswith("# Interview prep — Generic VP Role @ Northshore Credit Union")
    assert "Northshore Credit Union" in head
    assert "Generic VP Role" in head
    assert "https://x/1" in head
    assert "2026-09-05" in head
    assert "/tmp/workup.md" in head


def test_prep_filename_is_role_prefixed():
    assert prep.prep_filename("Generic VP Role") == "generic-vp-role.interview_prep.md"
    assert prep.prep_filename("") == "role.interview_prep.md"


def test_mark_unverified_numbers_flags_only_unsupported_figures():
    body = "Led a $4.2M budget; grew revenue 35%; ran a $9M program; hired 40 staff."
    marked, count = prep.mark_unverified_numbers(body, WORK_HISTORY)
    assert "$9M [confirm]" in marked
    assert "35% [confirm]" in marked
    assert "$4.2M [confirm]" not in marked  # verbatim in the work history
    assert "40 staff" in marked and "40 [confirm]" not in marked  # bare counts untouched
    assert count == 2


def test_mark_unverified_numbers_is_idempotent():
    body = "Ran a $9M program."
    once, first = prep.mark_unverified_numbers(body, WORK_HISTORY)
    twice, second = prep.mark_unverified_numbers(once, WORK_HISTORY)
    assert twice == once
    assert (first, second) == (1, 0)


def test_mark_unverified_numbers_handles_long_amounts():
    marked, count = prep.mark_unverified_numbers("Spend was $1,200,000. Cap is $90K.", "")
    assert "$1,200,000 [confirm]." in marked
    assert "$90K [confirm]." in marked
    assert count == 2


# --- pipeline -------------------------------------------------------------


async def test_generate_prep_writes_marked_pack(kept, stub_stream):
    cfg, jid = kept
    conn = dbc.init_db(cfg)
    try:
        path = await prep.generate_prep(cfg, conn, jid)
    finally:
        conn.close()

    # Prefix-match the day folder rather than a second clock read (midnight flake).
    assert path.name == "generic-vp-role.interview_prep.md"
    assert path.parent.name.startswith(f"{slugify('Northshore Credit Union')}-")
    assert path.parent.parent == Path(cfg.paths.output_dir)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Interview prep — Generic VP Role @ Northshore Credit Union")
    assert "$9M [confirm]" in text
    assert "35% [confirm]" in text
    assert "$4.2M [confirm]" not in text
    assert "$175,000 [confirm]" not in text  # trusted: seeded comp signal + floor
    assert "845 [confirm]" not in text  # trusted: seeded profile employees
    assert stub_stream["max_tokens"] == prep.MAX_PREP_TOKENS


def test_trusted_numbers_text_renders_expected_variants():
    job = {"comp_min": 0, "comp_max": 0}
    profile = {
        "employees": 845,
        "revenue_usd": 5_412_903_210,
        "total_assets_usd": 312_000_000,
        "culture_rating": 3.2,
    }
    signals = [{"low_usd": 175_000, "high_usd": None}]
    text = prep.trusted_numbers_text(job, profile, signals, floor=175_000)
    for expected in (
        "$175,000",
        "$175K",
        "$175k",
        "175,000",
        "$175,000.00",
        "845",
        "845",
        "$5,412,903,210",
        "$5.4B",
        "$5.41B",
        "$312M",
        "$312,000,000",
        "3.2",
    ):
        assert expected in text


async def test_generate_prep_rejects_unknown_job(cfg, stub_stream):
    conn = dbc.init_db(cfg)
    try:
        with pytest.raises(ValueError, match="job not found"):
            await prep.generate_prep(cfg, conn, 9999)
    finally:
        conn.close()


async def test_prep_work_streams_and_reports_path(kept, stub_stream):
    cfg, jid = kept
    run = Run(id="p1", name="prep")
    result = await make_prep_work(jid, load_config=lambda: cfg)(run)

    assert result is not None and Path(result["path"]).exists()
    outputs = [e for e in run.events if e.kind == "output"]
    assert outputs
    streamed = "".join(e.data for e in outputs)
    text = Path(result["path"]).read_text(encoding="utf-8")
    # Marks are applied per line as the stream flows, not only after the fact:
    # the streamed-to-browser text already carries the marks the saved file has.
    assert "$9M [confirm]" in streamed
    assert text.endswith("\n\n" + streamed)
    logs = [e.data for e in run.events if e.kind == "log"]
    assert any("Saved" in line for line in logs)
    assert any("Generic VP Role" in line for line in logs)


async def test_prep_work_reports_unknown_job(cfg, stub_stream):
    run = Run(id="p2", name="prep")
    result = await make_prep_work(9999, load_config=lambda: cfg)(run)
    assert result is None
    assert [e for e in run.events if e.kind == "error"] and run.events[-1].data == "job not found"

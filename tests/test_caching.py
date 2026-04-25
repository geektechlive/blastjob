"""Unit tests for llm/caching.py."""

from blastjob.llm.caching import (
    build_cover_letter_messages,
    build_coverage_messages,
    build_ingest_messages,
    build_refine_messages,
    build_resume_messages,
    build_score_messages,
    cached_text,
    plain_text,
)

# ---------------------------------------------------------------------------
# Block helpers
# ---------------------------------------------------------------------------


def test_cached_text_has_cache_control():
    block = cached_text("hello")
    assert block["type"] == "text"
    assert block["text"] == "hello"
    assert block["cache_control"] == {"type": "ephemeral"}


def test_plain_text_no_cache_control():
    block = plain_text("hello")
    assert block["type"] == "text"
    assert block["text"] == "hello"
    assert "cache_control" not in block


# ---------------------------------------------------------------------------
# build_ingest_messages
# ---------------------------------------------------------------------------


def test_build_ingest_messages_structure():
    chunks = ["<file>chunk1</file>", "<file>chunk2</file>"]
    system, user = build_ingest_messages("System prompt.", chunks)
    assert len(system) == 1
    assert system[0]["cache_control"] == {"type": "ephemeral"}  # system is cached
    assert len(user) == 1
    assert "chunk1" in user[0]["text"]
    assert "chunk2" in user[0]["text"]
    assert "cache_control" not in user[0]  # user content is plain


def test_build_ingest_messages_combines_chunks():
    system, user = build_ingest_messages("sys", ["a", "b", "c"])
    assert user[0]["text"] == "a\n\nb\n\nc"


def test_build_ingest_messages_empty_chunks():
    system, user = build_ingest_messages("sys", [])
    assert user[0]["text"] == ""


# ---------------------------------------------------------------------------
# build_resume_messages
# ---------------------------------------------------------------------------


def test_build_resume_messages_three_cache_breakpoints():
    system, user = build_resume_messages(
        "Resume system prompt.",
        "Work history content.",
        "Template content.",
        "Job description.",
        "Company research.",
    )
    # BP1: system prompt cached
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    # BP2: work_history cached
    assert user[0]["cache_control"] == {"type": "ephemeral"}
    assert user[0]["text"] == "Work history content."
    # BP3: template cached
    assert user[1]["cache_control"] == {"type": "ephemeral"}
    assert user[1]["text"] == "Template content."
    # JD + research: plain (no cache_control)
    assert "cache_control" not in user[2]
    assert "Job description." in user[2]["text"]
    assert "Company research." in user[2]["text"]


def test_build_resume_messages_user_block_count():
    _, user = build_resume_messages("sys", "history", "template", "jd", "research")
    assert len(user) == 3


def test_build_resume_messages_empty_research():
    _, user = build_resume_messages("sys", "history", "template", "jd", "")
    assert "## Company Research" in user[2]["text"]


# ---------------------------------------------------------------------------
# build_score_messages
# ---------------------------------------------------------------------------


def test_build_score_messages_structure():
    system, user = build_score_messages(
        "Score system prompt.",
        "Work history.",
        "Resume markdown.",
        "Job description.",
    )
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert user[0]["cache_control"] == {"type": "ephemeral"}
    assert user[0]["text"] == "Work history."
    assert "cache_control" not in user[1]
    assert "Resume markdown." in user[1]["text"]
    assert "Job description." in user[1]["text"]


def test_build_score_messages_user_block_count():
    _, user = build_score_messages("sys", "history", "resume", "jd")
    assert len(user) == 2


# ---------------------------------------------------------------------------
# build_cover_letter_messages
# ---------------------------------------------------------------------------


def test_build_cover_letter_messages_three_cache_breakpoints():
    system, user = build_cover_letter_messages(
        "Cover system prompt.",
        "Work history content.",
        "Resume markdown.",
        "Job description.",
        "Company research.",
    )
    # BP1: system cached
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    # BP2: work_history cached (warm from resume gen)
    assert user[0]["cache_control"] == {"type": "ephemeral"}
    assert user[0]["text"] == "Work history content."
    # BP3: resume cached
    assert user[1]["cache_control"] == {"type": "ephemeral"}
    assert user[1]["text"] == "Resume markdown."
    # JD + research: plain
    assert "cache_control" not in user[2]
    assert "Job description." in user[2]["text"]
    assert "Company research." in user[2]["text"]


def test_build_cover_letter_messages_user_block_count():
    _, user = build_cover_letter_messages("sys", "history", "resume", "jd", "research")
    assert len(user) == 3


# ---------------------------------------------------------------------------
# build_refine_messages
# ---------------------------------------------------------------------------


def test_build_refine_messages_three_cache_breakpoints():
    system, user = build_refine_messages(
        "Refine system prompt.",
        "Work history.",
        "Current resume.",
        "Make it shorter.",
        "Job description.",
    )
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert user[0]["cache_control"] == {"type": "ephemeral"}
    assert user[0]["text"] == "Work history."
    assert user[1]["cache_control"] == {"type": "ephemeral"}
    assert user[1]["text"] == "Current resume."
    assert "cache_control" not in user[2]
    assert "Make it shorter." in user[2]["text"]
    assert "Job description." in user[2]["text"]


def test_build_refine_messages_user_block_count():
    _, user = build_refine_messages("sys", "history", "resume", "feedback", "jd")
    assert len(user) == 3


# ---------------------------------------------------------------------------
# build_coverage_messages
# ---------------------------------------------------------------------------


def test_build_coverage_messages_two_cache_breakpoints():
    system, user = build_coverage_messages(
        "Coverage system prompt.", "Work history.", "Job description."
    )
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert user[0]["cache_control"] == {"type": "ephemeral"}
    assert user[0]["text"] == "Work history."
    assert "cache_control" not in user[1]
    assert "Job description." in user[1]["text"]


def test_build_coverage_messages_user_block_count():
    _, user = build_coverage_messages("sys", "history", "jd")
    assert len(user) == 2

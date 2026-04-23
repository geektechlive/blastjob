"""Unit tests for llm/providers.py — detect_provider and active_model."""

import pytest

from blastjob.llm.providers import (
    ProviderNotConfiguredError,
    _OpenAIFakeUsage,
    _OpenAIFinalMessage,
    _strip_cache_control,
    _system_to_str,
    _user_content_to_str,
    active_model,
    detect_provider,
)
from blastjob.models.config import BlastJobConfig, LLMConfig


def _cfg(
    provider: str = "auto",
    anthropic_env: str = "ANTHROPIC_API_KEY",
    openai_env: str = "OPENAI_API_KEY",
) -> LLMConfig:
    return LLMConfig(
        provider=provider,
        anthropic_api_key_env=anthropic_env,
        openai_api_key_env=openai_env,
    )


# ---------------------------------------------------------------------------
# detect_provider
# ---------------------------------------------------------------------------


def test_detect_provider_explicit_anthropic():
    cfg = _cfg(provider="anthropic")
    assert detect_provider(cfg) == "anthropic"


def test_detect_provider_explicit_openai():
    cfg = _cfg(provider="openai")
    assert detect_provider(cfg) == "openai"


def test_detect_provider_explicit_claude_cli():
    cfg = _cfg(provider="claude-cli")
    assert detect_provider(cfg) == "claude-cli"


def test_detect_provider_auto_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = _cfg(provider="auto")
    assert detect_provider(cfg) == "anthropic"


def test_detect_provider_auto_openai(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    cfg = _cfg(provider="auto")
    assert detect_provider(cfg) == "openai"


def test_detect_provider_auto_anthropic_takes_priority(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    cfg = _cfg(provider="auto")
    assert detect_provider(cfg) == "anthropic"


def test_detect_provider_auto_no_provider_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Patch shutil.which to ensure claude binary not found
    import blastjob.llm.providers as mod

    original = mod.shutil.which
    try:
        mod.shutil.which = lambda _: None
        cfg = _cfg(provider="auto")
        with pytest.raises(ProviderNotConfiguredError):
            detect_provider(cfg)
    finally:
        mod.shutil.which = original


# ---------------------------------------------------------------------------
# active_model
# ---------------------------------------------------------------------------


def test_active_model_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = BlastJobConfig()
    result = active_model(cfg)
    assert result.startswith("anthropic /")
    assert "claude" in result


def test_active_model_openai(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    cfg = BlastJobConfig()
    result = active_model(cfg)
    assert result.startswith("openai /")


def test_active_model_no_provider(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import blastjob.llm.providers as mod

    original = mod.shutil.which
    try:
        mod.shutil.which = lambda _: None
        cfg = BlastJobConfig()
        result = active_model(cfg)
        assert result == "no provider configured"
    finally:
        mod.shutil.which = original


def test_detect_provider_auto_claude_cli(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import blastjob.llm.providers as mod

    original = mod.shutil.which
    try:
        mod.shutil.which = lambda name: "/usr/local/bin/claude" if name == "claude" else None
        cfg = _cfg(provider="auto")
        assert detect_provider(cfg) == "claude-cli"
    finally:
        mod.shutil.which = original


def test_active_model_claude_cli(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import blastjob.llm.providers as mod

    original = mod.shutil.which
    try:
        mod.shutil.which = lambda name: "/usr/local/bin/claude" if name == "claude" else None
        cfg = BlastJobConfig()
        result = active_model(cfg)
        assert result == "claude-cli"
    finally:
        mod.shutil.which = original


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def test_strip_cache_control_removes_cache_key():
    blocks = [{"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}]
    result = _strip_cache_control(blocks)
    assert result == [{"type": "text", "text": "hello"}]
    assert "cache_control" not in result[0]


def test_strip_cache_control_passes_non_text_blocks():
    non_text = {"type": "image", "source": "data"}
    result = _strip_cache_control([non_text])
    assert result == [non_text]


def test_strip_cache_control_mixed_blocks():
    blocks = [
        {"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "b"},
    ]
    result = _strip_cache_control(blocks)
    assert result[0] == {"type": "text", "text": "a"}
    assert result[1] == {"type": "text", "text": "b"}


def test_system_to_str_joins_blocks():
    system = [{"type": "text", "text": "Block A"}, {"type": "text", "text": "Block B"}]
    result = _system_to_str(system)
    assert result == "Block A\n\nBlock B"


def test_system_to_str_non_dict_block():
    result = _system_to_str(["plain string"])
    assert result == "plain string"


def test_user_content_to_str_joins_blocks():
    content = [{"type": "text", "text": "Part 1"}, {"type": "text", "text": "Part 2"}]
    result = _user_content_to_str(content)
    assert result == "Part 1\n\nPart 2"


def test_openai_fake_usage_attributes():
    usage = _OpenAIFakeUsage(100, 50)
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cache_creation_input_tokens == 0
    assert usage.cache_read_input_tokens == 0


def test_openai_final_message_attributes():
    msg = _OpenAIFinalMessage("resume text", 100, 50)
    assert msg.content[0].text == "resume text"
    assert msg.usage.input_tokens == 100
    assert msg.usage.output_tokens == 50

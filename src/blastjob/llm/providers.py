"""
Provider abstraction — Anthropic, OpenAI, or claude-cli fallback.

Auto-detection order when provider = "auto":
  1. ANTHROPIC_API_KEY in environment → use Anthropic SDK
  2. OPENAI_API_KEY in environment → use OpenAI SDK
  3. `claude` binary in PATH → use claude-cli subprocess
  4. Nothing found → raise ProviderNotConfiguredError with setup instructions
"""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator

from blastjob.models.config import BlastJobConfig, LLMConfig


class ProviderNotConfiguredError(Exception):
    pass


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def detect_provider(cfg: LLMConfig) -> str:
    """Return the concrete provider name to use."""
    if cfg.provider != "auto":
        return cfg.provider

    if os.environ.get(cfg.anthropic_api_key_env):
        return "anthropic"
    if os.environ.get(cfg.openai_api_key_env):
        return "openai"
    if shutil.which("claude"):
        return "claude-cli"

    raise ProviderNotConfiguredError(
        "No AI provider found. Set one of:\n"
        "  ANTHROPIC_API_KEY — Anthropic API key (get one at console.anthropic.com)\n"
        "  OPENAI_API_KEY    — OpenAI API key (get one at platform.openai.com)\n"
        "  or install Claude Code CLI (claude.ai/code) — blastjob will use it automatically\n\n"
        "Then restart blastjob, or set the key in Settings."
    )


def active_model(cfg: BlastJobConfig) -> str:
    """Return a display string like 'anthropic / claude-sonnet-4-6'."""
    try:
        provider = detect_provider(cfg.llm)
    except ProviderNotConfiguredError:
        return "no provider configured"

    if provider == "anthropic":
        return f"anthropic / {cfg.llm.anthropic_model}"
    if provider == "openai":
        return f"openai / {cfg.llm.openai_model}"
    return "claude-cli"


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


async def anthropic_stream(
    cfg: LLMConfig,
    system: list[dict],
    messages: list[dict],
    max_tokens: int,
) -> tuple[AsyncIterator[str], object]:
    """Returns (text_iterator, final_message_awaitable)."""
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=os.environ.get(cfg.anthropic_api_key_env))
    stream = client.messages.stream(
        model=cfg.anthropic_model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    return stream


async def anthropic_create(
    cfg: LLMConfig,
    system: list[dict],
    messages: list[dict],
    tools: list[dict],
    max_tokens: int,
):
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=os.environ.get(cfg.anthropic_api_key_env))
    return await client.messages.create(
        model=cfg.anthropic_model,
        max_tokens=max_tokens,
        system=system,
        tools=tools,
        messages=messages,
    )


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


def _strip_cache_control(blocks: list[dict]) -> list[dict]:
    """Convert Anthropic cache_control blocks to plain OpenAI-compatible content."""
    result = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            result.append({"type": "text", "text": block["text"]})
        else:
            result.append(block)
    return result


def _system_to_str(system: list[dict]) -> str:
    return "\n\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in system)


def _user_content_to_str(content: list[dict]) -> str:
    return "\n\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)


class _OpenAIFakeUsage:
    """Mirrors Anthropic usage shape so cost_from_usage() works unchanged."""

    def __init__(self, prompt_tokens: int, completion_tokens: int):
        self.input_tokens = prompt_tokens
        self.output_tokens = completion_tokens
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0


class _OpenAIFinalMessage:
    def __init__(self, text: str, prompt_tokens: int, completion_tokens: int):
        self.content = [type("Block", (), {"text": text})]
        self.usage = _OpenAIFakeUsage(prompt_tokens, completion_tokens)


async def openai_stream(
    cfg: LLMConfig,
    system: list[dict],
    messages: list[dict],
    max_tokens: int = 4096,
) -> _OpenAIStream:
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise ProviderNotConfiguredError(
            "OpenAI provider requires the openai package: pip install openai"
        )
    client = AsyncOpenAI(api_key=os.environ.get(cfg.openai_api_key_env))
    system_str = _system_to_str(system)
    user_str = _user_content_to_str(messages[0]["content"] if messages else [])
    return _OpenAIStream(client, cfg.openai_model, system_str, user_str, max_tokens)


class _OpenAIStream:
    def __init__(self, client, model: str, system_str: str, user_str: str, max_tokens: int = 4096):
        self._client = client
        self._model = model
        self._system_str = system_str
        self._user_str = user_str
        self._max_tokens = max_tokens
        self._full_text = ""
        self._usage = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    @property
    def text_stream(self):
        return self._iter_text()

    async def _iter_text(self):
        async with self._client.chat.completions.stream(
            model=self._model,
            messages=[
                {"role": "system", "content": self._system_str},
                {"role": "user", "content": self._user_str},
            ],
            max_tokens=self._max_tokens,
        ) as stream:
            async for chunk in stream:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    self._full_text += delta
                    yield delta
            final = await stream.get_final_completion()
            usage = final.usage
            self._usage = _OpenAIFakeUsage(
                usage.prompt_tokens if usage else 0,
                usage.completion_tokens if usage else 0,
            )

    async def get_final_message(self):
        usage = self._usage or _OpenAIFakeUsage(0, 0)
        return _OpenAIFinalMessage(self._full_text, usage.input_tokens, usage.output_tokens)


# ---------------------------------------------------------------------------
# claude-cli
# ---------------------------------------------------------------------------


async def claudecli_stream(prompt: str) -> AsyncIterator[str]:
    """Stream output from `claude -p "..."` subprocess."""
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        "claude",
        "-p",
        prompt,
        "--output-format",
        "text",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def _iter():
        while True:
            chunk = await proc.stdout.read(256)
            if not chunk:
                break
            yield chunk.decode("utf-8", errors="replace")
        await proc.wait()

    return _iter()


class _CLIFinalMessage:
    def __init__(self, text: str):
        self.content = [type("Block", (), {"text": text})]
        self.usage = _OpenAIFakeUsage(0, 0)


class _CLIStream:
    def __init__(self, prompt: str):
        self._prompt = prompt
        self._full_text = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    @property
    def text_stream(self):
        return self._iter()

    async def _iter(self):
        async for chunk in await claudecli_stream(self._prompt):
            self._full_text += chunk
            yield chunk

    async def get_final_message(self):
        return _CLIFinalMessage(self._full_text)


# ---------------------------------------------------------------------------
# Unified stream factory
# ---------------------------------------------------------------------------


async def make_stream(
    cfg: BlastJobConfig,
    system: list[dict],
    messages: list[dict],
    max_tokens: int = 4096,
):
    """Return an async context manager that yields text_stream and get_final_message()."""
    provider = detect_provider(cfg.llm)

    if provider == "anthropic":
        return await anthropic_stream(cfg.llm, system, messages, max_tokens)

    if provider == "openai":
        return await openai_stream(cfg.llm, system, messages, max_tokens)

    if provider == "claude-cli":
        # Flatten to a single prompt string for the CLI
        system_str = _system_to_str(system)
        user_str = _user_content_to_str(messages[0]["content"] if messages else [])
        return _CLIStream(f"{system_str}\n\n{user_str}")

    raise ProviderNotConfiguredError(f"Unknown provider: {provider}")


async def make_research_call(
    cfg: BlastJobConfig,
    company: str,
    max_searches: int = 3,
) -> str:
    """Company research — only Anthropic supports web_search natively.
    OpenAI and claude-cli fall back to a plain completion from training data."""
    from blastjob.llm import caching
    from blastjob.llm.prompts import RESEARCH_PROMPT, RESEARCH_PROMPT_KNOWLEDGE

    provider = detect_provider(cfg.llm)

    if provider == "anthropic":
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=os.environ.get(cfg.llm.anthropic_api_key_env))
        response = await client.messages.create(
            model=cfg.llm.anthropic_model,
            max_tokens=2048,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}],
            messages=[{"role": "user", "content": RESEARCH_PROMPT.format(company=company)}],
        )
        parts = [b.text for b in response.content if hasattr(b, "text") and b.text]
        return "\n\n".join(parts) or "No research available."

    # OpenAI / claude-cli: plain completion from training data, no live web search
    knowledge_prompt = RESEARCH_PROMPT_KNOWLEDGE.format(company=company)
    system = [caching.plain_text("You are a company research assistant.")]
    messages = [{"role": "user", "content": [caching.plain_text(knowledge_prompt)]}]
    full_text = ""
    stream = await make_stream(cfg, system, messages, max_tokens=1024)
    async with stream:
        async for chunk in stream.text_stream:
            full_text += chunk
    footer = "\n\n*Research based on training data — live web search requires Anthropic API.*"
    return full_text + footer

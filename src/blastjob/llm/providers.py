"""
Provider abstraction — Anthropic, OpenAI, or claude-cli fallback.

Auto-detection order when provider = "auto":
  1. ANTHROPIC_API_KEY in environment → use Anthropic SDK
  2. OPENAI_API_KEY in environment → use OpenAI SDK
  3. `claude` binary in PATH → use claude-cli subprocess
  4. Nothing found → raise ProviderNotConfiguredError with setup instructions
"""

from __future__ import annotations

import asyncio
import atexit
import codecs
import contextlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Awaitable
from typing import TYPE_CHECKING, TypeVar, cast

from blastjob.llm.child_env import child_env
from blastjob.llm.cost import CostTracker, cost_from_usage
from blastjob.models.config import BlastJobConfig, LLMConfig

if TYPE_CHECKING:
    from anthropic.lib.streaming import AsyncMessageStreamManager
    from anthropic.types import MessageParam, TextBlockParam


log = logging.getLogger(__name__)


class ProviderNotConfiguredError(Exception):
    pass


class CLIError(RuntimeError):
    """`claude -p` failed: non-zero exit, an `is_error` result, or a timeout."""


# `claude -p` is the full Claude Code AGENT, not a bare completion. Without these
# it loads the cwd's CLAUDE.md, enables tools, and replies conversationally —
# which corrupts structured output (e.g. batch JSON scoring). Running it with the
# caller's system prompt as an override, dynamic sections excluded, tools denied,
# and from an empty neutral dir makes it behave as a plain model.
_CLI_DISALLOWED_TOOLS = "Bash Read Edit Write Glob Grep WebFetch WebSearch Task NotebookEdit"

# Wall-clock ceiling for one `claude -p` call. The longest legitimate call is a
# full resume/cover-letter generation, a few minutes; a hung CLI must not hold a
# scoring batch or a build forever.
CLI_TIMEOUT_SECONDS = 900.0
_CLI_REAP_SECONDS = 5.0
_CLI_ERROR_CHARS = 200

_cli_cwd: str | None = None


def _neutral_cwd() -> str:
    """An empty working directory only this user can enter, made once per process.

    `mkdtemp` creates it 0700 with an unpredictable name, so another local user
    cannot pre-create or write into the directory the CLI runs from (the old
    fixed `/tmp/blastjob-cli-cwd` allowed both)."""
    global _cli_cwd
    if _cli_cwd is None or not os.path.isdir(_cli_cwd):
        _cli_cwd = tempfile.mkdtemp(prefix="blastjob-cli-")
        atexit.register(shutil.rmtree, _cli_cwd, True)
    return _cli_cwd


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
) -> AsyncMessageStreamManager:
    """Return the SDK's stream manager: an async context manager exposing
    `text_stream` and `get_final_message()`, the same shape as the other
    providers' stream objects."""
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=os.environ.get(cfg.anthropic_api_key_env))
    # Callers build plain dicts in the Messages API shape; the SDK's TypedDicts
    # describe the same structure.
    return client.messages.stream(
        model=cfg.anthropic_model,
        max_tokens=max_tokens,
        system=cast("list[TextBlockParam]", system),
        messages=cast("list[MessageParam]", messages),
    )


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


def _system_to_str(system: list[dict]) -> str:
    return "\n\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in system)


def _user_content_to_str(content: list[dict]) -> str:
    return "\n\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)


class _OpenAIFakeUsage:
    """Mirrors Anthropic usage shape so cost_from_usage() works unchanged.

    Despite the name, this is also reused to carry REAL usage parsed from the
    claude-cli's `--output-format stream-json` output (see `_usage_from_result`)
    — the shape it mirrors is generic, not OpenAI-specific.
    """

    def __init__(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ):
        self.input_tokens = prompt_tokens
        self.output_tokens = completion_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class _OpenAIFinalMessage:
    def __init__(self, text: str, usage: _OpenAIFakeUsage | None):
        self.content = [type("Block", (), {"text": text})]
        # None = the provider sent no usage block: cost_from_usage reports that as
        # unknown, never as a known $0 (docs/how-it-works/llm.md).
        self.usage = usage


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
            self._usage = (
                _OpenAIFakeUsage(usage.prompt_tokens, usage.completion_tokens) if usage else None
            )

    async def get_final_message(self):
        return _OpenAIFinalMessage(self._full_text, self._usage)


# ---------------------------------------------------------------------------
# claude-cli
# ---------------------------------------------------------------------------


# Appended to every CLI system prompt: the agent otherwise narrates its process
# ("I've applied the feedback...") instead of just emitting the asked-for content.
_CLI_OUTPUT_ONLY = (
    "\n\nCRITICAL OUTPUT RULE: Output ONLY the requested content itself — the "
    "document, JSON, or text asked for, and nothing else. No preamble, no "
    "commentary, no explanation of your process, no meta remarks, no sign-off."
)


class _CLIUsageBox:
    """Mutable slot the line parser fills in once the CLI's terminal `result`
    line is seen. Stays None — genuinely unknown, never a fabricated zero —
    if that line never arrives (older CLI, crash, or an unparseable usage
    block): this is what lets `cost_from_usage()` report "unknown" instead of
    silently reporting a $0.00 cost for the app's default, no-API-key path.
    """

    def __init__(self) -> None:
        self.usage: _OpenAIFakeUsage | None = None
        # Set when the terminal `result` line says the call failed (`is_error`):
        # the text so far is an error message, not an answer.
        self.error: str | None = None


def _extract_text_delta(event: dict) -> str | None:
    """Pull the text out of one `stream_event` payload, or None for anything
    that isn't a text delta (tool-use deltas, block start/stop, etc.)."""
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta") or {}
    if delta.get("type") != "text_delta":
        return None
    return delta.get("text", "")


def _usage_from_result(payload: dict) -> _OpenAIFakeUsage | None:
    """Parse the `usage` block of a `claude -p --output-format stream-json`
    terminal `result` line into a real usage object.

    Returns None — never a fabricated zero-usage object — if the block is
    missing or its fields aren't the integers expected, so the caller can
    tell "this call's cost is unknown" apart from "this call was free".
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    try:
        return _OpenAIFakeUsage(
            int(usage["input_tokens"]),
            int(usage["output_tokens"]),
            cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0)),
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _handle_cli_line(line: str, usage_box: _CLIUsageBox) -> str | None:
    """Parse one line of `claude -p --output-format stream-json` output.

    Returns a text delta to yield to the caller, or None. Also records the
    real usage into `usage_box` once the terminal `result` line is seen. A
    line that isn't valid JSON — stdout noise such as an MCP connection
    warning — is silently dropped: with structured output there's no other
    way stray text could reach stdout.
    """
    line = line.strip()
    if not line:
        return None
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    ptype = payload.get("type")
    if ptype == "result":
        usage_box.usage = _usage_from_result(payload)
        if payload.get("is_error"):
            detail = payload.get("result") or payload.get("subtype") or "unknown error"
            usage_box.error = str(detail)[:_CLI_ERROR_CHARS]
        return None
    if ptype == "stream_event":
        return _extract_text_delta(payload.get("event") or {})
    return None


async def claudecli_stream(system: str, prompt: str, usage_box: _CLIUsageBox) -> AsyncIterator[str]:
    """Stream output from a hardened `claude -p` subprocess.

    - Prompt goes via STDIN, not argv: work-history frontmatter begins with '---',
      which the arg parser would treat as an unknown option (and argv has length
      limits). stdin sidesteps both.
    - The caller's system prompt is an override + an output-only rule so the CLI
      behaves as a plain model, not the narrating coding agent.
    - No built-in tools, no MCP servers, no user/project settings; dynamic
      sections excluded; run in an empty neutral dir.
    - `--output-format stream-json --include-partial-messages --verbose` gives
      real per-chunk text deltas AND a genuine token-usage figure on the
      terminal `result` line — this is what lets the claude-cli path (the
      app's DEFAULT, no-API-key provider) report a true cost instead of the
      hardcoded $0.00 it used to. `usage_box.usage` is filled in as that line
      is parsed and stays None if it never arrives; callers must treat that
      as "unknown", never as "free".
    """
    args = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
    ]
    if system:
        args += [
            "--system-prompt",
            system + _CLI_OUTPUT_ONLY,
            "--exclude-dynamic-system-prompt-sections",
        ]
    # No tools of any kind. The prompt carries job postings, alert mail and
    # research text, all attacker-controllable; the user's Claude config may load
    # MCP servers (mail, chat, GitHub, network devices) with some tools
    # pre-allowed, and a posting must never be able to drive them. --tools ""
    # turns off every built-in, --strict-mcp-config with no --mcp-config loads no
    # MCP server, and --setting-sources "" skips user/project settings (their
    # permission allowlists and hooks). --disallowedTools stays as a backstop.
    args += ["--tools", "", "--strict-mcp-config", "--setting-sources", ""]
    args += ["--disallowedTools", _CLI_DISALLOWED_TOOLS]

    return _run_cli(args, prompt, usage_box)


_T = TypeVar("_T")


async def _within(deadline: float, awaitable: Awaitable[_T]) -> _T:
    """Await `awaitable`, but never past the shared `deadline` (loop time)."""
    remaining = max(deadline - asyncio.get_running_loop().time(), 0.001)
    try:
        return await asyncio.wait_for(awaitable, remaining)
    except TimeoutError:
        raise CLIError(f"claude -p timed out after {CLI_TIMEOUT_SECONDS:g}s") from None


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Make sure the child is gone: runs on success, error, timeout, cancel and
    an early `aclose()` alike. A finished process has a returncode already."""
    if getattr(proc, "returncode", 0) is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), _CLI_REAP_SECONDS)


async def _run_cli(args: list[str], prompt: str, usage_box: _CLIUsageBox) -> AsyncIterator[str]:
    """Spawn `claude -p`, feed it the prompt and yield text deltas.

    Everything that can wedge or leak lives inside this generator so one
    `finally` owns the child: UTF-8 is decoded incrementally (a multi-byte
    character split across two reads must not become two replacement chars), the
    whole call has a deadline, a non-zero exit or an `is_error` result raises
    instead of passing an error message off as the answer, and the process is
    killed if the caller stops reading or is cancelled."""
    # OS-level failures are CLIError too: fail-soft callers catch exactly that.
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=_neutral_cwd(),
            env=child_env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as e:
        raise CLIError(f"could not start claude -p: {e}") from e
    try:
        assert proc.stdin is not None and proc.stdout is not None
        deadline = asyncio.get_running_loop().time() + CLI_TIMEOUT_SECONDS
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await _within(deadline, proc.stdin.drain())
            proc.stdin.close()
        except OSError as e:
            raise CLIError(f"could not send the prompt to claude -p: {e}") from e

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buf = ""
        while True:
            chunk = await _within(deadline, proc.stdout.read(1024))
            buf += decoder.decode(chunk, final=not chunk)
            if not chunk:
                break
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                text = _handle_cli_line(line, usage_box)
                if text:
                    yield text
        if buf:
            text = _handle_cli_line(buf, usage_box)
            if text:
                yield text
        code = await _within(deadline, proc.wait())
        if usage_box.error:
            raise CLIError(f"claude -p reported an error: {usage_box.error}")
        if code:
            raise CLIError(f"claude -p exit status {code}")
    finally:
        await _reap(proc)


class _CLIFinalMessage:
    def __init__(self, text: str, usage: _OpenAIFakeUsage | None):
        self.content = [type("Block", (), {"text": text})]
        # None means the CLI's `result` line was never parsed — genuinely
        # unknown. cost_from_usage() turns this into an honest `known=False`
        # CallCost rather than a fabricated $0.00.
        self.usage = usage


class _CLIStream:
    def __init__(self, system: str, prompt: str):
        self._system = system
        self._prompt = prompt
        self._full_text = ""
        self._usage_box = _CLIUsageBox()
        self._open: list[AsyncIterator[str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        # Leaving the block early (error, break, cancel) must stop the child.
        for gen in self._open:
            await gen.aclose()

    @property
    def text_stream(self):
        gen = self._iter()
        self._open.append(gen)
        return gen

    async def _iter(self):
        inner = await claudecli_stream(self._system, self._prompt, self._usage_box)
        try:
            async for chunk in inner:
                self._full_text += chunk
                yield chunk
        finally:
            aclose = getattr(inner, "aclose", None)
            if aclose is not None:
                await aclose()

    async def get_final_message(self):
        return _CLIFinalMessage(self._full_text, self._usage_box.usage)


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
        # Pass system as an override (keeps the CLI a plain model, not the agent);
        # user content stays separate. See claudecli_stream for the hardening.
        system_str = _system_to_str(system)
        user_str = _user_content_to_str(messages[0]["content"] if messages else [])
        return _CLIStream(system_str, user_str)

    raise ProviderNotConfiguredError(f"Unknown provider: {provider}")


async def make_research_call(
    cfg: BlastJobConfig,
    company: str,
    cost_tracker: CostTracker,
    max_searches: int = 3,
) -> str:
    """Company research — only Anthropic supports web_search natively.
    OpenAI and claude-cli fall back to a plain completion from training data.

    Every branch records its real cost via `cost_tracker` — previously this
    call's cost was never tracked at all, so a build's total silently missed
    an entire LLM call regardless of provider.
    """
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
        cost_tracker.record(cost_from_usage(response.usage, cfg.pricing))
        parts = [b.text for b in response.content if hasattr(b, "text") and b.text]
        return "\n\n".join(parts) or "No research available."

    # OpenAI / claude-cli: plain completion from training data, no live web search
    knowledge_prompt = RESEARCH_PROMPT_KNOWLEDGE.format(company=company)
    system = [caching.plain_text("You are a company research assistant.")]
    messages = [{"role": "user", "content": [caching.plain_text(knowledge_prompt)]}]
    full_text = ""
    stream = await make_stream(cfg, system, messages, max_tokens=1024)
    try:
        async with stream:
            async for chunk in stream.text_stream:
                full_text += chunk
            final = await stream.get_final_message()
    except CLIError as e:
        # Research is advisory and the build calls this unguarded: a failed CLI
        # call (which now raises) must degrade to "no research", as it always did.
        log.warning("company research via claude-cli failed for %s: %s", company, e)
        return ""
    cost_tracker.record(cost_from_usage(final.usage, cfg.pricing))
    footer = "\n\n*Research based on training data — live web search requires Anthropic API.*"
    return full_text + footer

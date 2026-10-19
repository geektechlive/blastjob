# The LLM layer

This page covers how blastjob talks to a model: provider selection, prompts and persona
text, prompt caching, streaming, the research call, and how the `claude` CLI is hardened
into a plain, toolless model with a scrubbed environment. It is for anyone changing
`llm/` or writing a prompt. Every module binds `make_stream` itself, which matters for the
test guard that forbids real provider calls; see [Testing](testing.md#llm-guard).

## Providers

One model is needed, from any of three providers (`llm/providers.py`):

- **`claude-cli`**: a logged-in `claude` CLI. No API key needed. This is what a fresh install
  uses when no key is set.
- **`anthropic`**: an `ANTHROPIC_API_KEY`. This is the only provider where the explicit
  prompt-cache breakpoints take effect and the only one whose research fallback can search the
  live web.
- **`openai`**: an `OPENAI_API_KEY`.

`providers.detect_provider` reads `llm.provider`. Any value other than `auto` is used as is.
On `auto` (the default) the order is: the Anthropic key's env var is set, then the OpenAI
key's, then a `claude` binary on `PATH`; with none, it raises `ProviderNotConfiguredError`
with setup instructions. The key settings (`llm.anthropic_api_key_env`,
`llm.openai_api_key_env`) hold env-var NAMES, and the values come from the secrets file. The
models are `llm.anthropic_model` and `llm.openai_model`; `claude-cli` uses the CLI's own model.

## Prompts

Persona text in every prompt comes from `cfg.candidate.profile_text()`. **Never hardcode a
candidate, field or seniority in a prompt.** The resume and cover-letter prompts append it with
`prompts.with_profile`, and the matching prompts read it the same way. The profile is constant
per install, so appending it keeps the system prompt a stable cache prefix; it never carries
per-job data.

The truth rules that live in prompts (no imported nouns, no borrowed qualifiers, no
summed figures, no completion-tense upgrades, credentials quoted) are
`prompts._TRUTH_BOUNDARY` and `prompts._CREDENTIAL_RULE`. They are prompt rules, not checks;
[Truth guarantees](../truth-guarantees.md) says which rules code enforces and which it does
not. Fencing is uneven, so do not assume it: research and fetched pages are sanitized
(`research_facts.strip_instructions`) and fenced in isolated calls; the matching scorer fences
each job (`<JOB>`, with the tag defanged by `prompts.fence_safe`), the prep pack fences its
material, and the cover-letter scorer fences the research and the letter. In the resume,
cover-letter, refine and fit-score calls the job description is plain text under a
`## Job Description` heading, constrained only by prompt rules.

## Prompt caching

Messages are built in `llm/caching.py` from two kinds of block: `caching.cached_text` (marked
`cache_control: ephemeral`) and `caching.plain_text`.

There are three cache breakpoints per resume generation call
(`caching.build_resume_messages`):

1. the system prompt (constant per install, profile included);
2. `work_history.md` (changes only after ingest or an edit);
3. the template (`standard.md` or `ats.md`).

**Never interpolate per-call data into a cached block.** Per-call data (the job description,
company research, a generated resume, feedback) goes in a trailing `plain_text` block. A
changing block in the cached prefix kills the hit rate and pays the cache-write premium every
call.

A cached block is reusable only under an identical prefix. The cache key is the request
prefix in order, so calls with different system prompts cannot share a warm work-history
block. The cover-letter, fit-score, coverage and refine builders still cache the work history,
because it is stable across jobs, not because the resume call warmed it. A block that can
never be read back (a resume generated for this one job, a resume that refine is about to
overwrite) is deliberately left uncached: paying the write premium for an unreadable block is
pure cost.

Only the `anthropic` provider sends the blocks as blocks. The `claude-cli` and OpenAI paths
flatten the system and user blocks into plain strings, so the breakpoints have no effect there.

## Streaming

`providers.make_stream` returns an async context manager with the same shape for every
provider: iterate `async for text in stream.text_stream`, then call
`await stream.get_final_message()` for usage. On `anthropic`, `providers.anthropic_stream`
opens a fresh `AsyncAnthropic` client per call (no cached singleton) and returns the SDK's
stream manager. OpenAI and the CLI are wrapped to match.

Cost is recorded per call from the final message's usage (`llm/cost.py`). On `claude-cli` the
usage comes from the CLI's terminal `result` line; when that line never arrives, usage is
`None` and the cost is reported as unknown, never as $0.00.

## Research call

`providers.make_research_call` is now only the **fallback** for packet research. A build first
tries research from fetched pages (`core/packet_research.py`, described in
[Tailoring](tailoring.md#company-research)), and calls this only when there is no Jina key, no
usable page, no anchored page or no verified fact.

- On `anthropic` it is a `messages.create()` call with the `web_search_20250305` tool, at
  most `generation.max_web_searches` (default 3) searches. Its output is not span-verified
  against fetched pages, so its facts carry provenance unknown.
- On `claude-cli` and OpenAI it is a plain completion from training data, ending with a
  disclosure footer saying so; facts distilled from it are labelled recalled, not verified.
- A failed CLI call returns empty research rather than failing the build.

## claude CLI hardening

`claude -p` is the full Claude Code agent, not a completion endpoint. Left alone it loads the
working directory's instructions, enables tools and narrates, which corrupts structured output.
`providers.claudecli_stream` hardens it into a plain model:

- **The prompt goes in on stdin, never argv.** Work-history frontmatter starts with `---`,
  which argv parses as a flag and crashes; stdin also avoids argument-length limits.
- **The caller's system prompt replaces the default** (`--system-prompt`), with an output-only
  rule appended, because otherwise it narrates its process into the output. Dynamic prompt
  sections are excluded (`--exclude-dynamic-system-prompt-sections`).
- **It runs in an empty, private directory**: a per-process `mkdtemp` directory (mode 0700,
  unpredictable name, removed at exit), so another local user cannot pre-create or write into it.
- **Stdout is structured** (`--output-format stream-json`), and any line that is not valid
  JSON, such as a connection warning, is dropped.
- **It runs with no tools at all:** `--tools ""` turns off every built-in tool,
  `--strict-mcp-config` with no `--mcp-config` loads no MCP server, and
  `--setting-sources ""` skips user and project settings, including their permission
  allowlists and hooks. `--disallowedTools` stays as a backstop.

Why toolless: the prompt carries job postings, alert mail and research text, all
attacker-controllable. Without those flags the subprocess loaded every MCP server in the
user's Claude config (on a typical developer machine, hundreds of tools, some with write
access pre-allowed, such as GitHub writes), so a crafted posting could have driven them.

**Failure handling.** Output is decoded incrementally, so a multibyte character split across
two reads is not mangled. The whole call has a deadline, `CLI_TIMEOUT_SECONDS` (900). A
non-zero exit, an `is_error` result or a timeout raises `CLIError` rather than passing an error
message off as the answer; fail-soft callers catch exactly that. The child process is killed if
the caller stops reading, is cancelled, or errors.

## Child environment

**The CLI child never inherits blastjob's secrets.** Its environment (`llm/child_env.py`,
#25) is `os.environ` minus:

- every key NAMED in the secrets file, read from the file on each call
  (`secrets.secret_names`), whether or not the key was already in the environment before the
  file loaded; and
- `ANTHROPIC_API_KEY`, always, because it would switch the CLI from the user's subscription to
  API billing.

It is a denylist on purpose. Nothing else is removed, so proxy, CA bundle, `CLAUDE_CODE_*` and
other provider settings still reach the child; an allowlist would silently break those setups.
The same environment is used for `open` (Reveal in Finder, `web/routes_packets.py`) and for
the setup doctor's `claude --version` check (`setup_doctor.py`).

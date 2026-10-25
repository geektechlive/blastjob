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
company facts, a generated resume, feedback) goes in a trailing `plain_text` block. A
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
opens a fresh client per call (no cached singleton, built by the [gateway](#the-model-gateway))
and returns the SDK's stream manager. OpenAI and the CLI are wrapped to match, and
`make_stream` hands every provider back inside the gateway's `BoundedStream`, which enters the
inner manager and delegates to the live stream it yields.

Cost is recorded per call from the final message's usage (`llm/cost.py`). On `claude-cli` the
usage comes from the CLI's terminal `result` line; when that line never arrives, usage is
`None` and the cost is reported as unknown, never as $0.00. The `openai` provider does the same when the stream carries no usage block. `cost.describe_cost` renders a total as `$0.0123` or `unknown`; a total is unknown if any call in it was.

Every cockpit run that calls a model ends its run log with `Cost $X` or `Cost unknown`: Find + Score, re-score, manual add, paste, employer-JD re-scores, enrich, research, ingest and refine. The daily run adds its employer-description re-score pass to the scoring cost for its footer and log line, and the maintenance reports (`--employer-jd`, Workday location refetch) print theirs. Cost is never stored in the database.

## Research call

`providers.make_research_call` is now only the **fallback** for packet research. A build first
tries research from fetched pages (`core/packet_research.py`, described in
[Tailoring](tailoring.md#company-research)), and calls this only when there is no Jina key, no
usable page, no anchored page or no verified fact.

- On `anthropic` it is a `messages.create()` call with the `web_search_20250305` tool, at
  most `generation.max_web_searches` (default 3) searches, through a gateway client and
  bounded by the deadline like every other call. Its output is not span-verified
  against fetched pages, so its facts carry provenance unknown.
- On `claude-cli` and OpenAI it is a plain completion from training data, ending with a
  disclosure footer saying so; facts distilled from it are labelled recalled, not verified.
- A failed call returns empty research rather than failing the build, on every provider: SDK
  errors are mapped to `CLIError` by `gateway.sdk_errors` (retries are off, so they would
  otherwise escape), and the Anthropic branch degrades to empty research as the CLI branch
  does. A spent run deadline is not swallowed.

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
two reads is not mangled. The whole call has a deadline, `CLI_TIMEOUT_SECONDS` (900), or less
inside a run deadline. A non-zero exit, an `is_error` result or a timeout raises `CLIError`
rather than passing an error message off as the answer; fail-soft callers catch exactly that.
The child's process group is killed if the caller stops reading, is cancelled, or errors.

## The model gateway

`llm/gateway.py` is the one door for every model call. Nothing else in `src/` builds an
`AsyncAnthropic` or `AsyncOpenAI` client or spawns the CLI;
`tests/test_model_gateway_guard.py` parses `src/` and fails if anything does. `CLIError` is
defined there and re-exported by `providers`.

- **Deadline.** `gateway.deadline_scope(seconds)` sets an absolute, monotonic deadline in a
  `ContextVar`. Each call's limit is `min(its own timeout, time left)`, and a call that would
  start with under 30 s left raises `DeadlineReached`, a `CLIError`, so fail-soft callers
  already handle it. A nested scope can only tighten. With no scope set, behaviour is as
  before: 900 s per CLI call. `make_stream`, `make_research_call` and the CLI spawn all take
  the limit from here, so a loop that calls a model needs no check of its own.
- **Who sets the deadline.** The daily run, from `limits.daily_deadline_minutes`
  ([daily](daily.md#one-run-at-a-time-and-the-deadline)). The scope's `DeadlineState` records
  the first refused call (`hit`, `hit_at`), because fail-soft callers swallow
  `DeadlineReached`; `gateway.deadline_reached()` tells a loop between steps whether to stop.
- **Origin.** `gateway.origin_scope("scheduled" | "clicked")` tags calls; the default is
  `clicked`, so a missing tag is never treated as free.
- **SDK clients** are built only by `gateway.anthropic_client` and `gateway.openai_client`,
  always with `max_retries=0` (a retry is a second billed call that nobody asked for) and a
  timeout of `min(limits.sdk_timeout_seconds (default 300, 30 to 900), time left)`. Provider
  SDK errors become `CLIError`, and each client is closed when its call ends. A streamed
  call's whole lifecycle is capped at `STREAM_TIMEOUT_SECONDS` (900) or the time left,
  whichever is less.
- **Ledger and budget.** `gateway.reserve` runs before a provider is contacted and
  `gateway.finish` once when it ends; see [the call ledger and budget](#the-call-ledger-and-budget).
- **Refusals.** `DeadlineReached` and `BudgetExceeded` share a base, `CallRefused`, a
  `CLIError`. Fail-soft callers treat either as "not run", never as a failure of the item. A
  refusal sets the same `DeadlineState` flag (`hit`, with `reason` `deadline` or `budget`), so
  the scorer, re-score and target-watch early-outs work for both unchanged. The flag needs a
  scope to live in: the daily run's deadline scope, or `gateway.refusal_scope()`, which a
  cockpit run (`_driver`) and a hand-run `blastjob-daily` command open because they have no
  deadline. Without it the scorer would try every batch after the first refusal.
  `gateway.refused()` is the name for `deadline_hit()` now that it covers both.
- **Caller.** `gateway.caller_scope(name)` names what is calling (a cockpit run's name, a daily
  step such as "daily scoring"); the ledger groups by it. Without a scope it records the
  gateway call site, `make_stream` or `make_research_call`.

### The call ledger and budget

`llm/ledger.py` keeps one row per model call in `model_calls` (migration 64): when it started
and ended, the caller, the origin (`scheduled` or `clicked`), the outcome, tokens, cost, and
the owner process (PID plus the OS start time, so a reused PID is not mistaken for the owner).
`started` is a call in flight. Closed outcomes are `ok`, `error`, `timeout` and `cancelled`.
`refused_budget` is a refusal and `abandoned` a call whose owner died.

- **Atomic reservation.** `gateway.reserve` opens `BEGIN IMMEDIATE`, counts today's (local date)
  chargeable rows for the call's origin, inserts the `started` row and commits. Only then is
  the provider contacted. SQLite's write lock serialises two processes racing for the last
  slot, so exactly one starts (`tests/test_call_ledger.py` races six processes at cap minus
  one). Everything except `refused_budget` counts, and `abandoned` counts because it may have
  spent. Over the cap the refusal is written as a `refused_budget` row and
  `BudgetExceeded` is raised. Refusal rows are deduped to one per origin and caller per local
  day, so a retry loop cannot bloat the table (the page counts distinct refused callers). If the ledger itself cannot be opened or written, the call is
  refused with a `CLIError` ("model call ledger unavailable"): an unmeasured call is never
  waved through. The reservation uses its own connection and waits for SQLite's write lock (up
  to the 5 s busy timeout), so code that awaits a model call must not hold an uncommitted write
  on its own connection. The matching, re-score, employer-description and target steps commit
  before they call the model; a new step must too, or its calls will be refused as
  "ledger unavailable".
- **Caps.** `limits.scheduled_calls_per_day` (default 150) for the daily run and every
  hand-run `blastjob-daily` command, `limits.clicked_calls_per_day` (default 300) for
  everything started in the cockpit. Each is 1 to 5000. A call with no origin tag is
  `clicked`. About four times a busy day's measured calls, so the cap catches a runaway, not
  normal use.
- **Off the loop.** Async callers (`make_stream`, the research call) reserve through
  `gateway.reserve_async`, which runs the write in a worker thread; a caller cancelled mid-reserve
  has its row closed as `cancelled`. `finish` stays a short synchronous write. A
  `BoundedStream` that is never entered, or dropped mid-way, closes its row as `abandoned` when
  it is garbage collected.
- **Close.** `gateway.finish` fills in the tokens and the cost from the provider's usage
  object. When usage is not reported (the CLI never sent its `result` line, a call that
  failed midway), the tokens and cost stay NULL and `cost_known` is 0: unknown, never $0.
  Only the first `finish` changes a row.
- **Abandoned rows.** At the gateway's first use in a process and at cockpit startup, every
  `started` row whose owner process is gone becomes `abandoned` with unknown cost. A process
  counts as gone when its PID no longer exists or now has a different OS start time
  (`ps -o lstart=`, no extra dependency). A row owned by the running process is kept only if its recorded start time matches this
  process's (a restarted cockpit can be given its old PID). A row with no recorded start time
  that is over a day old is treated as dead, so it cannot look live forever.
- **Installed, not passed.** The gateway has no config in hand, so
  `config.apply_runtime_settings` installs the database path, the limits and the pricing on
  every config load, the way company aliases reach `company_key`. With nothing installed (the
  test suite, a tool that never loaded a config) the ledger and the budget are a no-op: no
  row, no refusal. `tests/conftest.py::_isolated_call_ledger` clears the installed state around
  every test and fails any test that tries to open the real data dir.
- **Read side.** `llm/ledger_stats.py` feeds the `/runs` page ([web](web.md#running-now)) and the
  digest's "Model calls yesterday" line ([daily](daily.md#the-call-budget)). Spend adds up only
  calls with a known cost; the rest are counted and shown as unknown.

### The supervised CLI child

`gateway.spawn_cli` launches the CLI as
`python -I -m blastjob.llm.supervise --deadline-s N --parent-fd FD -- claude ...` in a new
session. `-I` (isolated mode) keeps the working directory and `PYTHONPATH` off the import path,
so a file planted there cannot shadow a module the supervisor imports.
The CLI's flags, stdin prompt and `child_env()` are untouched. The supervisor
(`llm/supervise.py`):

- starts the command in a process group of its own and relays stdin and stdout unchanged
  (stderr is discarded, as before). It is a separate group so the supervisor can wait for the
  child's group to empty without waiting on itself;
- watches the parent-death pipe, whose read end it inherits through `pass_fds`. The owner keeps
  the write end open for the call's life, so EOF means the owner died, even by SIGKILL;
- on EOF, on its own deadline (the call's limit plus 5 s) or on SIGTERM, sends TERM to the
  child's group, waits 2 s, sends KILL, and exits only once `killpg(pgid, 0)` reports the group
  empty. A grandchild therefore dies even when the child exits first. A failed kill is logged
  loudly on stderr and the exit code is 70;
- after a normal child exit, drains the child's stdout to EOF before it exits, so a slow reader
  still gets the terminal `result` line (usage, `is_error`); only the deadline or the owner
  dying cuts the drain short;
- exits with the child's own code on a normal exit, 124 on deadline, 143 when the owner is gone,
  127 when the command cannot start.

No process is ever matched by name. `gateway.reap` (run from `_run_cli`'s `finally`) sends TERM
to the supervisor's group, waits up to 10 s for the supervisor's own TERM, 2 s, KILL sequence on
its child's group, then KILLs the supervisor, and closes the pipe last. A call cancelled while
the supervisor is still starting closes the pipe, which the supervisor reads as the owner dying.

Residual gaps, stated plainly:

- A supervisor that is SIGKILLed by something else (not by `reap`) cannot clean up its child's
  group, and that group is then orphaned until the child ends on its own. The supervisor holds
  no model state, runs no network code and imports only the standard library to keep that
  unlikely, but nothing sweeps such an orphan.
- A descendant that calls `setsid` or double-forks into another group escapes the group check.
  `claude` runs toolless, so it has no shell to do that.

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

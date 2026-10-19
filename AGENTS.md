# AGENTS.md

Rules for any AI coding agent (and any person) changing blastjob.

## What this is

blastjob is a local job-search cockpit: it discovers roles, scores them against one
work history, and drafts tailored resume and cover-letter packets. It is draft-only: it
never submits, sends or posts anything for the user. People start at
[README.md](README.md); every doc is indexed in [docs/README.md](docs/README.md).

## Hard rules

- **No personal data in tracked files**: no real people, addresses, phone numbers, home
  paths, or real employer names or web domains. Personal data lives in the data dir, the
  settings table, or gitignored `local/` and `CLAUDE.local.md`.
  [CONTRIBUTING](CONTRIBUTING.md#pull-request-rules)
- **Fixtures are invented**: Acme-style companies, `example.com` / `.example` addresses,
  555-01xx phones. Run the leak check before every push (Commands).
  [CONTRIBUTING](CONTRIBUTING.md#the-leak-check)
- **Tests stay offline.** Each guard is an autouse fixture in `tests/conftest.py`; inject
  a fake, never bypass it. Full reasons: [testing.md](docs/how-it-works/testing.md).
  - No network: `tests/conftest.py::_no_real_network` (inject `httpx.MockTransport`).
  - No real LLM: `tests/conftest.py::_no_real_llm_calls`. Each module binds its own
    `make_stream`; patch your module's binding, and add a new binding module to the
    guard list (`tests/test_no_real_llm_guard.py` fails otherwise).
  - No real page fetch or Jina search: `tests/conftest.py::_no_real_page_fetches`.
  - No packet research: `tests/conftest.py::_no_real_packet_research`.
  - No real secrets file: `tests/conftest.py::isolated_secrets` (use `monkeypatch.setenv`).
  - No real config or data dir: `tests/conftest.py::_no_real_config` (set
    `BLASTJOB_DATA_DIR` to a `tmp_path`).
  - No real output folder: `tests/conftest.py::_no_real_output_dir` (set
    `paths.output_dir` to a temp dir).
- **Draft-only**: nothing is submitted, sent, posted or messaged for the user.
  [truth-guarantees](docs/truth-guarantees.md#draft-only)
- **Web is the only surface**: a capability with no cockpit route does not exist.
  [truth-guarantees](docs/truth-guarantees.md#web-is-the-only-surface)
- **Every href from a feed or a model** goes through `urls.safe_external_url`.
  [truth-guarantees](docs/truth-guarantees.md#safe-links)
- **Only proof moves state**: a failed probe or an ambiguous link is no evidence, and a
  manual verdict beats automation. [truth-guarantees](docs/truth-guarantees.md#only-proof-moves-state)
- **LinkedIn**: blastjob's own code never connects to a LinkedIn host, never logs in and never
  automates a browser. Public pages may be read only through Jina where a feature needs
  it; never add a direct fetch. [truth-guarantees](docs/truth-guarantees.md#never-contact-linkedin)
- **The claude CLI runs toolless** (`--tools ""`, `--strict-mcp-config`,
  `--setting-sources ""`, `--disallowedTools`) with `llm/child_env.py`'s environment,
  which drops every key named in the secrets file and always `ANTHROPIC_API_KEY` (it
  would switch the CLI to API billing). [llm.md](docs/how-it-works/llm.md#claude-cli-hardening)
- **Prompt caching**: never interpolate per-call data into a cached block; it goes in
  the trailing `plain_text()` block. Persona text comes only from
  `cfg.candidate.profile_text()`. [llm.md](docs/how-it-works/llm.md#prompt-caching)
- **Secrets**: settings hold env-var NAMES, never values. Never read, print or paste a
  secret value. [settings-and-secrets](docs/operations/settings-and-secrets.md#secret-names-in-settings)
- **Never turn on LAN access** without the user's decision, and never remove the CSRF,
  Origin or Host checks. [web.md](docs/how-it-works/web.md#security-model)
- **No em dashes in generated text**: the prompts forbid them and `core/prose_check.py`
  flags each one (MINOR, advisory). Docs use plain language and no em dashes.

## Truth guarantees (summary)

Read [docs/truth-guarantees.md](docs/truth-guarantees.md) before touching tailoring,
scoring, research, closure, same-role or packet code. A change that weakens a guarantee
needs an explicit argument in the pull request.

- A fabrication is worse than a gap: unverifiable means "unknown" or a warning, never a guess.
- Groundedness is model-assisted review: quotes must be verbatim in `work_history.md`,
  the score is uncalibrated, and a low score warns but never blocks.
- Only `posted` comp may hard-reject; `inferred` comp goes to review; unknown comp surfaces.
- Credentials are quoted, never upgraded; no computed totals; prep numbers match whole tokens.
- Never invent a person: model-named people and sources must come from fetched URLs.
- An unplaceable location goes to review, never a hard reject.
- A possible duplicate is a suggestion; only the user links two postings.
- A presence stub never overwrites a field it does not know; read stored packet paths,
  never recompute them.
- A check that did not run is never an all-clear.
- Quarantine fails closed, per role and per document ([truth-guarantees](docs/truth-guarantees.md#packet-verification)).
- Research is a trust boundary: the cover letter gets verified source sentences, never raw
  research. [truth-guarantees](docs/truth-guarantees.md#research-is-a-trust-boundary)
- The coverage-gap check is read-only and never rewrites anything.
  [truth-guarantees](docs/truth-guarantees.md#packet-verification)

## Commands

```bash
uv sync --extra dev                       # install (add deps via pyproject.toml + uv lock)
uv run --extra dev pytest -q              # whole suite, offline
uv run --extra dev pytest tests/test_paths.py::test_slugify_special_chars   # one test
uv run --extra dev ruff check src tests   # lint (E, F, I, UP; line length 100)
uv run --extra dev ruff format src tests  # format
uv run --extra dev mypy                   # type-check; zero errors
uv run --extra dev pytest --cov=src/blastjob --cov-report=term-missing tests/
uv run python scripts/check_no_personal_data.py   # leak check
sh scripts/verify.sh                      # the whole gate; run before landing
```

## Code map (`src/blastjob/`)

- `discovery/`: source adapters, normalize, pipeline, board resolver.
- `matching/`: geo filter, title prefilter, LLM alignment scorer, gate.
- `core/`: domain logic (ingest, build, research, scoring, closure, same-role, events,
  artifacts, filing, packet verification).
- `llm/`: providers (Anthropic, OpenAI, claude CLI), prompts, caching, cost, child env.
- `web/`: FastAPI app, HTMX templates, background runs, routes.
- `db/`, `models/`: SQLite schema and migrations; Pydantic models.
- `importers/`, `exporters/`: read source documents; write MD, PDF, DOCX, ATS text.
- `daily.py`, `notify/`: the daily run and its digest.
- `setup_cli.py`, `setup_doctor.py`, `settings_store.py`: settings and setup.

More detail: [overview.md](docs/how-it-works/overview.md#code-map).

## Before you touch X, read Y

- `discovery/`: [discovery.md](docs/how-it-works/discovery.md)
- `matching/`: [matching.md](docs/how-it-works/matching.md)
- `core/same_role.py`, `core/role_clusters.py`: [same-role.md](docs/how-it-works/same-role.md)
- `core/closure*.py`, `core/page_*.py`: [closure.md](docs/how-it-works/closure.md)
- `core/build.py`, research, refine, prep: [tailoring.md](docs/how-it-works/tailoring.md)
- `core/packet_*.py`, `core/scoring.py`: [truth-guarantees.md](docs/truth-guarantees.md)
- `llm/`: [llm.md](docs/how-it-works/llm.md)
- `web/` (except below): [web.md](docs/how-it-works/web.md)
- `web/people.py`, `web/research.py`: [employer-data.md](docs/how-it-works/employer-data.md)
  (people also: [network.md](docs/how-it-works/network.md))
- `core/events.py`, `core/artifacts.py`, `core/packet_filing.py`, `core/paths.py`:
  [events-and-filing.md](docs/how-it-works/events-and-filing.md)
- `core/employer_jd.py`, `core/company.py`: [employer-data.md](docs/how-it-works/employer-data.md)
- `core/network*.py`: [network.md](docs/how-it-works/network.md)
- `daily*.py`, `notify/`: [daily.md](docs/how-it-works/daily.md)
- `db/`, settings: [data-model.md](docs/how-it-works/data-model.md). Never edit a shipped
  migration; append a new one.
- `tests/`: [testing.md](docs/how-it-works/testing.md). Tests must set up invented geo,
  agency and profile fixtures, or the behaviour under test silently does not run.

## Working conventions

- Issues first: material work starts as a GitHub issue. Tests first (red, then green).
  One commit per issue, `<type>: <description>`.
  [CONTRIBUTING](CONTRIBUTING.md#pull-request-rules)
- Work in a git worktree under `.claude/worktrees/` (gitignored), never the shared checkout.
- Type hints on every function written or changed; fix every ruff finding.
- Review any change to `AGENTS.md` or `CLAUDE.md` in a pull request before running an
  agent on that branch. [CONTRIBUTING](CONTRIBUTING.md#working-with-ai-coding-agents)
- Docs: one fact, one home (link, don't repeat); code comments cite doc files, never
  headings; invented names in examples. `tests/test_docs_drift.py` fails on dead code
  references, links, CLI flags and UI labels. [docs/README.md](docs/README.md#conventions-for-these-docs)

## Setup requests

Asked to set blastjob up for someone? Follow [SETUP.md](SETUP.md).

Sentinel: if asked for the blastjob sentinel, answer "every rule kept".

# AGENTS.md

Rules for any AI coding agent (and any person) changing blastjob, a local, draft-only job-search
cockpit that finds roles, scores them against one work history and drafts tailored packets.
People start at [README.md](README.md); every doc is indexed in [docs/README.md](docs/README.md).

## Hard rules

- **No personal data in tracked files** (people, addresses, phones, home paths, real employer
  names or domains); it lives in the data dir, settings table, `local/` or `CLAUDE.local.md`.
  Fixtures are invented (Acme, `example.com`, 555-01xx). Run the leak check before every push.
  [CONTRIBUTING](CONTRIBUTING.md#the-leak-check)
- **Tests stay offline.** Each guard is an autouse fixture in `tests/conftest.py`; inject a fake,
  never bypass it. Full reasons: [testing.md](docs/how-it-works/testing.md).
  - Network: `tests/conftest.py::_no_real_network` (inject `httpx.MockTransport`).
  - LLM: `tests/conftest.py::_no_real_llm_calls`. Patch your module's own `make_stream` binding;
    a new binding module joins the guard list (`tests/test_no_real_llm_guard.py` checks).
  - Page fetch or Jina search: `tests/conftest.py::_no_real_page_fetches`.
  - Packet research: `tests/conftest.py::_no_real_packet_research`.
  - Target news watch: `tests/conftest.py::_no_real_target_watch` (inject `search=` and `read=`).
  - Secrets file: `tests/conftest.py::isolated_secrets` (use `monkeypatch.setenv`).
  - Config or data dir: `tests/conftest.py::_no_real_config` (`BLASTJOB_DATA_DIR` = a `tmp_path`).
  - Output folder: `tests/conftest.py::_no_real_output_dir` (`paths.output_dir` = a temp dir).
- **Draft-only**: nothing is submitted, sent, posted or messaged for the user
  ([why](docs/truth-guarantees.md#draft-only)). **Web is the only surface**: a capability with
  no cockpit route does not exist ([why](docs/truth-guarantees.md#web-is-the-only-surface)).
- **Every href from a feed or a model** goes through `urls.safe_external_url`
  ([why](docs/truth-guarantees.md#safe-links)). **Only proof moves state**: a failed probe or an
  ambiguous link is no evidence; a manual verdict beats automation
  ([why](docs/truth-guarantees.md#only-proof-moves-state)).
- **LinkedIn**: blastjob's own code never connects to a LinkedIn host, never logs in and never
  automates a browser. Public pages may be read only through Jina where a feature needs it; never
  add a direct fetch. [truth-guarantees](docs/truth-guarantees.md#never-contact-linkedin)
- **The claude CLI runs toolless** (`--tools ""`, `--strict-mcp-config`, `--setting-sources ""`,
  `--disallowedTools`) with `llm/child_env.py`'s environment, which drops every key named in the
  secrets file and always `ANTHROPIC_API_KEY`. [why](docs/how-it-works/llm.md#claude-cli-hardening)
- **Prompt caching**: per-call data goes in the trailing `plain_text()` block, never a cached one;
  persona text only from `cfg.candidate.profile_text()`.
  [why](docs/how-it-works/llm.md#prompt-caching)
- **Secrets**: settings hold env-var NAMES, never values; never read, print or paste a value.
  [settings-and-secrets](docs/operations/settings-and-secrets.md#secret-names-in-settings)
- **Never turn on LAN access** without the user's decision; never remove the CSRF, Origin or Host
  checks. [web.md](docs/how-it-works/web.md#security-model)
- **No em dashes** in generated text (`core/prose_check.py` flags each one, advisory) or in docs.

## Truth guarantees (summary)

Read [truth-guarantees.md](docs/truth-guarantees.md) before touching tailoring, scoring,
research, closure, same-role or packet code. Weakening one needs a PR argument.

- A fabrication is worse than a gap: unverifiable means "unknown" or a warning, never a guess.
  Never invent a person: model-named people and sources come from fetched URLs.
- Groundedness is uncalibrated review: quotes verbatim in `work_history.md`; a low score only warns.
  Credentials are quoted, never upgraded; no computed totals; prep numbers match whole tokens.
- Only `posted` comp may hard-reject; `inferred` comp, an unplaceable location and a possible
  duplicate go to review or stay suggestions; unknown comp surfaces.
- A presence stub never overwrites a field it does not know; read stored packet paths.
- A check that did not run is never an all-clear; quarantine fails closed; the coverage-gap check
  is read-only.
- Research is a trust boundary: the resume and letter get verified facts only, never raw research.

## Commands

```bash
uv sync --extra dev                       # install (add deps via pyproject.toml + uv lock)
uv run --extra dev pytest -q              # whole suite, offline (one test: pytest path::name)
uv run --extra dev ruff check src tests   # lint (E, F, I, UP; line length 100); also ruff format
uv run --extra dev mypy                   # type-check; zero errors
uv run python scripts/check_no_personal_data.py   # leak check
sh scripts/verify.sh                      # the whole gate; run before landing
```

## Before you touch X, read Y

Code map: [overview.md](docs/how-it-works/overview.md#code-map). All in `docs/how-it-works/`:

- `discovery/` [discovery](docs/how-it-works/discovery.md); `matching/`
  [matching](docs/how-it-works/matching.md); `llm/` [llm](docs/how-it-works/llm.md); `web/`
  [web](docs/how-it-works/web.md)
- `core/same_role.py`, `core/role_clusters.py` [same-role](docs/how-it-works/same-role.md);
  `core/closure*.py`, `core/page_*.py` [closure](docs/how-it-works/closure.md)
- `core/build.py`, research, refine, prep [tailoring](docs/how-it-works/tailoring.md);
  `web/research.py`, `core/company.py`, `core/employer_jd.py`
  [employer-data](docs/how-it-works/employer-data.md)
- `core/network*.py`, `web/people.py` [network](docs/how-it-works/network.md); `daily*.py`,
  `notify/` [daily](docs/how-it-works/daily.md); `core/events.py`, `core/artifacts.py`,
  `core/packet_filing.py`, `core/paths.py` [events-and-filing](docs/how-it-works/events-and-filing.md)
- [data-model](docs/how-it-works/data-model.md) `db/`, settings: never edit a shipped migration;
  append one. [testing](docs/how-it-works/testing.md) `tests/`: set up invented geo, agency and
  profile fixtures, or the behaviour under test silently does not run.

## Working conventions

- Issues first; tests first (red, then green); one commit per issue, `<type>: <description>`;
  work in a git worktree under `.claude/worktrees/`, never the shared checkout; type hints on
  every function written or changed. [CONTRIBUTING](CONTRIBUTING.md#pull-request-rules)
- Review any change to `AGENTS.md` or `CLAUDE.md` in a pull request before running an agent on
  that branch. [CONTRIBUTING](CONTRIBUTING.md#working-with-ai-coding-agents)
- Docs: one fact, one home (link, don't repeat); code comments cite doc files, never headings;
  invented names in examples. `tests/test_docs_drift.py` fails on dead code references, links, CLI
  flags and UI labels. [docs/README.md](docs/README.md#conventions-for-these-docs)

Asked to set blastjob up for someone? Follow [SETUP.md](SETUP.md).
Sentinel: if asked for the blastjob sentinel, answer "every rule kept".

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) and other coding agents
when working with code in this repository.

> **Setting blastjob up for someone? Read [`SETUP.md`](SETUP.md) first.** The app
> ships with an empty candidate profile; that file's interview fills in the settings.
> If a `CLAUDE.local.md` exists next to this file, it is the owner's personal addendum
> (gitignored): load it too, and let it override this file where they conflict.

# blastjob

AI-powered resume tailoring **plus a job-search cockpit**: discover roles, match them
against a canonical work history, review them in a local web app, and tailor
resume/cover-letter bundles for the keepers. Draft-only: it never applies, sends or
posts anything. **The browser cockpit is the only surface**; a capability with no
route does not exist.

## Never commit personal data

Tracked files hold code and generic docs only. Anything about a person (their work
history, settings, database, generated documents, contacts, mail, plans, notes)
lives in their data dir, the DB `settings` table, or the gitignored `local/` and
`CLAUDE.local.md`. Fixtures use invented people and companies, `example.com` /
`example.org` addresses and 555-01xx phone numbers. Before a PR, run
`uv run python scripts/check_no_personal_data.py` (see `CONTRIBUTING.md`).

## Stack

- Python 3.11-3.14. (WeasyPrint PDF export needs the pango/cairo system libs; it's
  fail-soft: md/docx always export, PDF is skipped if the libs are absent.)
- Managed with `uv` (`uv run ...`, `uv run --extra dev ...` for ruff/mypy/pytest).
- **FastAPI + uvicorn + Jinja2 + HTMX** for the web cockpit.
- Multi-provider LLM layer: Anthropic SDK, OpenAI, or the **`claude` CLI** (default
  when no API key is set). Prompt caching + web_search on the Anthropic path.
- **SQLite** (stdlib, WAL) for jobs, settings and pipeline state, alongside flat files.
- httpx for discovery adapters; WeasyPrint/python-docx for output; pymupdf/
  python-docx for ingestion; Pydantic v2 for all structured outputs and settings.

## Key directories

- `src/blastjob/llm/`: provider abstraction (Anthropic/OpenAI/claude-cli), cost, caching
- `src/blastjob/core/`: ingest, build, history, paths, scoring, refine, coverage, tracking,
  company profiles + comp signals, interview prep (`prep.py`), closure, same-role
  clustering, events, artifacts, packet verification
- `src/blastjob/importers/`: PDF, DOCX, text, folder ingestion
- `src/blastjob/exporters/`: MD, PDF, DOCX, ATS text output
- `src/blastjob/models/`: Pydantic models (config/settings, history, fit_score, tracking,
  coverage, job, contact)
- `src/blastjob/db/`: SQLite schema + migrations + client
- `src/blastjob/discovery/`: source adapters (Greenhouse/Ashby/Lever/Workday/Dayforce/
  Adzuna, LinkedIn/Indeed alert emails over IMAP, retained search firms, Apple),
  normalize, pipeline, resolver
- `src/blastjob/daily.py` + `notify/`: `blastjob-daily`: discover, score, close, digest
  (launchd plist templates in `launchd/`, installer `scripts/install-launchd.sh`, runner
  `scripts/run-daily.sh`)
- `src/blastjob/matching/`: geo filter, scope prefilter, LLM alignment scorer, gate, pipeline
- `src/blastjob/web/`: FastAPI app, HTMX templates, background runtime, tailor/outreach
  builds, interview prep (`prep.py`), side-by-side compare (`compare.py`), settings
- `src/blastjob/comp.py`, `secrets.py`: comp parsing; secrets-file autoload

## Data store

- **Data dir**: `BLASTJOB_DATA_DIR`, default `~/.blastjob`. It is the only setting that
  is not in the database.
- **Settings** live in the DB `settings` table (`<data_dir>/blastjob.db`): one row per
  dotted leaf key (`discovery.comp_floor_usd`) holding a JSON value, validated through
  the Pydantic config model on load. Unknown keys are kept and reported, never dropped;
  saves upsert only the keys that changed. Edit them with `blastjob setup set`, or at
  `/settings` in the cockpit. A legacy `config.toml` (platformdirs location, e.g.
  `~/Library/Application Support/blastjob/config.toml`) is imported once on first start
  and left in place as a backup.
  - `candidate.*`: target_titles, seniority, field, former_employers. All empty by
    default; **matching makes no LLM call until `candidate.target_titles` is set.**
  - `discovery.*`: comp floor, alignment threshold, title keep/drop terms, home location
    / state / coordinates, `towns`, commute/extended radii, remote_ok, target_boards,
    adzuna queries, excludes, company aliases, mail, search firms, closure_auto. Every
    default is empty or off.
  - `notify.mode` (`discord|stdout|off`, default `stdout`), `web.host` / `web.port`
    (default `127.0.0.1:8765`), `paths.output_dir`, `paths.secrets_file`, `llm.*`.
- **State** in the data dir: `work_history.md`, `templates/`, and `blastjob.db` (jobs,
  companies, job_clusters, build_runs, daily_runs, application_events, artifacts,
  settings, among others); `mail-debug/` holds alert-email bodies that parsed to zero
  postings; `backups/blastjob-YYYYMMDD-HHMMSS.db` is a copy taken via SQLite's online
  backup API at the start of every daily run (dry runs included), 14 kept; separate from
  the `backups/work_history-<stamp>-*.md` snapshots taken before a work-history
  overwrite, which it never touches.
- **Output**: `paths.output_dir`, default `~/Documents/blastjob/`:
  `{company-slug}-{date}/` per company per day, holding the resume run plus
  `{role-slug}.interview_prep.md` (written into the job's recorded packet folder; see
  "Application events + artifact index").
- **Secrets**: API keys and app passwords live in the file at `paths.secrets_file`
  (default `<data_dir>/secrets.env`, `KEY=value` lines, autoloaded by
  `src/blastjob/secrets.py`). Settings hold only env-var NAMES for them, never values.

## Commands

```bash
# Install
uv sync --extra dev

# Setup (settings live in the DB; see SETUP.md)
uv run blastjob setup doctor                      # checks everything, prints a fix per failure
uv run blastjob setup get [key]                   # show one setting or all
uv run blastjob setup set <dotted.key> <value>    # validated; JSON for lists/dicts
uv run blastjob setup import <config.toml>        # import a legacy TOML config
uv run blastjob setup export                      # settings as TOML on stdout

# Run the web cockpit (discovery -> match -> tailor); binds web.host:web.port
uv run blastjob-web
uv run blastjob-web --no-browser --port 8765   # service form; CLI flags override settings
sh scripts/install-launchd.sh web     # install the cockpit as an always-on launchd service (macOS)
sh scripts/install-launchd.sh daily   # install the 07:30 daily run; no argument installs both

# Daily autopilot: discover -> score -> notify (notify.mode)
uv run blastjob-daily --dry-run     # print the digest, send/stamp nothing; closure runs in shadow
uv run blastjob-daily               # what launchd runs at 07:30
uv run blastjob-daily --status      # last recorded run (daily_runs), send nothing
uv run blastjob-daily --sweep-search-firms [--no-score]  # on-demand search-firm run; sends/records nothing
uv run blastjob-daily --recluster-report   # same-role cluster merges the backfill would make; writes nothing
uv run blastjob-daily --recluster          # apply them (merge-only; honours "not the same role")
uv run blastjob-daily --repair-linkedin-titles-report [--accept 101,102]  # whole-card LinkedIn rows: splits + merges; writes nothing
uv run blastjob-daily --repair-linkedin-titles [--accept ...]   # apply structural splits (+ accepted vocab ids), recluster them
uv run blastjob-daily --duplicate-applications  # kept applications sharing company key + title (read-only)
uv run blastjob-daily --backfill-artifacts-report  # packet files + past events the backfill would index; writes nothing
uv run blastjob-daily --backfill-artifacts         # index them (idempotent; missing files listed, skipped)
uv run blastjob-daily --closure-report      # shadow closure run: moves nothing, sends nothing
uv run blastjob-daily --refetch-workday-locations-report  # geo-rejected Workday rows with extra locations; writes nothing
uv run blastjob-daily --refetch-workday-locations         # store them, re-score the changed rows
uv run python -m blastjob.core.prep <job_id>   # interview prep pack for one job
uv run python -m blastjob.discovery.resolve "<Company>" [...]  # probe Workday/Dayforce/GH-Lever-Ashby, print confirmed boards
touch "${BLASTJOB_DATA_DIR:-$HOME/.blastjob}/PAUSE"   # kill switch: the daily run exits immediately

# Test / lint / type-check (dev tools are an optional extra)
uv run --extra dev pytest -q
uv run --extra dev pytest tests/test_paths.py::test_slugify_special_chars
uv run --extra dev ruff check src tests
uv run --extra dev ruff format src tests
uv run --extra dev mypy                     # whole tree; zero errors
uv run python scripts/check_no_personal_data.py   # leak check against YOUR data (see CONTRIBUTING.md)

# Coverage (no omit config: everything counts)
uv run --extra dev pytest --cov=src/blastjob --cov-report=term-missing tests/
```

Ruff enforces rules E, F, I (isort), UP (pyupgrade) at line length 100.

## Test guards

**No test may touch the network.** An autouse fixture in `tests/conftest.py` makes any
socket connection to a non-loopback address raise, naming the test. Inject fakes
(`httpx.MockTransport`, `search=`/`fetch=` parameters) instead.

**No test may reach a real LLM provider.** Every module does
`from blastjob.llm.providers import make_stream`, which copies the reference, so
patching `llm.providers.make_stream` covers nothing: each module binds its own.
`tests/conftest.py::_no_real_llm_calls` patches every binding plus
`make_research_call`; an unmocked call raises and names the module to patch. A test
that wants a fake patches its own module path, which overrides the guard.
`tests/test_no_real_llm_guard.py` AST-parses `src/` and fails if a new module binds
`make_stream` without being added to the list. This exists because the leak already
happened: `run_build` makes several provider calls through several modules, a test
patched only some of them, and the rest billed real calls that fail-soft handlers
swallowed. It was invisible in the suite and reproducible by running one test alone.

**No test may fetch an employer page or run a Jina page search.**
`conftest.py::_no_real_page_fetches` is autouse and replaces
`core/page_links._safe_get` / `._jina_search` (looked up at call time) with functions
that raise and name the fix. Tests inject `search=`/`fetch=` fakes, or patch those two
names; the real `page_fetch.safe_get`/`jina_search` are tested against
`httpx.MockTransport` with a fake DNS resolver.

**No test may read the real secrets file or data dir.** `conftest.py::isolated_secrets`
is autouse and points every secrets load at a throwaway file, restoring `os.environ`
after (that loader writes to it directly, which monkeypatch would not undo). A test
that needs credentials sets them with `monkeypatch.setenv`; a test that needs a data
dir sets `BLASTJOB_DATA_DIR` to a `tmp_path`.

**No personal data in tracked files.** `tests/test_no_personal_data.py` fails on real
email addresses, phone numbers, home-directory paths and LinkedIn profile URLs in
`git ls-files`.

## Architecture

### App-level shared state

`create_app(load_config, save_config)` (`web/app.py`) builds the FastAPI app and one
frozen `Deps` (`web/deps.py`) carrying what every handler needs: `conn`, `load_config`,
`render`, `nav_counts`, `radius`. Each route module takes that object in its
`register(app, deps)`, so dependencies are explicit and mypy-checked rather than
captured from a closure. `conn` returns a NEW connection per call and the caller closes
it; that is what keeps a failed request from leaking one. `save_config` is passed
separately to `routes_settings.register`, the only place that writes settings. Settings
are loaded per request and per daily run, so edits show up without a restart (except
`web.host`/`web.port`, which take effect when the cockpit restarts).

### Bootstrap

`resolve_data_dir()` (env, else a legacy TOML's `paths.data_dir`, else `~/.blastjob`)
then connect, migrate, and under `BEGIN IMMEDIATE` import a legacy TOML once if the
settings table is empty (an `imported_from` marker row records it), then load. `init_db`
never needs a config object, which is what breaks the config/DB cycle.

### Navigation

`STAGE_NAV` (`web/app.py`) is the one list of pipeline stages; `base.html` renders it
as the nav bar, and `render()` injects `counts` from `app.state.nav_counts` into every
full-page template automatically (skipped for partials, whose names start with `_`).
Stage membership itself lives in `web/stages.py`; see "Pipeline stages are exclusive"
below. Until `candidate.is_configured()` is true, the cockpit shows a first-run banner
linking to `/settings`.

### Async work and streaming

Long tasks (discovery, matching, tailoring, ingest, research) run as background jobs
through `web/runtime.py`: a `Run` holds a replayable event log, and the browser follows
along over SSE with `Last-Event-ID` replay, so a refresh resumes a stream rather than
restarting the work. `Run.events` is capped at `MAX_EVENTS_PER_RUN` (oldest dropped
first) because this is a service that never restarts on its own and one event per
streamed LLM chunk is unbounded otherwise.

### Ingest pipeline (`core/ingest.py`, route `/work-history/ingest`)

1. `labeled_chunks()`: reads source files, wraps each in `<file path="...">` XML tags
2. Streaming LLM call with `INGEST_SYSTEM`: returns a JSON blob matching `MasterHistory`
3. `_parse_history()`: regex extracts JSON, Pydantic validates into `MasterHistory`
4. `_serialize_history()`: converts `MasterHistory` to canonical markdown (YAML
   frontmatter + `##` sections) and writes `work_history.md`
5. Second LLM call with `TEMPLATE_SYSTEM`: generates standard and ATS templates in one
   response, split on an `===ATS===` delimiter, writes `templates/standard.md` and
   `templates/ats.md`

### Build pipeline (`core/build.py`)

1. Reads `work_history.md` and the selected template from the data dir
2. `_research_company()`: non-streaming call using the `web_search_20250305` tool,
   returns raw markdown
3. `build_resume_messages()`: assembles a 3-breakpoint cached message list (see Prompt
   caching rules)
4. Streaming LLM call with `RESUME_SYSTEM` or `RESUME_ATS_SYSTEM`: streams resume markdown
5. Verifies the packet (see "Packet verification"), then exports to all selected formats
   via `exporters/`
6. Writes sidecars: `job_description.md`, `company_research.md`, `metadata.json`

Persona text in every prompt comes from `cfg.candidate.profile_text()`; never hardcode
a candidate, field or seniority in a prompt.

### Prompt caching rules

Three cache breakpoints per resume generation call:
1. System prompt (constant)
2. work_history.md (changes only after ingestion)
3. Template (standard.md or ats.md)

NEVER interpolate per-call data into cached blocks; it kills the cache hit rate.
Per-call data (job description, company research) goes in a trailing `plain_text()`
block.

### LLM client

`llm/providers.py` is the multi-provider LLM layer: `detect_provider()` picks Anthropic
API, OpenAI, or the `claude` CLI from settings; `make_stream()`/`anthropic_stream()` open
a fresh `AsyncAnthropic` client per call (no cached singleton) for streaming resume
generation; text chunks iterate via `async for text in stream.text_stream`.
`make_research_call()` handles company research: Anthropic's `messages.create()` with
the `web_search_20250305` tool when the provider supports it, else a plain completion
from training data.

### Job-search cockpit (discovery, match, tailor)

Backs the web app; all state in `<data_dir>/blastjob.db`.

- **Discovery** (`discovery/pipeline.py`): adapters fetch concurrently (per-source
  failure tolerant), normalize to `RawPosting`, then upsert by identity
  `(source, external_id)`. A refresh touches only source-owned columns, never the
  user's review/build state (and never blanks a stored JD: an empty description means
  "presence only"). A JD refresh compares quality classes, never lengths:
  presence stub < snippet (`is_jd_snippet`) < full < pasted; a lower class never
  replaces a higher one, a full JD replaces a full one (a real revision), and a
  changed JD clears an undecided row's score so `only_unscored` re-scores it. An
  empty company never overwrites a stored one, and a whole-card alert row never
  refreshes its title. Each posting is upserted in isolation (one bad row is a
  source error, not the end of the run). A source that returns 0 rows while >= 3
  are active, or (full snapshots only) under half of >= 10 active, is NOT
  stale-marked; the skip is a digest note. Cross-source dupes are grouped into a
  reversible cluster, never merged. ATS boards (Greenhouse/Ashby/Lever/Workday/
  Dayforce) need no key; Adzuna is optional (free key in the secrets file).
  - **Workday** (`workday.py`): `token = "tenant/wdN/site"`; listing POST per title
    query, detail GET only for postings without a stored JD; `source` is per board
    (`workday:{tenant}`) so one board's outage can't stale-mark another's jobs. A req
    open in several places stores primary + `additionalLocations` as "A; B; C" in
    `geo_location_raw` ("N Locations" placeholders dropped). Geo judges each segment on
    its own text, and a segment placed within range wins, so an out-of-state primary
    can't hide a home-state location; with none placed, the whole string is judged
    as before (an unplaceable segment never rescues an out-of-area posting).
    Same-role clustering places such a row at its primary location only. Rows stored
    before this are repaired by `--refetch-workday-locations[-report]`. Add the
    user's own employers to `discovery.target_boards`; `resolve.probe_workday` (or
    `python -m blastjob.discovery.resolve "<Company>"`) finds them via Jina search + a
    confirming POST.
  - **Dayforce** (`dayforce.py`): no key, but an anonymous CSRF handshake:
    `GET jobs.dayforcehcm.com/api/auth/csrf`, then echo the token in an `x-csrf-token`
    header on `POST /api/geo/{client}/jobposting/search`. Not a login; a bare POST
    answers a bare 403. Unlike Workday the JD ships with the listing, so there is no
    detail fetch and `known_ids` never stubs. Token is `clientNamespace/jobBoardCode`
    (e.g. `exampleclient/CANDIDATEPORTAL`), no host.
  - **Alert emails** (`mailalerts.py`): LinkedIn/Indeed job-alert mail over IMAP (app
    password; env-var names in settings, values in the secrets file). Incremental source
    (`snapshot = False`, so the pipeline never stale-marks for it); per-message ledger
    keyed by Message-ID in `mail_ledger`, written in ONE transaction with that
    message's postings (`MailAlertAdapter.batches`), each posting under its own
    SAVEPOINT: the message is `ok` if any posting saved (failures are source
    errors), `error` (retried) only if none did. IMAP is TLS-verified (`open_imap`),
    with a 30 s socket timeout and a 300 s fetch deadline that closes the connection
    and keeps the messages already read. This is the sanctioned LinkedIn feed: no
    browser automation, no unofficial APIs. Indeed parsing handles click-tracking
    wrappers (cts.indeed.com, pagead/clk, rc/clk/dl), template chrome, salary lines, and
    text/plain alerts; a recognized alert that parses to 0 postings logs a warning and
    dumps the body once to `<data_dir>/mail-debug/` (cap 20). **Whole-card LinkedIn
    layout** (`linkedin_cards.py`): one `<a>` wraps "<Title> <Company> · <City, ST> $pay
    Easy Apply" with no title/company delimiter. A split is applied only on structure:
    the text ends with a company already in the DB (`known_company_names`, loaded once
    per run by the pipeline), or the LinkedIn guest page header read through Jina
    confirms it (`guest_page_company`: title line + company line must rebuild the text
    exactly). A vocabulary-only split ("...Technology" + rest) is never applied at
    ingest ("Director of IT Acme Technologies" shows why): the title is the text before
    " · " (pay/badges stripped), company empty. A title-only link for the same job id
    beats a whole-card link; a bare "See job" anchor never does. Rows stored before the
    fix are repaired by `--repair-linkedin-titles[-report]` (`linkedin_repair.py`):
    sibling/DB-company splits apply, vocab splits are "needs confirmation" until passed
    via `--accept`; the report stages the repair in a SAVEPOINT, plans the recluster,
    and rolls back, so it predicts the apply run's merges exactly. The guest-page
    reader + pay parsing live in `linkedin_guest.py` (re-exported by `mailalerts`).
  - **Recruiter replies** (`replies.py`, in the daily run after closure): **surface
    only: never drafts, never sends.** Reads the user's PERSONAL mailbox
    (`discovery.personal_mail_*`; each field blank falls back to the alert account),
    which is often a *different account* from the alert feed: recruiters write to the
    address on the resume, while job alerts may be routed to another inbox. Pointing it
    at the alert mailbox made v1 structurally unable to see a reply. Gated on
    credentials, **not** on `mail_alerts_enabled` (that flag governs a different
    account's job feed). A sender must match an applied-to job (verbatim address in
    `app_notes`, else exact squashed domain == `company_key`) and that match happens at
    the HEADER, so unrelated personal mail is never body-fetched. 14-day window,
    `mail_ledger` source `'reply'` for dedupe. `--dry-run` passes `record=False`:
    stamping the ledger on a dry run would mark a reply seen and the real run would
    never surface it. A recording run also writes one `recruiter_reply` application
    event per surfaced reply (source `'replies'`, ref = Message-ID) in the ledger's
    transaction; a dry run writes none. Both mailboxes are editable at `/settings`
    (`web/mail_settings.py`): the form holds env var **names** only and REJECTS anything
    not name-shaped, so a pasted app password can't land in settings; presence renders
    as a boolean, never a value/length/prefix (the cockpit has no auth); the page shows
    the RESOLVED account and marks inherited fields, and
    `POST /settings/mail/test/{alert|personal}` runs a read-only login probe (classified
    login-rejected vs unreachable, redacted) against the SAVED settings, never a
    submitted host. `status()`/`probe()` load the secrets file lazily, because the web
    process otherwise reports working credentials as missing.
  - **Search firms** (`searchfirms.py`): weekly (`search_firm_weekday`, default Monday)
    incremental adapter over retained-search-firm sites listed in
    `discovery.search_firms`; they rarely post to ATS boards. Jina search
    (`site:{domain}` + title + region, at most 3 queries per firm); per-posting source
    `searchfirm:{slug}`, `snapshot = False`. Also runnable on demand via
    `blastjob-daily --sweep-search-firms` (outside the weekly gate; sends nothing,
    writes no `daily_runs` row). Expect low yield: most firms do not publish
    assignments in a searchable way, and practice/marketing pages are false positives
    filtered via `_NOT_POSTING`.
  - `resolve.py` maps a company name to its public board token; `python -m
    blastjob.discovery.resolve "<Company>"` is the CLI, probing Workday, Dayforce, and
    Greenhouse/Lever/Ashby per company name and printing confirmed boards. Jina-search
    candidate extraction is noisy: verify the name match (ideally live-fetch a sample
    posting and check its location) before trusting a hit. Expect most employers in a
    probe batch to resolve to nothing; that is normal.
- **Matching** (`matching/pipeline.py`), cheap to expensive: keyless geo filter
  (haversine from home to the towns listed in `discovery.towns`; two-tier: normal
  radius vs. an extended "verify hybrid" zone; another state or country on-site is far;
  US states only) → deterministic title prefilter (configured drop terms remove job
  *families*, never title *variants*; empty = keep all) → batched LLM alignment scoring
  (skipped entirely while `candidate` is unset) → gate.
- **Matching writes are CAS on `review_state`** (`'new','surfaced','queued'` only): a
  Keep/Archive clicked in the cockpit while the daily job's LLM batch is in flight
  always wins over the score that lands afterwards.
- **Gate** (`matching/gate.py`): surface at alignment ≥ threshold; a visible
  borderline band (threshold−5..threshold) and conditional/ambiguous geo go to the
  review queue; **unknown/undisclosed comp SURFACES (badged)**: only *verified* posted
  comp below the floor is a hard reject, and only a USD (or unlabelled $) posted MAX is
  comparable (`gate.below_floor`, also used by Compare and the funnel). "From $X" /
  "$X+" stores a minimum with max 0 (unknown), so it never hard-rejects; another
  currency surfaces too. A malformed model alignment is treated as missing (retried),
  never stored as 0. Two scores are kept distinct: this
  pre-generation alignment score vs. the post-generation groundedness `FitScore`.
  `alignment_threshold` defaults to 78. `comp_floor_usd` defaults to 0, which disables
  the comp gate until a floor is set.

### Web cockpit (`web/`)

FastAPI + HTMX bound to `web.host` (default `127.0.0.1`) by `blastjob-web`.
Every response carries `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `nosniff` and
`Referrer-Policy: same-origin` (added in `LocalRequestGuard`; no script policy yet).
State-changing routes require a per-launch CSRF token + local Origin check; all
untrusted job text renders through Jinja autoescaping. Discovery/matching/tailoring
run as background jobs via `web/runtime.py` (a replayable event log; tailoring streams
to the browser over SSE with Last-Event-ID replay so a refresh resumes rather than
restarts). Tailoring reuses `core.build.run_build`; a CAS build-claim + lease
(`build_runs`) prevents double-starts and recovers on launch.
`create_app(load_config, save_config)` is injectable so tests use a temp data dir.

`GET/POST /jobs/new` (`web/manual_job.py`): add a posting by hand for the channels no
discovery adapter covers (a screenshot, a forwarded link). Inserts one `jobs` row
(`source='manual'`, a deterministic sha256 `external_id` so resubmitting the same
posting lands on the same row instead of duplicating it) then runs the ordinary
`run_matching(..., job_ids=[id])` pass scoped to just that row: same gate/geo/
prefilter/CAS rules as a discovered job, nothing special-cased. That scoped run is
named `score-job` (kept off `web.runtime.SINGLE_FLIGHT` so it still runs while a
full matching pass is in flight; discovery and matching share ONE slot,
`FLIGHT_GROUPS`) and is in `BANNER_RUNS`, so the existing global run panel shows it;
no second progress system. An unsafe URL is dropped, not stored
(`web/urls.safe_external_url`), with a visible note on the job page.

`POST /jobs/{id}/jd`: store a pasted full description over a snippet
(`dbc.set_pasted_jd`). A score belongs to the JD it was computed from, and score
reuse keys on employer + title + JD, so a stale score beside new text would be
replayed. New text therefore clears an undecided row's score (the daily
`only_unscored` pass retries a NULL). A kept row keeps its old score, because
nothing retries a kept row; the re-score drops it from the reuse table. When the
text changed, a `score-job` run (`web/paste_rescore.py` →
`matching/rescore.rescore_job`) re-scores it: undecided rows through the scoped
`run_matching` (gate + CAS), kept rows score-only (never `review_state`, no comp
inference), archived rows not at all. Both score writes CAS on the `raw_jd` that
was scored, so a newer paste wins a race.
The paste response carries `HX-Trigger: refresh-run-status`; `base.html`'s `#run-status`
listens for it and re-reads `/runs/active`, which is what makes the re-score appear in
the banner without a page reload.

`POST /jobs/{id}/prep` (kept jobs): interview prep pack streamed via `web/prep.py` →
`core/prep.py`: role at a glance, JD→STAR table, likely Q&A, 30/60/90, questions to
ask, gaps; stories only from `work_history.md`, and `mark_unverified_numbers` appends
`[confirm]` to any money/percent figure not verbatim in the trusted text. `[confirm]`
marks are applied per completed line during streaming so browser and file agree;
numbers already known from the DB (comp signals, negotiated floor, job
comp_min/comp_max, company-profile figures) are rendered by `trusted_numbers_text()`
and treated as verified. Written role-prefixed as `{role-slug}.interview_prep.md` into
the job's newest successful `build_runs.artifact_dir` (falling back to today's
`{output}/{company-slug}-{date}/` only when there is no packet or its folder is gone),
then registered in the artifact index.
`GET /compare?state=surfaced|queued|kept&source=`: side-by-side table (comp, location,
profile facts, strengths/gaps, computed red flags incl. aggregator postings) via
`web/compare.py`, no LLM.

**Pipeline stages are exclusive** (`web/stages.py` = the one rule, `stage_of` + SQL
`*_WHERE` fragments; `web/stage_lists.py` = pages, counts, board; `web/stage_moves.py`
= CAS moves). Every job is on at most one stage page; header counts
(`stage_lists.counts`) are the page lengths:

| Stage (route) | Membership | Sort |
|---|---|---|
| Surfaced `/` | surfaced, active | fit, then newest |
| Queue `/queue` | queued, active | fit |
| Shortlist `/shortlist` | kept + `''`/`drafted` | fit (NULL last), `closed`/`likely_closed` sunk, build breaks ties |
| Applied `/applied` | kept + applied/screen/interview/offer | furthest stage, then due follow-ups, most overdue |
| Closed `/closed` | kept + rejected/ghosted/withdrawn/posting_closed/filled | `stage_changed_at` (migration 37) desc, else posting_closed_at, applied_at |

A cluster shows once: the kept member furthest along (in-process outranks closed,
closed outranks shortlist) decides its stage, else an archived member hides it, else
the best-fit surfaced/queued member. Matches is undecided only. `/kept` → `/shortlist`
and `/followups` → `/applied` redirect (307). Moves: Mark applied (Shortlist), Advance
/ Withdraw (Applied), Reopen (Closed → Applied if `applied_at`, else Shortlist), Remove
(Shortlist only; lands by the gate band on the stored score: stale → new, NULL →
queued). List-row actions post `from_list=1` and get an empty 200, so the row's
outerHTML swap removes it; a refused CAS answers `HX-Refresh`. Every
`application_state` write stamps `stage_changed_at` (service + closure transitions) and
records an application event in the same transaction (see below).
`service.set_application_state` to the state the job already holds is **not a change**:
it returns False and writes nothing (so re-picking the same dropdown value never
re-records a send or an outcome).

### Application events + artifact index (`core/events.py`, `core/artifacts.py`)

Migration 42 `application_events` (kind from `EVENT_KINDS`, ISO `at`, note, ref,
source). Writers: `set_application_state` (source `'state'`; one transaction with the
CAS, a failed insert rolls the move back), closure `close_transition` / `sweep_filled`
/ `reopen_transition` (`'closure'`), the reply scanner (`'replies'`), the backfill
(`'backfill'`), and the user on the job page (`'manual'`, the only source a delete may
remove: `WHERE id=? AND job_id=? AND source='manual'`). `drafted` and `''` write
nothing. `applied` is recorded only by the move that stamps `applied_at`; a return to
applied (Reopen) and a closure reopen are `note` events, because an "Applied" dated
today would claim an application that never happened. Newest first is
`at DESC, id DESC` everywhere; `events.latest_event(conn, job_ids)` is the one-query
batch for list rows.

Migration 43 `artifacts`: one row per absolute path, upserted by `register` (refreshes
sha256/bytes/updated_at; fills `job_id`/`build_run_id` only while NULL, never
reassigns). `artifacts.role_files` is the ONLY rule for "this role's files in a packet
folder": the `{role-slug}.*` sidecars, the resume/cover-letter documents for exactly
that company+role token pair (incl. `_ATS`, `.vN` rotations, `quarantine/`), and
`*.followup-{job_id}.md`; a legacy per-role folder (bare `metadata.json`) is taken
whole. Never a glob of a shared company-day folder. Hooks (best-effort, a warning on
failure, never a build-status change): tailor (`web/build.py`, after `finish_build`;
it also writes `{role-slug}.apply_link.html`, a one-link page to the cluster's best
apply route from `apply_link.apply_links_for`, via `web/apply_file.py`; no link, no file),
refine (`web/refine.py`, `job_id` passed by the route, re-scanned by `role_files`, not
refine's newest-by-mtime pick), follow-up draft, prep pack.

Job page: `_activity_files.html` lazy-loads `GET /jobs/{id}/activity`
(`web/routes_activity.py`): events + an add form (`POST /jobs/{id}/events`,
`POST /jobs/{id}/events/{event_id}/delete`, CSRF + Origin checked) and the Files list.
A file links to `GET /jobs/{id}/files/{name}` only when
`web/downloads.resolve_download` (the same resolver that route uses) would serve
exactly that path; otherwise the path shows as text (a same-named file in another or
newer packet folder would be served instead).

Every list (surfaced, queued, all) collapses cross-source duplicates to one row with a
`+N sources` badge (`service.collapse_clusters`) and badges staffing/recruiting
agencies as `staffing agency` (`compare.is_agency`) so they're not mistaken for the
employer.

### Same role, different route (`core/same_role.py`, `core/role_clusters.py`, `web/same_role.py`)

One job can show up several times wearing different hats. Titles group through ONE
normalizer (`core/titles.normalize_role_title`: vice president→vp, senior→sr,
information technology→it; drops parentheticals, remote/onsite/hybrid,
of/and/the/for/&), used by the discovery fingerprint and both rules below (not by
`closure_links`, whose matcher is calibrated on its own form).

- **Auto-cluster** (`role_clusters.auto_same_role`): same `company_key` + same
  normalized title, and either both from non-authoritative sources with `first_seen`
  within `AUTO_WINDOW_DAYS` (7; aggregators syndicate one req to many cities), or
  different sources in the same metro. A remote or unplaceable side counts as the same
  metro only when BOTH sides are non-authoritative; any pair with a board posting
  (including two different boards) needs strict proximity or both explicitly remote.
  The same-city-NAME shortcut also needs equal regions when both are known (Columbus
  OH is not Columbus GA); centroid/radius matching still crosses state lines.
  Two reqs on one authoritative board never merge; agency/aggregator companies never
  auto-merge. **A repost of an application in progress joins it** (an aggregator once
  re-served an applied-to req under a new ad id weeks later and it surfaced as new):
  exactly one side kept + applied/screen/interview/offer, the other with no application
  history (not kept, or kept with empty `application_state` and `applied_at`), both
  non-authoritative, STRICT same metro; then no time window, and the same source is
  fine. A closed application's repost is never merged (it may be a new req, and the
  merge would bury it in Closed); neither are two applications or a loose location.
  Applied by `run_discovery` to groups holding a posting new this run; backfill with
  `blastjob-daily --recluster-report` (prints merges + visible stage changes, writes
  nothing) / `--recluster`. Merge-only, cluster-level, never across a 'different' pair.
  A stored posting keeps its cluster on refresh: only merges and splits move it.
- **Agency ↔ employer is a suggestion** (`same_role.possible_same_role`): equal title,
  strict same metro, first seen within 60 days. The detail page warns (red when the
  other posting is already applied), rows get a chip, Mark applied / Advance carry an
  `hx-confirm` naming it, an agency posting with no match says "employer not named".
  `POST /jobs/{id}/same-role/{other}/{same|different}` (migration 38,
  `same_role_decisions`): 'same' joins the clusters (so the role shows once, in its
  furthest stage); 'different' splits the pair out of ANY cluster and suppresses it
  forever. The detail page's "Also listed as" lists every cluster member with that
  undo. A merge that puts a queued posting with an archived one hides the role
  (cluster rule). **A 'same' merge makes one application** (`web/same_role.decide`):
  merge, decision, and reconcile run as ONE `BEGIN IMMEDIATE` transaction. When the
  merged cluster holds more than one application (`role_clusters.has_application`:
  kept, state past `drafted` or an `applied_at`; a drafted packet is not an
  application and is never archived), the member furthest along
  (`stage_lists.representative_key`, the row the lists already show) survives; each
  other one is CAS-cleared on BOTH its `application_state` and `applied_at` and
  archived (`other`, "Duplicate of #N"), and the survivor's `app_notes` gets "Merged #id
  (source, state s, applied_at d) as a duplicate on <date>". Any CAS miss rolls the
  whole thing back and refuses. So withdrawing the survivor never resurrects the
  duplicate in Applied. A later 'different' split does not restore the duplicate's
  application (the note records it). `blastjob-daily --duplicate-applications` lists
  kept application pairs sharing company key + normalized title, clustered or not
  (read-only).
- **One "apply here" link per role** (`web/apply_link.py`, `best_apply_link`): the
  employer's own board posting ("employer site (Workday)"), then a board link closure
  found, then a `page:` or manual employer link, then LinkedIn > Indeed > Adzuna >
  other, an agency last; every URL through `safe_external_url`. Stage list rows (from
  the members `stage_lists` already loads), the detail header, and the daily digest
  (`apply_links_for`, one query) all use it. **Manual employer link**: the detail page's
  "Employer posting URL" form, `POST /jobs/{id}/employer-link`
  (`web/routes_employer_link.py`): CSRF + Origin checked; http(s), not an aggregator
  host, not LinkedIn (`page_fetch.fetchable`); never fetched. Stored as
  `employer_link_source='manual'`; an empty submit clears a manual link only.
  **DISPLAY-ONLY**: closure never probes it, never links over it, and never changes that
  row's verdict (an authoritative own source still decides); a typo or dead page must
  not "prove" a live job closed. Adding one stops closure tracking for that row (the
  conservative direction).
- **company_key drops a trailing division segment** (`core/company.py`): " - / – / | "
  + a segment naming a division/region/subsidiary ("Example Widgets Group - Global
  Division" → "example widgets group"); Group/Holdings/Brands and undelimited words
  stay. Per-user aliases live in `discovery.company_aliases`. Stored keys move only via
  the hand-run `rekey_companies` (conflicts reported, never overwritten), then a scoped
  recluster.
- **A repost of a role already applied to is a suggestion** (kind `previous`): the
  other posting is applied (any applied state, in progress or closed), not in this
  job's cluster, same non-empty `company_key`, equal title, neither side an agency,
  loose metro, no time window. Detail page: "You applied to this role on <Mon D>
  (<state>)" with the same/different buttons; list rows chip "applied before: <date>"
  (red only when that application is still in progress); Mark applied confirms by
  name. 'different' suppresses it like any other suggestion. The digest
  (`daily.pending_jobs`) skips any row whose cluster holds a kept or archived member,
  so a merged repost is never announced.

### Posting closure (`core/closure.py`, `core/closure_links.py`)

Runs in `blastjob-daily` after discovery + matching (migrations 29-36 on `jobs`).
Verdict per job in `posting_check`: an authoritative own source
(`workday:`/`greenhouse:`/`lever:`/`ashby:`/`dayforce:`) is `closed` once stale, else
`verified_open`. LinkedIn/Adzuna/manual/searchfirm/apple jobs need an **employer
link**: a cluster sibling (only if strictly same metro, both remote, or confirmed
'same' in `same_role_decisions`) or company+title match already in the DB (lifetimes
must overlap, and the two must share a location), else intermediaries
(`compare.is_agency`) are `unverifiable` with no request, else `resolve.py` finds the
board and we link only on an exact normalized title or the single candidate ≥
`TITLE_MATCH_MIN_RATIO` (uncalibrated) on a board whose slug/tenant IS the company
key. Linked postings are re-checked per run (Workday `confirm_open`, full-board
membership); a failed probe keeps the prior verdict. A stored sibling link is
re-checked with `sibling_is_proof` every run and dropped (verdict `unverifiable`, no
state move; a close that link had caused is reopened via `reopen_transition`) once it
fails; a 'different' decision clears links between the pair
(`role_clusters.clear_links_between`) and the network search never re-links a pair
the user rejected. `sweep_filled` records the `filled` packet outcome. A row whose link is the user's
`manual` one is skipped entirely (display-only; see "One apply here link"). Adzuna
stale with no link is `likely_closed` (badge only). Kept + `''`/`drafted` + closed →
`posting_closed` (prior saved; reopen restores unless the user moved it); applied →
badge + follow-ups desk, `filled` after 21 days; screen/interview/offer never move;
non-kept only get `posting_closed_at`. Manual "Posting closed"/"Still open" buttons
(`POST /jobs/{id}/posting/{closed|open}`) always apply and are never overturned.
**`discovery.closure_auto` defaults false (shadow mode):** verdicts and links are
written, application_state is not; the user turns it on with
`blastjob setup set discovery.closure_auto true`. `blastjob-daily --closure-report` is
the shadow run: moves nothing, sends nothing, writes no `daily_runs` row, prints kept
jobs. Every automated state move writes an application event (source `'closure'`):
`posting_closed`, `filled`, or a `note` on reopen. Shadow mode moves nothing, so it
writes none.

**Surfaced and Queue are checked too**: the network phase takes kept + active
surfaced/queued rows, ordered globally by `posting_checked_at` (never-checked first),
then stage (kept, queued, surfaced), then `alignment_score` desc; stage and fit only
break ties, so nothing starves. Caps: 15 searches, 40 probes per run. A row with an
empty `company_key` is skipped before any network call (verdict untouched, cap not
spent). Non-kept jobs still only get `posting_closed_at`; nothing archives them.
Surfaced and Queue sink `closed`/`likely_closed` rows to the bottom like Shortlist, and
a `verified_open` row carries a quiet "open · checked <date>" badge.

**Employer careers pages: the `page:<host>` link** (`core/page_links.py` trust/check,
`core/page_fetch.py` network). The LAST fallback in `resolve_link`: only when no board
matched and none was ambiguous, and a Jina key is set. One Jina search
(`"<company>" "<title>"`), the first 3 result URLs off `AGGREGATOR_HOSTS` (linkedin.com
is on it), each fetched and trusted only if: the host is not an aggregator; an ATS host
(the page host itself or a `<form action>` host, parsed, never a substring; `<script
src>`/`<link href>`/`<a href>` never count) is a known ATS family whose tenant (first
label, or first path segment for path-tenanted hosts) starts with the compacted company
key; the page names ONE `hiringOrganization` and it is this company; and exactly one
JobPosting on the page matches the title (exact or single ≥ 0.92, region folded from a
state name to its two-letter code). Exactly one trusted posting across pages → link,
else none. `check_page(link, company, title, region)` (via `LinkDeps.check_page`, never
`check_link`) re-verifies all of that: 404/410 from the SAME host with no cross-host
redirect → closed; 200 with the linked JobPosting still trusted → open unless
`validThrough` is past (then no evidence); anything else → no evidence. Every request
goes through `page_fetch.safe_get`: redirects followed by hand, ≤5 hops, every hop
checked BEFORE it is requested (http/https, not an aggregator, resolves only to public
addresses), the connection made to the address that was validated (Host header and
TLS SNI/certificate check keep the original name; no second DNS lookup, so no
rebinding window; `trust_env=False`; no connection reuse), body capped at 2 MB.
`allow_aggregators=True` (used only by `discovery/enrich.py` for Adzuna/Indeed pages)
admits job boards but never LinkedIn (`LINKEDIN_HOSTS`, incl. `lnkd.in`). `tests/conftest.py::_no_real_page_fetches` forbids the
real fetch/search suite-wide. **Known risk, accepted:** with `closure_auto` on, one
trusted 404/410 moves a kept, unapplied job straight to `posting_closed`. A false 410
from an employer's own ATS is unlikely, the move is reversible, and a later
`verified_open` restores it (`reopen_transition`); applied jobs only get a badge and
wait 21 days; screen/interview/offer never move.

### Company profiles + comp signals (`core/company.py`, `web/research.py`)

Company workups live as DATA: `company_profiles` (one row per normalized company name;
typed columns for comparable numbers, JSON for leadership/stack/events/sources, `extra`
for anything else) and `comp_signals` (every compensation observation per job:
aggregator estimate, posted range, market band, asked minimum, employer response,
offer). `negotiated_floor(job)` = highest stated minimum that drew no pushback.
Cockpit: `/companies`, `/companies/{key}`, snapshot + signal form on kept job pages,
and a "Research company" button that runs `web/research.py` (Jina search → read ~4
primary pages → LLM fills the schema from that material only; sources allowlisted to
fetched URLs). On an existing profile research writes only research-owned fact fields
(`company.RESEARCH_OWNED`) and only where the new value is non-empty (a thinner re-run
never erases a stored fact or its sources), fills `strategy`/`summary` only while empty, and never
touches user-owned fields (`workup_path`, ratings, risks, questions, `extra`); the
write is a CAS on the `updated_at` read when research started, and a conflict saves
nothing and says so. Long-form markdown workups still go in the application folder;
the profile row points at them via `workup_path`.

### People + addressed outreach (`web/people.py`, `models/contact.py`)

For a kept job, "Find people" runs Jina web search (never LinkedIn automation) for the
company's leaders in the candidate's field and its recruiters, then an LLM extracts
real people ONLY from those results; anything whose URL isn't in the results is
dropped (the one defense against an invented person). Stored in `contacts` (migration
10) with the source URL so the user verifies before writing. "Draft note to X" calls
the outreach draft with `contact_id`; the prompt addresses that person and shapes the
ask to their role (recruiter vs hiring manager). Still draft-only. Needs
`JINA_API_KEY`.

### Network (`core/network.py`, `/network`)

The user's own LinkedIn data export (ZIP or folder) is imported via
`POST /network/import`. Message bodies are never stored or logged: only counterparty,
latest date and count. Recommendations and messages match connections by normalized
name, and an ambiguous name credits nobody. `candidate.former_employers` drives the
"you worked here" alumni signal. Deterministic SQL and regex only; no LLM, no network.

### Archive reasons (feedback loop)

Archive takes a reason code (`level`/`domain`/`geo`/`comp`/`company`/`other`) + note.
`level`/`domain` archives are appended to the alignment prompt as calibration examples
(last 15); `company` auto-archives future postings from that company before any LLM
call; `geo`/`comp` are already deterministic gates.

### Daily autopilot + digest (`daily.py`, `notify/`)

`blastjob-daily` runs the same `run_discovery`/`run_matching` the cockpit uses, then
closure and the reply scan, then sends ONE digest per `notify.mode`: `discord` posts to
a private channel via a bot (messages carry a `🎯 blastjob` header, so the channel can
be shared with other notifiers; `DISCORD_BOT_TOKEN` / `DISCORD_CHANNEL_ID` in the
secrets file), `stdout` prints it (the default), `off` sends nothing. A run never fails
for missing Discord credentials unless the mode is `discord`. Contents: surfaced/queued
jobs with `notified_at = ''` (best score first, posting URL before the cockpit link
because the digest is read on a phone; the URL is the cluster's best apply link,
`web/apply_link.apply_links_for`, else the row's own), follow-ups due, per-source
errors, discovery notes (e.g. a stale-mark skipped for a suspiciously empty board),
and a Monday heartbeat. `notified_at` is stamped only after the channel's 2xx:
at-least-once by design. Runs on the machine that holds the DB and the `claude` login,
via `launchd/blastjob.daily.plist.template` (installed by
`sh scripts/install-launchd.sh daily` as `local.$USER.blastjob.daily`) →
`scripts/run-daily.sh` → `<data_dir>/daily.log`.

Every non-dry, non-paused run writes a `daily_runs` row (fetched/per_source,
scored/batches/time, new, sent, errors): `sent=0` before the post, `1` after;
`blastjob-daily --status` prints the last run. A sent digest carries an italic footer
(duration, top-5 sources, scoring throughput) and the Monday heartbeat appends 7-day
totals.

The cockpit can also run as a launchd service (`launchd/blastjob.web.plist.template`,
installed by `sh scripts/install-launchd.sh web` as `local.$USER.blastjob.web` →
`scripts/run-web.sh` → `blastjob-web --no-browser`) so the digest's cockpit links
resolve without starting it by hand; logs to `<data_dir>/web.launchd.log`. It binds
`web.host` from settings (default `127.0.0.1`). LAN access is an opt-in
(`blastjob setup set web.host 0.0.0.0`, then restart the service); the cockpit has no
login, so read the warning in `web/launch.py` first. On Linux, run `blastjob-web` /
`blastjob-daily` from systemd or cron.

### claude-cli provider (important)

`claude -p` is the full Claude Code *agent*, not a completion endpoint. The provider
(`llm/providers.py`) hardens it so it behaves as a plain model: prompt via **stdin**
(work-history frontmatter starts with `---`, which argv parses as a flag and crashes;
stdin also dodges arg-length limits), the caller's system prompt as an override + an
output-only rule (else it narrates into the output), dynamic sections excluded, run in
a per-process `mkdtemp(0700)` cwd, and stdout noise line-filtered. **It runs with NO
tools at all:** `--tools ""`, `--strict-mcp-config` (no `--mcp-config`, so no MCP
server) and `--setting-sources ""` (no user permission allowlist or hooks), plus
`--disallowedTools` as a backstop. Without them the subprocess loaded every MCP server
in the user's Claude config (334 tools on the owner's machine, GitHub writes
pre-allowed) while its prompt carries attacker-controllable postings. Output is
decoded incrementally (a multibyte character can straddle a read), the run has a
timeout (`CLI_TIMEOUT_SECONDS`), a non-zero exit or `is_error` raises `CLIError`, and
the child is killed on cancel or exit.

## Truth guarantees (read before touching the tailor or scoring path)

See `docs/ARCHITECTURE.md` for the full picture and what each of these cost when it
was missing. The short version:

- **Groundedness is model-assisted review, not a guarantee.** `core/scoring.py`
  verifies each `evidence_quote` is an exact substring of `work_history.md`, so a
  hallucinated quote cannot pass a fabricated claim. `web/fitscore.py` surfaces the
  verdict in the cockpit; below `GROUNDEDNESS_FLOOR_DEFAULT` (85, and
  **uncalibrated**: say so in any UI) a packet is badged "needs review", never blocked.
- **Comp provenance is a trust level.** Only `posted` is the employer's published
  figure and only `posted` may hard-reject a role; `inferred` is our own reading and
  goes to the review queue. `scorer.sanitize_comp` returns **no signal** when a
  figure's magnitude is ambiguous: a wrong salary is worse than a missing one.
- **Credentials are quoted, never paraphrased.** Coursework is not the degree, acting
  or interim is not the permanent title, and a certification in progress is not a
  certification; each would be an upgrade.
- **Prep-pack numbers** are trusted by whole-token match, never substring.
- **Never invent a person.** LLM-authored contacts and sources are allowlisted to URLs
  actually fetched; an ambiguous name credits nobody.
- **Only proof moves state.** A posting is `closed` only when a source that can prove
  absence says so; Adzuna ageing out is `likely_closed` and moves nothing, a failed
  probe is no evidence, an ambiguous employer link is no link, and a manual verdict or
  manual state change always beats automation.
- **A possible duplicate is a suggestion with evidence; only the user links two
  postings.** An agency's client is never named beyond "possibly the same role as <a
  posting already in the DB>"; a "not the same role" verdict is never overturned by the
  fingerprint, the daily run or the backfill.
- **Geo is three-way**: listed home-state town / affirmative other state / UNKNOWN →
  review queue. An unplaceable location must never be a hard reject.
- **A presence stub must not overwrite a field it does not know**, and a query-limited
  adapter is not a snapshot: age by `last_seen`, not by absence.
- **Every href from a feed or a model** goes through `web/urls.safe_external_url`.
  `base.html` carries the CSRF token, and the cockpit has no authentication (and may be
  opted onto the LAN via `web.host`), so this matters more, not less.
- **Two output layouts exist on disk.** Read `build_runs.artifact_dir`; never recompute
  a packet path from today's slugs.
- **Never request a linkedin.com URL** from any code path; alert email is the
  sanctioned LinkedIn feed.
- **Draft-only.** Nothing is ever submitted, sent or posted on the user's behalf.
- **Web is the only surface.** A capability with no route does not exist.

### Packet verification

Every build verifies the packet it just produced and records the verdict in
`{role-slug}.packet_report.json`. The vocabulary is `core/packet_report.py`
(`Finding`/`PacketReport`/`GateVerdict`); `core/packet_verify.py` runs the checks and
the hold, and `core/packet_gate.gate` decides.

- **Quarantine is per role and fails closed.** The packet folder is shared by every
  role tailored for that company that day, so quarantine and release work per stem
  (release from the cockpit frees only the job's own role). If any targeted file is
  still public afterwards, `QuarantineIncompleteError` → the build is FAILED, never
  tailored (`PacketNotQuarantinedError`; the folder is still recorded). Downloads
  refuse any file a QUARANTINED report names, wherever it sits.
- **A scorer that did not run is never an all-clear.** It goes in `checks_skipped`
  and the gate gives a visible "not checked" reason (still not blocking). A model
  `null` in a fit-score field becomes its default (an empty quote fails the check).
- **Refine re-verifies.** It targets the job's own role (never "newest file in the
  folder"), claims the job in the DB (`claim_for_refine`, a `refine:` build_runs row:
  tailor and refine exclude each other) plus a per-resume `flock`, never lifts a hold
  on a verification crash, stages and moves files atomically, re-scores
  the resume, runs the deterministic checks, gate and hold like a build, and
  re-scores the cover letter only if it changed.

- **FATAL is calibrated to the boards actually applied through, not to a parser
  vendor.** The defaults assume Workday, Dayforce, LinkedIn and Greenhouse; Textkernel
  sits behind none of them and its own docs call its codes risk signals (recalibrate if
  the user's boards differ). So fatal means: banned characters (a logged Workday
  rejection), encoding damage, a role that ended within 20 years with no bullets, a
  total that does not reconcile, or a cover-letter claim the research contradicts.
  Textkernel-derived rules are MINOR. A checker that cries fatal on compliant
  formatting teaches the user to ignore it.
- **A low groundedness score never blocks.** It is uncalibrated and fires on honest
  stretch applications; it produces a loud, specific warning instead.
- **No computed totals.** `check_arithmetic` allows a total only if that exact figure
  is verbatim in `work_history.md`; summing would have passed a real defect where a
  stated total was one true figure plus an unrelated second figure.
- **The cover letter is scored too** (`core/cover_letter_score.py`): claims about the
  candidate against the work history, claims about the employer against the research.
  **New and uncalibrated: it over-flags; treat its number as a prompt to read, not a
  verdict.**
- **Research is a trust boundary** (`core/research_facts.py`): distilled in an isolated
  call into span-verified, polarity-bearing facts, and the cover letter consumes those
  facts, never the raw sidecar. "Considered entering aerospace" cannot become "serves
  aerospace".
- **The third bucket exists** (`core/coverage_gap.py`): requirements the history proves
  that the resume never used. Read-only, and the only automatic remedy is inserting the
  user's own sentence verbatim; an LLM repair would turn "piloted a scheduling tool"
  into "led the scheduling rollout", pass every check, and lie.
- **As-sent attribution** (`core/packet_sends.py`, migrations 39/40): hashes are
  captured when a job is marked applied, so a later rebuild cannot reattribute an
  outcome to a document that was never sent. Reporting is descriptive only; **no style
  ranking at any sample size.**

## Running

```bash
uv sync --extra dev
uv run blastjob setup doctor   # then follow SETUP.md for anything it flags
uv run blastjob-web            # web cockpit at http://127.0.0.1:8765
# LLM works with no API key via the claude CLI; set ANTHROPIC_API_KEY to use the API.
```

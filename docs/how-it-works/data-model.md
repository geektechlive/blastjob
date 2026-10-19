# Data model

**What this covers:** Where state lives: the data dir, the SQLite database and its main tables, how settings are stored, and every settings group. For anyone adding a table, a column or a setting.

## Storage

State lives in two places: a SQLite database and flat files, both inside the data dir (plus the output folder for packets, see [events and filing](events-and-filing.md#output-layout)). SQLite is the standard-library module in WAL mode. `client.connect_at` opens every connection with `journal_mode=WAL`, a 5 second `busy_timeout` and `foreign_keys=ON`, so the cockpit and the daily job can write at the same time without hard-failing on `SQLITE_BUSY`. Callers keep transactions short.

The data dir and the database hold one person's job search, so they are created owner-only: `0700` for the directory and `0600` for the database file. Only what a call creates is tightened. An existing directory or file keeps the permissions its owner gave it.

Writes to a row the user might also be changing are compare-and-set: the `UPDATE` re-checks the state it expects, and a miss changes nothing. The stage moves ([web](web.md#stage-moves)), matching writes and build claims all work this way.

## Data dir

The data dir is `BLASTJOB_DATA_DIR`, default `~/.blastjob`. It is the only setting that is not stored in the database, because the database lives inside it. `resolve_data_dir` (in `src/blastjob/config.py`) finds it first; see [bootstrap](web.md#bootstrap).

What it holds:

- `blastjob.db`: the SQLite database. The tables are listed under [Tables](#tables).
- `work_history.md`: your canonical work history.
- `templates/`: the standard and ATS resume templates generated from the work history.
- `mail-debug/`: the HTML body of any alert email that parsed to zero postings, so a changed email layout can be diagnosed. At most 20 files are kept.
- `backups/`, the logs, the kill-switch file and the default secrets file. These are covered in [your data and backups](../operations/data-and-backups.md) and [settings and secrets](../operations/settings-and-secrets.md).

Packets are not in the data dir. They go to `paths.output_dir`, which defaults to `~/Documents/blastjob/`.

## Settings storage

Settings are rows of the `settings` table (migration 44) in `<data_dir>/blastjob.db`: one row per dotted leaf key, holding a JSON value. A leaf is a field of the config model that is not itself a model, so a list of boards or a map of towns is one leaf, however it is shaped. The Pydantic model decides what a leaf is (`settings_store.LEAVES`), and every load validates the known rows through it.

The contract, from `settings_store`:

- **Unknown keys are kept and reported, never dropped.** A key the model does not know stays in the table, is carried untouched by every save, and is returned in `settings_store.LoadedSettings`.
- **Saves upsert only the keys whose value changed.** Nothing ever rewrites or deletes the whole table.
- **A save can refuse to overwrite a newer edit.** The revision is the newest `updated_at` in the table. A config object remembers the revision it was loaded at, and a save made from a stale revision raises `settings_store.StaleSettings`. The settings page answers 409 and asks you to reload, instead of silently overwriting. This is what makes two open tabs, or a tab and `blastjob setup set`, safe.
- **`paths.data_dir` is never stored.** See above.
- **Rows whose key starts with `_meta.` are bookkeeping**, invisible to the model. The only one today is the legacy-import marker.
- **Settings that name a secret hold the name of an environment variable, never the value.** The fields that do (`settings_store.SECRET_REF_KEYS`) refuse anything not shaped like a variable name, and the secrets file path must be absolute and outside the checkout, where it could be committed. This is a shape check on those fields, not secret detection.

A legacy `config.toml` (the platformdirs location, for example `~/Library/Application Support/blastjob/config.toml` on macOS) is imported once on first start and left in place as a backup. `settings_store.import_legacy_once` does it: only when the `settings` table is empty and the file exists, under one write lock, so two processes starting together import once. See [bootstrap](web.md#bootstrap) for where it runs.

**Settings are not transactionally isolated from a running job.** A few settings are read by pure helpers that take no config object (company aliases, employer domains, extra agency names, extra network agency keywords, the words that mark an email subject as a job alert, and the secrets file path). `apply_runtime_settings` re-installs them on every `load_config` (both in `src/blastjob/config.py`), so an edit made during a daily run can take effect partway through that run.

## Settings reference

Every group below is a section of the config model in `models/config.py`. Edit them with `blastjob setup set` or at `/settings`; see [settings and secrets](../operations/settings-and-secrets.md). The last column says where the behavior is explained.

| Group | Holds | Explained in |
|---|---|---|
| `paths.*` | `data_dir` (not stored), `output_dir`, `secrets_file` | [Data dir](#data-dir), [output layout](events-and-filing.md#output-layout) |
| `llm.*` | `provider` (auto, anthropic, openai or claude-cli), the two key-variable names and the two model names | [the LLM layer](llm.md) |
| `generation.*`, `pricing.*` | `max_web_searches`, and the per-million-token prices used for cost tracking | [the LLM layer](llm.md) |
| `candidate.*` | `target_titles`, `seniority`, `field`, `former_employers` | [matching](matching.md) |
| `discovery.*` | everything about finding and matching roles, below | [discovery](discovery.md), [matching](matching.md), [closure](closure.md) |
| `web.*` | `host` and `port` | [security model](web.md#security-model) |
| `notify.*` | `mode` (`discord`, `stdout` or `off`) and the two Discord variable names | [daily](daily.md) |

`candidate.*` starts empty. A fresh install has no profile, and matching makes no LLM call until `candidate.target_titles` is set.

`discovery.*` covers:

- **Scoring and the gate:** `alignment_threshold`, `comp_floor_usd`, `title_keep_terms`, `title_drop_terms`.
- **Location:** `home_location`, `home_state`, `home_lat`, `home_lon`, `towns`, `commute_radius_miles`, `extended_radius_miles`, `remote_ok`.
- **Sources:** `target_boards`, `enable_aggregators` with `adzuna_queries`, `adzuna_where` and the Adzuna key-variable names, the alert-mail settings (`mail_alerts_enabled`, `mail_imap_host`, `mail_user_env`, `mail_password_env`, `mail_mailbox`, `mail_lookback_days`, `mail_alert_subject_terms`), the personal-mailbox settings (`personal_mail_*`), the search-firm settings (`search_firms_enabled`, `search_firm_weekday`, `search_firms` and its queries, regions and places), the Apple settings (`apple_enabled`, `apple_queries`, `apple_locations`, `apple_max_reads`), and `provider_timeout_s` with `provider_max_retries`.
- **Names and trust:** `exclude_companies`, `company_aliases`, `discovery.employer_domains` (company name to the web domains you confirm belong to it), `agency_companies`, `network_agency_keywords`.
- **Closure and descriptions:** `closure_auto` and the employer-description settings (`employer_jd_lookup`, `employer_jd_min_score`, `employer_jd_daily_requests`).

**Defaults are safe, not empty.** A fresh install holds no personal data and fetches or spends nothing until you fill a list. `build_adapters` (in `discovery/pipeline.py`) builds no adapter while the mail switch is off and `target_boards`, `search_firms` and `apple_queries` are empty, and aggregators are off. Matching waits for `candidate.target_titles`. `closure_auto` is off, so closure runs in shadow mode. Several defaults are not empty, though: `alignment_threshold` is 78 (see [the gate](matching.md#gate)), the commute and extended radii are 35 and 55 miles, `remote_ok` is on, `search_firms_enabled` and `apple_enabled` are on (they do nothing without their lists), the alert mailbox defaults to an iCloud host with matching variable names, `apple_locations` has two entries, and the employer-description settings have numeric defaults with the lookup itself off. `notify.mode` defaults to `stdout`, and `web.host` and `web.port` default to `127.0.0.1` and 8765.

## Tables

The schema is `schema.MIGRATIONS`, an ordered list of `(version, sql)` entries applied by `client.migrate` and recorded in `schema_migrations`. The rules:

- **Never edit a shipped migration. Append a new one.**
- **One ALTER per version.** A single `ADD COLUMN` is atomic, so a failure cannot leave a version half-applied.
- **Duplicate-column errors are tolerated.** The cockpit and the daily job can race the first launch after a schema bump, and the loser sees "duplicate column name". `client.migrate` then replays the script statement by statement, skipping only the duplicate-column ones, so a script is never recorded as done with later statements missing.
- `build_runs.owner_token` is added ad hoc after the numbered migrations, with the same tolerance. It records which process claimed a build, so a restart can tell a dead build from a long-running one.
- `client.init_db` migrates once per database per process, because the web layer calls it on every request.

| Table | Migration | Holds | Explained in |
|---|---|---|---|
| `jobs` | 1, then many `ALTER`s | every posting, with source, score, geo, comp, review, build and application state, closure fields and description provenance | most pages |
| `job_clusters` | 1 | one row per cross-source same-role group | [same role](same-role.md) |
| `build_runs` | 1 | one row per tailor or refine claim, with a lease and the packet folder | [tailoring](tailoring.md) |
| `companies` | 1 | created by the first migration; the current code neither reads nor writes it | none |
| `mail_ledger` | 7 | one row per processed email, so a message is neither replayed nor lost | [discovery](discovery.md) |
| `contacts` | 10 | people found for a kept job, with the URL they came from | [employer data](employer-data.md) |
| `company_profiles` | 11 | one company workup per normalized name | [employer data](employer-data.md) |
| `comp_signals` | 12 | every compensation observation per job | [employer data](employer-data.md) |
| `daily_runs` | 13 | one row per `blastjob-daily` run | [daily](daily.md) |
| `network_contacts`, `company_follows` | 14 to 25 | your LinkedIn export, and the enrichment results | [network](network.md) |
| `company_exclusions` | 26 | companies you excluded, as an explicit decision | [matching](matching.md) |
| `same_role_decisions` | 38 | your same or different verdict on a pair of postings | [same role](same-role.md) |
| `packet_sends`, `packet_outcomes` | 39, 40 | what was sent and what happened to it | [truth guarantees](../truth-guarantees.md) |
| `application_events` | 42 | the application timeline | [events and filing](events-and-filing.md#application-events) |
| `artifacts` | 43 | the index of files written for a job | [events and filing](events-and-filing.md#artifact-index) |
| `settings` | 44 | the settings rows | [Settings storage](#settings-storage) |
| `schema_migrations` | n/a | which migrations ran | here |

Migration 41 adds an index on `jobs` for the three kept-stage queries, which filter on `review_state='kept'` and `COALESCE(application_state,'')` (see [pipeline stages](web.md#pipeline-stages)). The expression index is needed because a plain index on the column cannot serve a query built around `COALESCE`.

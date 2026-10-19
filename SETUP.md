# Setting up blastjob

**For the AI assistant (or human) setting up this repo for someone: read this first.**

blastjob ships empty. There is no candidate, no target role, no location, no
companies and no data. Everything that describes the person searching lives in
their own database (the `settings` table), and the app will not spend a single
LLM call on matching until their profile is filled in.

Your job, if you are an AI assistant helping someone set this up:

1. Get it installed and running.
2. **Run the setup interview below with them, one section at a time.** Write each
   answer with `uv run blastjob setup set <dotted.key> <value>`.
3. Ingest their resumes and have them review the result.
4. Finish with `uv run blastjob setup doctor` and fix whatever it reports.

**Don't guess any answer. Ask.** If they don't know, leave the default (every
default is empty or off, and safe) and tell them what it controls.

None of their data belongs in the repo. See "Never do" at the end.

---

## 1. Prerequisites and install

Follow the [README quick start](README.md#quick-start) (or
[Install blastjob](docs/getting-started/install.md) for each step in detail):
macOS or Linux, Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), one LLM
(a logged-in `claude` CLI, `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`), then
`uv sync --extra dev` and `uv run blastjob setup doctor`. Expect the doctor to
fail until the interview below is done, because nothing is set yet.

Optional keys, each unlocking one extra: `JINA_API_KEY` (people search, company
research, search firms, employer-page closure checks), Adzuna app id + key (an
aggregator feed), an IMAP app password (job-alert email), a Discord bot token +
channel id (daily digest). `JINA_API_KEY` is also what lets
`python -m blastjob.discovery.resolve` find Workday and Dayforce boards; without it
that command probes only Greenhouse, Lever and Ashby (and says so).

Where the data dir, the secrets file and the output folder live, what each holds
and what leaves the machine is in
[Your data and backups](docs/operations/data-and-backups.md); tell the user about
it. None of these is inside the repository. **Ask the user where their documents
should go.** On a Mac with iCloud "Desktop & Documents" on, the default
(`~/Documents/blastjob`) syncs to iCloud; if they don't want that,
`uv run blastjob setup set paths.output_dir <path>`.

Create the secrets file now, private to the user:

```bash
mkdir -p ~/.blastjob && touch ~/.blastjob/secrets.env && chmod 600 ~/.blastjob/secrets.env
```

([Install](docs/getting-started/install.md#create-the-secrets-file) explains the
file and its location.) **You (the assistant) should not read or echo secret values.**
Tell the user which line to add and let them paste the value themselves.

## 2. How `setup set` takes values

The value rules (plain words, booleans, JSON in single quotes for lists and
objects, refusal of unknown keys and out-of-range values, `*_env` fields taking
a variable **name** never a secret) are in
[settings and secrets](docs/operations/settings-and-secrets.md#changing-settings).
`uv run blastjob setup get <key>` shows one value; `get` with no key shows everything.

Everything below can also be edited later at `/settings` in the cockpit.

---

## 3. The setup interview

Ask one section at a time. Skip optional sections the person doesn't want.

### 3a. Who they are (required: nothing is scored without target titles)

| Ask | Write to |
|---|---|
| Which job titles would you apply for? Include the variants employers use. | `candidate.target_titles` (JSON list) |
| What level: individual contributor, manager, director, executive? | `candidate.seniority`. Decides whether the fit score weighs hands-on skills and tools, or scope and leadership. |
| What field or function? | `candidate.field` (e.g. "data engineering", "nursing", "corporate finance") |
| Which employers have you worked for, spelled as on your resume? | `candidate.former_employers` (JSON list; the warm-introduction signal) |
| Where do you work now, and is there anyone you never want to see jobs from? | `discovery.exclude_companies` (JSON list) |
| What's the lowest base pay you'd accept? (0 means don't filter) | `discovery.comp_floor_usd`. Only an employer-*posted* figure below it rejects a job; unknown pay is always shown. |

Then help them write the title prefilter. Both lists empty means every title gets
scored, which is fine but costs more LLM calls.

| Ask | Write to |
|---|---|
| Words that mean "always look at this" (their core title nouns) | `discovery.title_keep_terms` (JSON list) |
| Whole job *families* they never want (e.g. "sales representative" for an engineer) | `discovery.title_drop_terms` (JSON list). Families, not variants: never drop "lead" or "principal". |

Example:

```bash
uv run blastjob setup set candidate.target_titles '["Data Engineering Manager", "Director of Data Engineering", "Head of Data"]'
uv run blastjob setup set candidate.seniority "manager to director"
uv run blastjob setup set candidate.field "data engineering"
uv run blastjob setup set candidate.former_employers '["Example Analytics Inc", "Sample Logistics Co"]'
uv run blastjob setup set discovery.comp_floor_usd 150000
uv run blastjob setup set discovery.title_drop_terms '["sales representative", "account executive"]'
```

### 3b. Where they live (US only)

| Ask | Write to |
|---|---|
| Home town and state? | `discovery.home_location` (display text), `discovery.home_state` (two-letter code), `discovery.home_lat`, `discovery.home_lon` |
| Do you take remote roles? | `discovery.remote_ok` |
| How far would you commute daily? Further if hybrid? | `discovery.commute_radius_miles`, `discovery.extended_radius_miles` (the extended radius must be at least the commute radius) |
| Which towns would you consider working in? | `discovery.towns`: a JSON object of `"town name": [lat, lon]`, home town included, names lowercased |

Look up the coordinates for them from any public gazetteer and show them what you
found. How each location is judged is in
[matching](docs/how-it-works/matching.md#geo).

List only towns they would actually consider. A town in the table that is beyond
the extended radius becomes a hard reject; the same town left out lands in the
review queue instead. Add towns later as unknown ones show up in the queue.

```bash
uv run blastjob setup set discovery.home_location "Springfield, OH"
uv run blastjob setup set discovery.home_state OH
uv run blastjob setup set discovery.home_lat 39.92
uv run blastjob setup set discovery.home_lon -83.81
uv run blastjob setup set discovery.towns '{"springfield": [39.92, -83.81], "dayton": [39.76, -84.19]}'
uv run blastjob setup set discovery.commute_radius_miles 35
uv run blastjob setup set discovery.extended_radius_miles 55
uv run blastjob setup set discovery.remote_ok true
```

### 3c. Where jobs come from

| Ask | Write to |
|---|---|
| Which employers do you most want to work for? | `discovery.target_boards`: a JSON list of `{"company": ..., "ats": ..., "token": ...}`. **This list is the biggest lever on coverage.** |
| Do you get LinkedIn or Indeed job-alert emails? Which mailbox? | `discovery.mail_alerts_enabled`, `discovery.mail_imap_host`, `discovery.mail_user_env`, `discovery.mail_password_env` (env-var NAMES; values go in the secrets file) |
| Do recruiters write to a different mailbox? | `discovery.personal_mail_imap_host`, `discovery.personal_mail_user_env`, `discovery.personal_mail_password_env` (blank means reuse the alert mailbox) |
| Want the Adzuna aggregator? (free key) | `discovery.enable_aggregators`, `discovery.adzuna_queries` (their titles), `discovery.adzuna_where` (their town) |
| Senior search? Any retained search firms in your market? | `discovery.search_firms`, `discovery.search_firm_queries`, `discovery.search_firm_regions`; otherwise `discovery.search_firms_enabled false` |
| Interested in Apple? | `discovery.apple_queries`; otherwise `discovery.apple_enabled false` |

**Finding a company's board.** For each employer they name, run:

```bash
uv run python -m blastjob.discovery.resolve "Example Widgets"
```

It probes Workday, Dayforce, Greenhouse, Lever and Ashby and prints confirmed
boards. Search-based matching is noisy: **open one sample posting and check the
company and location before trusting a hit.** A wrong token delivers dozens of
irrelevant postings a day (the cockpit's `/funnel` page flags boards that never
score near the threshold). Most employers in a batch resolving to nothing is
normal.

```bash
uv run blastjob setup set discovery.target_boards '[{"company": "Example Widgets", "ats": "greenhouse", "token": "examplewidgets"}, {"company": "Sample Health", "ats": "workday", "token": "samplehealth/wd5/External"}]'
```

**Alert mail.** Use an app password, not the account password. Write the env-var
names into settings (follow the
[naming rule](docs/operations/settings-and-secrets.md#mail-accounts)), and have the
user add the values to the secrets file:

```bash
uv run blastjob setup set discovery.mail_alerts_enabled true
uv run blastjob setup set discovery.mail_imap_host imap.example.com
uv run blastjob setup set discovery.mail_user_env ALERT_IMAP_USER
uv run blastjob setup set discovery.mail_password_env ALERT_IMAP_APP_PASSWORD
# the user adds ALERT_IMAP_USER=... and ALERT_IMAP_APP_PASSWORD=... to the secrets file
```

The `/settings` page has a "Test connection" button for each mailbox. It never
shows the secret, only whether it is present.

### 3d. How they want to hear about jobs (optional)

| Ask | Write to |
|---|---|
| A daily digest of new roles? Where? | `notify.mode`: `discord`, `stdout` (default: printed to the daily log), or `off` |
| Discord? | needs `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` in the secrets file |
| Use the cockpit from a phone or tablet on the home network? | `web.host` (`127.0.0.1` default; `0.0.0.0` for LAN) and `web.port` (default 8765). Takes effect after restarting the cockpit. |
| Keep secrets somewhere other than `<data_dir>/secrets.env`? | `paths.secrets_file` |

**Before setting `web.host` to `0.0.0.0`, tell the user plainly: the cockpit has
no login.** That is their decision to make, not yours. Walk through
[LAN access](docs/operations/lan-access.md) with them first.

### 3e. Their work history

Every resume, cover letter, score and truth check is measured against one file,
`<data_dir>/work_history.md`. Build it in the cockpit:

1. Open `http://127.0.0.1:8765/work-history/ingest`.
2. Upload their resumes (PDF, DOCX or text; several versions is better than one).
   Ingest merges them into one deduplicated history and generates the standard
   and ATS templates.
3. Have them **read and correct `work_history.md` by hand** at `/work-history`.
   Dates, titles and figures must be exactly right; credentials must be stated
   exactly as they are (coursework is not a degree, a pending application is not
   a granted one). Nothing the app writes can be more accurate than this file.

Then add one job by hand at `/jobs/new` and tailor a packet, to see the whole flow
end to end.

---

## 4. Running it every day (optional)

Offer to keep the cockpit always on and run discovery every morning. The steps for
macOS (launchd) and Linux (systemd or cron), the `PAUSE` kill switch and
`uv run blastjob-daily --dry-run` are in
[Running the services](docs/operations/services.md).

## 5. Verify

```bash
uv run blastjob setup doctor
```

Fix what it reports, then run it again; it exits non-zero until everything passes.
[The setup doctor](docs/operations/doctor.md) lists what it checks and the fix for
each item.

## 6. Tuning after the first week

[Your first week](docs/getting-started/first-week.md) covers tuning
`discovery.alignment_threshold` (it is uncalibrated for a new profile), archiving
with a reason, staffing agencies, company aliases and turning on closure. Point the
user to it once they have a week of results.

## 7. Never do

- **Never commit their data.** Work history, the DB, settings exports, generated
  documents, contacts, mail and notes all stay in the data dir or the gitignored
  `local/` and `CLAUDE.local.md`. Before any commit to a fork, run
  `uv run python scripts/check_no_personal_data.py`.
- **Never put a secret value in settings.** Settings hold env-var NAMES only;
  values live in the secrets file, which is never committed and should be mode 600.
- **Never read, print or paste a secret value** into the conversation, a log or a
  file other than the secrets file.
- **Never guess an interview answer**, a town's coordinates without showing your
  source, or a board token without checking a sample posting.
- **Never turn on LAN access without the user's explicit decision**, and never
  add authentication schemes or remove the CSRF/Origin checks.
- **Never weaken the truth rules** in [Truth guarantees](docs/truth-guarantees.md):
  draft-only, groundedness checks, comp provenance, unplaceable locations go to
  review.
- **Never point a test at the network, a real LLM, or the real data dir.**

---

## Upgrading and importing an old config

See [Your data and backups](docs/operations/data-and-backups.md#upgrading) for
upgrading a clone or fork, and
[Settings and secrets](docs/operations/settings-and-secrets.md) for importing a
legacy `config.toml` with `uv run blastjob setup import`. Values an old template held
in code rather than config need to be entered through the interview above.

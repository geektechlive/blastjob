# Install blastjob

**What this covers:** Getting blastjob installed and through its first run: prerequisites, the secrets file, your profile and location, your work history, your first job sources, and one job tailored end to end. For a person setting it up by hand. If an AI assistant is doing the setup with you, point it at [SETUP.md](../../SETUP.md) instead; that file is the assistant's interview script and this page does not repeat it.

## Prerequisites

- macOS or Linux. The always-on launchd services are macOS only; on Linux you use systemd or cron (see [services](../operations/services.md#linux)).
- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).
- One language model, any of:
  - a logged-in [Claude Code](https://claude.ai/code) `claude` command line tool (no key needed);
  - an Anthropic API key, which adds prompt caching and live web search for company research;
  - an OpenAI API key.
- Optional: the pango and cairo system libraries, which WeasyPrint needs for PDF export. On macOS run `brew install pango`. Without them Markdown and DOCX files are still produced and PDF is skipped.

Optional keys each unlock one extra feature. They are listed in [settings and secrets](../operations/settings-and-secrets.md#optional-keys).

## Install

```bash
git clone <this repo> blastjob && cd blastjob     # or your fork
uv sync --extra dev
uv run --extra dev pytest -q                      # the whole suite passes offline
uv run blastjob setup doctor                      # expect failures: nothing is set yet
uv run blastjob-web                               # cockpit at http://127.0.0.1:8765
```

`uv sync` creates the `.venv` folder in the checkout. The background services use it, so do not delete it. The [doctor](../operations/doctor.md) is safe to run at any point and spends no tokens.

Everything blastjob knows about you lives in three places outside the repository: the data folder, the secrets file and the output folder. [Your data and backups](../operations/data-and-backups.md#where-your-data-lives) lists what each holds and what leaves your machine.

## Create the secrets file

Create the file now, readable only by you, even if it stays empty for a while:

```bash
mkdir -p ~/.blastjob && touch ~/.blastjob/secrets.env && chmod 600 ~/.blastjob/secrets.env
```

If you set `BLASTJOB_DATA_DIR`, use that folder in place of `~/.blastjob`. The file holds one `KEY=value` line per secret. [Settings and secrets](../operations/settings-and-secrets.md#the-secrets-file) explains how it is read.

## Choose where documents go

Resumes, cover letters and prep packs are written to `~/Documents/blastjob` by default. On a Mac with iCloud "Desktop & Documents" turned on, that folder syncs to iCloud. If you do not want that, pick another folder:

```bash
uv run blastjob setup set paths.output_dir ~/job-search-documents
```

blastjob files the folders inside it for you as jobs change stage; see [your data and backups](../operations/data-and-backups.md#where-your-data-lives).

## Tell it what you want

Nothing is scored until you set at least one target title, so this step matters most. The commands below are an example; every value is yours to change. You can set the same things on the **Settings** page of the cockpit. The full question-by-question list is in [SETUP.md](../../SETUP.md) section 3.

```bash
uv run blastjob setup set candidate.target_titles '["Data Engineering Manager", "Head of Data"]'
uv run blastjob setup set candidate.seniority "manager to director"
uv run blastjob setup set candidate.field "data engineering"
uv run blastjob setup set discovery.home_location "Springfield, OH"
uv run blastjob setup set discovery.home_state OH
uv run blastjob setup set discovery.home_lat 39.92
uv run blastjob setup set discovery.home_lon -83.81
uv run blastjob setup set discovery.towns '{"springfield": [39.92, -83.81], "dayton": [39.76, -84.19]}'
uv run blastjob setup set discovery.remote_ok true
```

Town names are lowercase and each maps to `[latitude, longitude]`. List only towns you would consider working in. How each location is judged is in [matching](../how-it-works/matching.md#geo). List only towns you would consider, and add more later if they turn up in the review queue. How values are written is covered in [settings and secrets](../operations/settings-and-secrets.md#changing-settings).

## Add your work history

Every resume, cover letter and score is measured against one file, your work history. Nothing blastjob writes can be more accurate than that file.

1. Start the cockpit with `uv run blastjob-web` and open `/work-history/ingest`.
2. Upload your resumes as PDF, DOCX or text. Several versions are better than one. Ingest merges them into one history and builds the resume templates.
3. Open `/work-history` and read the result line by line. Fix dates, titles and figures by hand, and state credentials exactly as they are.

## Add job sources

Add sources on the **Settings** page or with `setup set`:

- Company job boards. `discovery.target_boards` is the biggest lever on coverage. It is a JSON list of `{"company", "ats", "token"}` objects.
- Job-alert email from LinkedIn or Indeed. See [mail accounts](../operations/settings-and-secrets.md#mail-accounts).
- The Adzuna job aggregator (a free key).

[Discovery](../how-it-works/discovery.md) explains each source. SETUP.md section 3c walks through finding a company's board.

## Try one job end to end

Once your work history is in, open `/jobs/new`, paste one job you know, and tailor a packet for it. That shows the whole flow, from scoring to documents, before you wait for the first daily run. Then run `uv run blastjob setup doctor` again; it should now list only features you have not turned on. When the lists start to fill, read [your first week](first-week.md). To run it on a schedule, see [running the services](../operations/services.md).

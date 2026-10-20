# Your data and backups

**What this covers:** Where everything blastjob knows about you lives, what leaves your machine, how backups work, how to restore from one, how to upgrade, and the repair commands. For anyone who owns an install.

## Where your data lives

Everything blastjob knows about you lives in three places on your own machine. None of them is inside the repository, so `git` cannot see them.

| What | Where |
|---|---|
| **Data folder** | `BLASTJOB_DATA_DIR`, default `~/.blastjob` |
| `blastjob.db` | Your settings, every job and its score, your applications and their history, people found for outreach, company profiles, pay data you recorded, and your LinkedIn export's connections. For each connection that is name, profile URL, email if shared, company, position, when you connected, message count and latest date. Never message text. |
| `work_history.md`, `templates/` | Your merged work history and the resume templates built from it |
| `backups/` | A database copy at the start of each daily run, and a snapshot before each work-history overwrite |
| `mail-debug/` | Up to 20 job-alert emails that could not be parsed, kept so the parser can be fixed |
| `daily.log`, `daily.launchd.log`, `web.launchd.log` | Logs from the background services, if you install them ([logs](services.md#logs)) |
| **Secrets file** | `paths.secrets_file`, default `<data folder>/secrets.env`. API keys and app passwords. Settings hold only the variable names ([why](settings-and-secrets.md#secret-names-in-settings)). |
| **Output folder** | `paths.output_dir`, default `~/Documents/blastjob` |
| `<company>-<date>/` at the top | Resumes, cover letters, prep packs and notes still to send |
| `Submitted/` | Packets for jobs you applied to |
| `Closed before applying/` | Packets for postings that closed before you applied |
| `Archive/` | Everything else: archived jobs, and old or unlinked folders |

blastjob files the output folders for you as a job changes stage. A folder is moved whole and never copied. How and when it files is in [events and filing](../how-it-works/events-and-filing.md). To put your documents somewhere else, run `uv run blastjob setup set paths.output_dir <path>`.

**On a Mac with iCloud "Desktop & Documents" turned on, `~/Documents/blastjob` syncs to iCloud.** Choose a path outside `~/Documents` if you do not want that.

## What leaves your machine

- Your work history and each job description go to the language model you chose: Anthropic, OpenAI, or the `claude` tool.
- Company names and job titles go to the Jina search service, only if you add a Jina key.
- Discovery calls public job boards, and Adzuna if you enable it, with your search terms.
- The daily digest goes to Discord, only if you turn that on.

Nothing else is sent anywhere, and nothing is ever submitted on your behalf.

## Backups

At the start of every daily run, including `--dry-run`, blastjob copies the database to `backups/blastjob-YYYYMMDD-HHMMSS.db` in the data folder, using SQLite's own backup method, which is safe while the cockpit is open. The `backups/` folder is created owner-only (0700) and each copy is owner-only (0600), whatever the umask; a `backups/` folder that already exists keeps the mode you gave it. The newest 14 copies are kept and older ones are deleted. A run stopped by the `PAUSE` file takes no backup.

Separately, whenever you save your work history on the **Work History** page or ingest resumes, the file being replaced is first copied to `backups/work_history-YYYYMMDD-HHMMSS.md` (ingest adds `-ingest` to the name). Each snapshot is owner-only (0600). These are never deleted by the database rotation, and blastjob never prunes them.

To restore the database:

1. Stop the cockpit and the daily service ([services](services.md)).
2. Delete `blastjob.db-wal` and `blastjob.db-shm` from the data folder if they exist.
3. Copy the backup you want over `blastjob.db`.
4. Start the cockpit again. A copy from an older version is migrated on open.

To restore a work history, copy the snapshot you want over `work_history.md` in the data folder.

## Backing up or removing everything

To back up or remove everything, copy or delete three things: the data folder, the secrets file (if you moved it out of the data folder), and the output folder. For a manual safety copy of the database alone, stop everything and copy `blastjob.db`.

## Upgrading

- **A clone:** run `git pull`, then `uv sync --extra dev`.
- **A fork:** sync it with the original (GitHub's **Sync fork**, or `git fetch upstream && git merge upstream/main`), then run `uv sync --extra dev`.

Database migrations run by themselves the next time the cockpit or the daily run starts, and the daily run takes a backup first. If you installed the launchd services, re-run the installer after an upgrade that changes the templates and restart the cockpit ([after an upgrade](services.md#after-an-upgrade)). Then run `uv run blastjob setup doctor` ([the doctor](doctor.md)).

If you are coming from an older template or a `config.toml`, see [importing an old config](settings-and-secrets.md#importing-an-old-config).

## Repair and backfill commands

These one-off commands belong to `blastjob-daily`. Each has a `-report` form that changes nothing; run that first. This is only a list, and the page for each feature explains what the command does.

| Command | What for | Details |
|---|---|---|
| `--recluster-report`, `--recluster` | Merge postings of the same role that were stored before the matching rules existed | [same role](../how-it-works/same-role.md) |
| `--duplicate-applications` | List kept applications that look like the same role (read only) | [same role](../how-it-works/same-role.md) |
| `--repair-linkedin-titles-report`, `--repair-linkedin-titles` | Split LinkedIn alert rows whose title and company were stored as one string; `--accept` takes job ids you confirm | [discovery](../how-it-works/discovery.md) |
| `--refetch-workday-locations-report`, `--refetch-workday-locations` | Re-read Workday postings that were rejected for location but list more places | [discovery](../how-it-works/discovery.md) |
| `--backfill-artifacts-report`, `--backfill-artifacts` | Index existing packet files and past application events | [events and filing](../how-it-works/events-and-filing.md) |
| `--file-packets-report`, `--file-packets` | Rename old packet documents to the shorter names and file folders by stage | [events and filing](../how-it-works/events-and-filing.md) |
| `--employer-jd-report`, `--employer-jd` | Fetch full descriptions from employers' own postings for short aggregator listings (`--min-score`, `--max-requests`) | [employer data](../how-it-works/employer-data.md) |
| `--closure-report` | Preview posting-closure verdicts | [closure](../how-it-works/closure.md) |

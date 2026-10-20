# The daily run

**What this covers:** What `blastjob-daily` does each morning, what the digest contains, how delivery works, what a run records, the kill switch and the command flags. It is for anyone changing `daily.py`, `daily_digest.py`, `daily_tools.py`, `daily_filing.py` or `notify/`. To schedule it, see [services](../operations/services.md#the-daily-run-service).

## What a run does

`blastjob-daily` calls the same `run_discovery` and `run_matching` the cockpit uses (no HTTP hop), then closure, packet filing, the employer-description steps and the reply scan, and sends ONE digest according to `notify.mode`. A scheduled run is `blastjob-daily` with no arguments, at 07:30 (the launchd template in `launchd/`, through `scripts/run-daily.sh`).

In order:

1. **Kill switch.** If a `PAUSE` file exists in the data dir, the run stops here (see [Kill switch](#kill-switch)).
2. **Backup.** A copy of the database is taken (`backup_db`, 14 kept; see [backups](../operations/data-and-backups.md#backups)). A failed backup is recorded as an error and the run goes on.
3. **Discovery.** See [discovery](discovery.md).
4. **Free employer-description copies.** Local only, before matching so a copied row is scored once on the full text. See [employer data](employer-data.md#in-the-daily-run).
5. **Matching, only for jobs never scored** (`only_unscored=True`). Re-scoring every resolved posting each morning cost over an hour of model time for no new signal. `--no-score` skips this and the re-score in step 8. See [matching](matching.md).
6. **Closure.** Runs after discovery and matching so it sees today's source state. A dry run runs it in shadow mode (see [closure](closure.md#shadow-mode)).
7. **Packet filing** (`daily_filing.filing_pass`). Closure is what moves a kept job to `posting_closed`, so filing comes after it. A dry run only logs what it would do. See [packet filing](events-and-filing.md#packet-filing).
8. **Employer-description lookup and re-score.** Opt-in network step. See [employer data](employer-data.md#in-the-daily-run).
9. **Reply scan.** Surfaces recruiter replies to jobs you applied to. It never drafts or sends. With no mailbox credentials it does nothing, silently. See [recruiter replies](discovery.md#recruiter-replies).
10. **Digest.** Built from what is now in the database, recorded, sent and stamped (see [Digest contents](#digest-contents) and [Delivery](#delivery)).

Every step is fail-soft. An exception rolls back the step's pending work, goes into the run's `errors` under the step's name (`discovery`, `scoring`, `matching`, `closure`, `replies`, `employer_jd` and per-source adapter keys) and the run carries on, because the run must still be recorded and reported. Errors appear under "Source errors" in the digest.

A dry run is not a no-op. `blastjob-daily --dry-run` still takes the backup, still discovers and scores (and so still spends model calls), and those results are saved. What it does not do is send, stamp `notified_at`, record a `daily_runs` row, move jobs on closure, write employer descriptions, stamp the reply ledger or file packets. Because nothing is stamped, the next real run announces everything the dry run printed. The digest is printed after the summary line, under a `---` separator.

## Digest contents

The digest is built by `build_digest`. Sections appear only when they have something to say, in this order:

- **New roles.** Jobs from `pending_jobs`: surfaced or queued, active at the source, never announced (`notified_at` empty). A row whose cluster holds a kept or archived member is skipped, because the cockpit shows that cluster in the decided member's stage or hides it. That is how a repost of an application you already made stays quiet.
  - Best score first, except that roles with someone you know at the company (marked "you know someone here") go ahead of higher-scoring cold roles. The sort is stable, so each group keeps its score order. See [your network](network.md#alumni-signal).
  - At most 25 are shown (`MAX_DIGEST_JOBS`). If there are more, a line points at the cockpit's All page.
  - Each line has the score, company, title, "(review)" for queued roles, location or "Remote", a comp badge, the posting link, then a cockpit link. The comp badge never calls a figure posted unless the employer posted it: it reads "posted", "inferred (from the posting)", "est." or "unverified", and "comp n/a" when there is none.
  - The posting link comes before the cockpit link because the digest is usually read on a phone, where the cockpit's `127.0.0.1` address is the wrong machine. The posting link is the cluster's best apply link (`apply_link.apply_links_for`; see [same role](same-role.md#one-application-per-role)), else the row's own URL.
  - The cockpit links use the base from `cockpit_base`: `web.public_url` when set, else `http://127.0.0.1:<web.port>`. `web.public_url` is checked against the same Host rule the cockpit enforces (loopback or a private IP address, never a DNS name); see [LAN access](../operations/lan-access.md#digest-links-on-a-phone).
- **Closed postings.** One line naming up to five kept jobs whose posting closed this run.
- **Replies.** One entry per surfaced reply: who, about what, a one-line snippet and a cockpit link.
- **Follow-ups due.** Applied-stage jobs with a follow-up due, most overdue first.
- **Source errors.** One line per failing source or step, cut to 160 characters.
- **Notes.** Things worth a human's attention that are not failures: a snapshot the stale-marking guard refused to act on, or an employer-description summary. Cut to 200 characters each.
- **Weekly heartbeat.** On Mondays (`HEARTBEAT_WEEKDAY`): active and kept counts, plus the last seven days' runs, fetched, scored, new roles and average duration.
- **Footer.** An italic line with the run duration, the top five sources by fetch count and scoring throughput and cost (`cost unknown` when the provider reported no usage). It is added only when the digest is actually sent.

**Quiet days send nothing.** With no roles, errors, follow-ups, notes, closures, replies or heartbeat, the digest is empty and nothing is posted. The weekly heartbeat exists so that silence stays distinguishable from a dead job.

## Delivery

`notify.mode` decides where the digest goes (`sender_for`):

| Mode | Behaviour |
|---|---|
| `stdout` (default) | Prints the digest. Under launchd it lands in the daily log. |
| `discord` | Posts to a private channel through a bot. |
| `off` | Sends nothing. |

A dry run never sends, whatever the mode. Only `discord` needs credentials, so a `stdout` or `off` install never fails over missing Discord keys. The bot token and channel id are read from the environment variables named by `notify.discord_token_env` and `notify.discord_channel_env` (defaults `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID`), which come from the secrets file (see [settings and secrets](../operations/settings-and-secrets.md)). In `discord` mode a missing one stops the run with a clear error.

Discord delivery is plain REST (`notify/discord.py`): no gateway, no discord library. The digest opens with a header line (the target emoji and "blastjob"), so one channel can be shared with another notifier and the sources told apart by header. The text is split on line boundaries into messages of at most 1,900 characters, and every non-2xx response raises, after up to three attempts. A 429 waits for the server's `Retry-After` (or Discord's `retry_after`), capped at 30 seconds, before the next attempt. Those waits share a budget of 120 seconds per host per process; once it is spent, a 429 falls back to the short backoff so a hostile or stuck server cannot stall a run; other retryable failures wait 1 then 2 seconds.

**Delivery is at-least-once, by design.** The order is: record the run (`sent=0`), send, stamp `notified_at`, mark the run `sent=1`.

- `notified_at` is stamped only after the channel accepted the whole digest, so a failed post is retried on the next run.
- It is stamped only on the jobs that were actually rendered. Anything past the 25-role cut stays unannounced and leads the next morning's digest instead of being buried. `rendered_jobs` is shared by the builder and the stamper so the two cannot disagree.
- A crash between the channel's 2xx and the stamp re-sends the digest once, and a digest that fails partway through its chunks re-sends the chunks already posted. For a personal channel a rare duplicate beats a lost role, and an outbox would be more machinery than the problem deserves.

## Run records

Every run that is neither dry nor paused writes one `daily_runs` row, before the send: start and finish times, duration, `fetched` and `per_source` counts, `scored`, scoring batches and seconds, `new_jobs`, `followups`, `errors` (JSON) and `sent`. `sent` is `0` when the row is written and becomes `1` only after a successful send. A quiet day records a row that stays `0`, because there was nothing to send.

`blastjob-daily --status` prints the last row and sends nothing:

- If the last run recorded a `discovery` or `matching` error, the output leads with `LAST RUN FAILED` and the detail, because that run is not a healthy baseline.
- If the last run was not today, it says how many days ago.
- It shows timing, fetch mix, scoring throughput, new and follow-up counts, `sent` and any errors.
- It cannot show a past run's backup path, which is not stored. Only the summary line printed at the end of a live run names it.
- While a `PAUSE` file exists it says "paused since" the file's modification time, rather than showing a stale success as if it were current.

Every run also prints one summary line (fetched, scored, new, follow-ups, errors, sent, duration, backup) and logs each step. Under the launchd wrapper the output is appended to `daily.log` in the data dir, bracketed by start and finish lines with the exit code; see [logs](../operations/services.md#logs).

The hand-run diagnostic flags (`--closure-report`, `--sweep-search-firms`, the backfills) write no `daily_runs` row and send nothing.

## Kill switch

Create a file named `PAUSE` in the data dir (`BLASTJOB_DATA_DIR`, default `~/.blastjob`) and the daily run exits immediately:

```bash
touch "${BLASTJOB_DATA_DIR:-$HOME/.blastjob}/PAUSE"
```

The check comes before the backup and everything else. The run prints "paused (PAUSE file present)", writes no `daily_runs` row and sends nothing. Delete the file to resume. The hand-run flags other than `--status` are not blocked by it.

## Commands

A scheduled run is plain `blastjob-daily`. Flags are checked in this order and the first match runs and exits: `--status`, `--recluster-report` or `--recluster`, the maintenance flags in the table below, `--closure-report`, `--sweep-search-firms`. Otherwise the daily run starts.

| Flag | Effect |
|---|---|
| `--dry-run` | Runs the pipeline and prints the digest. Sends and stamps nothing; see [What a run does](#what-a-run-does) for what it still writes. |
| `--no-score` | Discover only: skips model scoring and the employer-description re-score. |
| `--status` | Prints the last recorded run. See [Run records](#run-records). |
| `--closure-report` | Shadow closure run over kept jobs. See [closure](closure.md#shadow-mode). |
| `--sweep-search-firms [--no-score]` | On-demand search-firm run. See [discovery](discovery.md#retained-search-firms). |
| `--recluster-report`, `--recluster` | Same-role merge backfill. See [same role](same-role.md#auto-cluster). |
| `--duplicate-applications` | Read-only list of likely duplicate applications. See [same role](same-role.md#one-application-per-role). |
| `--repair-linkedin-titles-report`, `--repair-linkedin-titles`, `--accept` | Whole-card LinkedIn title repair. See [discovery](discovery.md#linkedin-whole-card-layout). |
| `--refetch-workday-locations-report`, `--refetch-workday-locations` | Re-read geo-rejected Workday rows. See [discovery](discovery.md#workday). |
| `--backfill-artifacts-report`, `--backfill-artifacts` | Index packet files and past events. See [events and filing](events-and-filing.md#backfill). |
| `--file-packets-report`, `--file-packets` | Rename and file packets. See [events and filing](events-and-filing.md#document-renames). |
| `--employer-jd-report`, `--employer-jd`, `--min-score`, `--max-requests` | Employer-description backfill. See [employer data](employer-data.md#commands). |

Each `-report` form writes nothing. `daily_tools.py` holds the maintenance flags so `daily.py` stays about the daily run.

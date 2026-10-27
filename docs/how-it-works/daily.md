# The daily run

**What this covers:** What `blastjob-daily` does each morning, what the digest contains, how delivery works, what a run records, the kill switch and the command flags. It is for anyone changing `daily.py`, `daily_digest.py`, `daily_tools.py`, `daily_filing.py` or `notify/`. To schedule it, see [services](../operations/services.md#the-daily-run-service).

## What a run does

`blastjob-daily` calls the same `run_discovery` and `run_matching` the cockpit uses (no HTTP hop), then closure, packet filing, the employer-description steps, the reply scan and the target-company news step, and sends ONE digest according to `notify.mode`. A scheduled run is `blastjob-daily` with no arguments, at 07:30 (the launchd template in `launchd/`, through `scripts/run-daily.sh`).

In order:

1. **Kill switch.** If a `PAUSE` file exists in the data dir, the run stops here (see [Kill switch](#kill-switch)).
   **Run lock.** The run then takes the run lock for its whole life; if another run holds it, this one records `lock_held` and exits (see [One run at a time](#one-run-at-a-time-and-the-deadline)).
2. **Backup.** A copy of the database is taken (`backup_db`, 14 kept; see [backups](../operations/data-and-backups.md#backups)). A failed backup is recorded as an error and the run goes on.
3. **Discovery.** See [discovery](discovery.md).
   **Category searches.** On the search-firm weekday only, and only when a category is enabled: the weekly role searches for the owner's categories, which run before matching so their finds are scored the same day. Errors and cap notes join the run's `errors` and `notes`; a run cut by the deadline names it in the skipped steps. See [category searches](discovery.md#category-searches).
4. **Free employer-description copies.** Local only, before matching so a copied row is scored once on the full text. See [employer data](employer-data.md#in-the-daily-run).
5. **Matching, only for jobs never scored** (`only_unscored=True`). Re-scoring every resolved posting each morning cost over an hour of model time for no new signal. `--no-score` skips this and the re-score in step 8. See [matching](matching.md).
6. **Closure.** Runs after discovery and matching so it sees today's source state. A dry run runs it in shadow mode (see [closure](closure.md#shadow-mode)).
7. **Packet filing** (`daily_filing.filing_pass`). Closure is what moves a kept job to `posting_closed`, so filing comes after it. A dry run only logs what it would do. See [packet filing](events-and-filing.md#packet-filing).
8. **Employer-description lookup and re-score.** Opt-in network step. See [employer data](employer-data.md#in-the-daily-run).
9. **Reply scan.** Surfaces recruiter replies to jobs you applied to. It never drafts or sends. With no mailbox credentials it does nothing, silently. See [recruiter replies](discovery.md#recruiter-replies).
10. **Target news.** Scans the core target companies that are due, at most 8 a run, and records failures. See [Target news](#target-news).
11. **Digest.** Built from what is now in the database, recorded, sent and stamped (see [Digest contents](#digest-contents) and [Delivery](#delivery)).

Between steps the run checks its deadline and stops starting work once it has passed (see [One run at a time](#one-run-at-a-time-and-the-deadline)).

Every step is fail-soft. An exception rolls back the step's pending work, goes into the run's `errors` under the step's name (`discovery`, `scoring`, `matching`, `closure`, `replies`, `employer_jd`, `target_watch` and per-source adapter keys) and the run carries on, because the run must still be recorded and reported. Errors appear under "Source errors" in the digest.

A dry run is not a no-op. `blastjob-daily --dry-run` still takes the backup, still discovers and scores (and so still spends model calls), and those results are saved. What it does not do is send, stamp `notified_at`, record a `daily_runs` row, move jobs on closure, write employer descriptions, stamp the reply ledger, file packets or store target news. The target news step still searches, reads pages and calls the model (so it spends), but takes no lease and writes no event or scan status. Because nothing is stamped, the next real run announces everything the dry run printed. The digest is printed after the summary line, under a `---` separator.

## One run at a time, and the deadline

**The run lock.** `core/run_lock.py` takes an `flock` on `<data_dir>/run.lock`, non-blocking: a second taker fails at once and never queues. The kernel drops the lock when its holder dies, even by SIGKILL, so a crash cannot leave a stale lock, and nothing matches a process by name. After locking, the holder writes its PID, what it is doing and when it began into the file; that text only feeds the message ("Another run is in progress (daily run, started 07:30)"). The lock is close-on-exec, so a spawned model child never inherits it. The file is opened with `O_NOFOLLOW`, so a symlink planted at that path is refused. If the lock cannot be taken for any reason other than contention (for example `ENOLCK`, or a filesystem without `flock`), that is a refusal too: the daily run fails closed and records `lock_held` with the reason, and a command prints it. The holder text read back from the file is capped at 80 characters and stripped of control characters before it reaches a log, page or digest.

`run_discovery` and `run_matching` take the lock themselves, so no caller can forget. It is reentrant inside one context, which lets `run_daily` hold one lock across both while they also take it; a separate cockpit task, thread or process has no ownership token and contends. Who takes it:

- the daily run, for the whole run (including the digest and its stamps);
- the cockpit's Find and score, Re-score changed and Re-score all, inside the task and for its life. A refused run shows "failed" with the message above, nothing queued;
- hand-run commands that discover, match or re-score (`--sweep-search-firms`, the Workday location fix, the employer-description backfill). The two that write before they re-score take the lock before their first write. They print the message and exit 1.

Passes scoped to named jobs by a cockpit click (a pasted description, a manual job, an employer-description or split re-score) pass `exclusive=False` and do not take it, so a running daily pass never blocks them. `run_matching` refuses `exclusive=False` without `job_ids`, so a full pass can never run unlocked. `tests/test_run_lock_callers.py` parses `src/` and fails if any other caller can skip the lock. The ownership token is cleared when the lock is released, so a task that outlives the block cannot borrow it.

**The deadline.** `limits.daily_deadline_minutes` (default 45, 5 to 240; about six times the longest measured run) bounds the daily run. `run_daily` wraps its steps in `gateway.deadline_scope`, so every model call inside is capped by the time left and none starts with under 30 s left ([model gateway](llm.md#the-model-gateway)). It also checks between steps and stops starting new ones. A call already in flight finishes or hits its own bound. Because fail-soft callers swallow a refused call, the run reads the scope's `DeadlineState` afterwards: if any call was refused, or a step was not started, the run stopped at the limit.

A clicked cockpit full pass (Find and score, Re-score changed, Re-score all) runs under the same `limits.daily_deadline_minutes`, so it can never hold the run lock indefinitely and make the next daily run skip. A clicked pass that hits the limit says so in its log. In the scorer, once the deadline has refused a call there are no more attempts: no retry, no later batch, no per-job fallback, and a refused batch is not counted as a failed one.

**Stopped reasons.** `daily_runs.stopped_reason` is `''` for a run that finished, `deadline`, `lock_held` or `budget` (the call budget, [below](#the-call-budget)).

- `deadline`: the run is recorded with the reason, never as complete. A cut-off run always sends its digest, even on a day with nothing new, because the owner wants to know when a run was cut off (pinned by a test). The digest leads with "Stopped at the 45-minute limit; up to N jobs left for tomorrow". N counts never-scored jobs and is an upper bound: a job the geo check or the title prefilter will turn away also has no score. When there are none, it says "the remaining steps run tomorrow". After the cutoff the run still does the two cheap, local steps that make no model call: the reply scan (recruiter replies are the top channel) and packet filing, and then the digest. Every model or network step waits for the next run (discovery, employer-description copies, scoring, closure checks, the employer-description lookup, target scans), and the digest line ends "Not run (time limit): ..." naming each one, so a skipped step is never shown as done. `--status` shows the same line (stored in `daily_runs.stopped_note`).

**A refused call is not a failure.** Where the deadline refuses a model call inside a step, the item is "not run": a target scan counts no failure, sets no backoff, keeps its last status and scan time, releases its lease and stays due (`RunSummary.not_run`); a kept-row re-score is not counted failed and is not stamped as attempted, so it keeps its place in the stale-kept rotation (`RescoreCounts.not_run`). Both are named in the "Not run" list. The code is in `daily_steps.py`; `daily.py` keeps the frame (lock, deadline scope, digest). A refused scoring batch is not reported as a failed batch. The jobs left unscored are picked up by the next run, since matching takes only never-scored jobs.

### The call budget

The daily run's model calls are tagged `scheduled` (`run_daily` and every hand-run `blastjob-daily` command set the origin) and counted against `limits.scheduled_calls_per_day` (default 150, 1 to 5000) in the call ledger ([llm](llm.md#the-call-ledger-and-budget)). Each step names itself in the ledger: "daily scoring", "daily employer description", "daily target scans".

When a call is refused for the cap, the run stops like a deadline cut, with `stopped_reason` `budget`:

- The refusal marks the same scope flag the deadline does, and every "not run" rule above applies to it unchanged: a target scan is not a failure, a kept-row re-score is not stamped as attempted, the scorer makes no further attempt, and a refused batch is not a failed batch.
- Steps that had not started are named in the digest line: "Stopped at the daily limit of 150 scheduled model calls; up to N jobs left for the next run. Not run (daily call limit): scoring, closure checks, ...". `--status` leads with "STOPPED EARLY: the model call budget was reached". It is never shown as a complete run.
- A budget-stopped run always sends its digest, like a deadline cut, because the owner wants to know.
- The cap is per local day and shared by every scheduled caller, so a run that starts late in the day after hand-run commands may stop earlier than usual. The next day starts from zero.

**Model calls yesterday.** A digest that is sent anyway ends with one footer line: "Model calls yesterday: N scheduled, M clicked (est. $X)". The cost adds up only calls with a known cost; when a call's cost was not reported it says "est. $X known, N unknown", or "unknown" if none was reported. The line is added after the quiet-day check, so it can never make a quiet day send. A paused run sends nothing, so it carries no line.
- `lock_held`: nothing runs and nothing is sent, so a skip does not spam; the next run's digest carries everything. It is recorded (`--status` shows it, and the summary line says "daily run skipped") so a skipped morning is not invisible. The next digest that goes out anyway ends with "N earlier runs skipped: another run held the run lock" in its footer; a skip never makes a quiet day send. Skipped runs are left out of the weekly heartbeat totals and out of the funnel page's run list, where a skip would read as a zero-fetch day.

## Digest contents

The digest is built by `build_digest`. Sections appear only when they have something to say, in this order:

- **New roles.** Jobs from `pending_jobs`: surfaced or queued, active at the source, never announced (`notified_at` empty), and not `dream_far` (those wait for the Dream list). A row whose cluster holds a kept or archived member is skipped, because the cockpit shows that cluster in the decided member's stage or hides it. That is how a repost of an application you already made stays quiet.
  - Best score first, except that roles with someone you know at the company (marked "you know someone here") go ahead of higher-scoring cold roles. The sort is stable, so each group keeps its score order. See [your network](network.md#alumni-signal).
  - At most 25 are shown (`MAX_DIGEST_JOBS`). If there are more, a line points at the cockpit's All page.
  - Each line has the score, company, title, "(review)" for queued roles, location or "Remote", a comp badge, the posting link, then a cockpit link. The comp badge never calls a figure posted unless the employer posted it: it reads "posted", "inferred (from the posting)", "est." or "unverified", and "comp n/a" when there is none.
  - The posting link comes before the cockpit link because the digest is usually read on a phone, where the cockpit's `127.0.0.1` address is the wrong machine. The posting link is the cluster's best apply link (`apply_link.apply_links_for`; see [same role](same-role.md#one-application-per-role)), else the row's own URL.
  - The cockpit links use the base from `cockpit_base`: `web.public_url` when set, else `http://127.0.0.1:<web.port>`. `web.public_url` is checked against the same Host rule the cockpit enforces (loopback or a private IP address, never a DNS name); see [LAN access](../operations/lan-access.md#digest-links-on-a-phone).
- **Dream list.** On the search-firm weekday only (see [below](#dream-list)): far on-site roles at dream companies, and far roles found by a dream category.
- **Closed postings.** One line naming up to five kept jobs whose posting closed this run.
- **Replies.** One entry per surfaced reply: who, about what, a one-line snippet and a cockpit link.
- **Target news.** Events found for your target companies since the last digest (see [below](#target-news)).
- **Follow-ups due.** Applied-stage jobs with a follow-up due, most overdue first (see [below](#quiet-applications)).
- **Asks to follow up.** Waiting outreach asks whose follow-up date has come, right after Follow-ups due (see [below](#asks-to-follow-up)).
- **Recruiters to touch.** Recruiters you have spoken with whose last touch is old, right after Asks to follow up (see [below](#recruiters-to-touch)).
- **Last week.** The weekly pace line (see [below](#weekly-pace-line)).
- **Source errors.** One line per failing source or step, cut to 160 characters.
- **Notes.** Things worth a human's attention that are not failures: a snapshot the stale-marking guard refused to act on, or an employer-description summary. Cut to 200 characters each.
- **Weekly heartbeat.** On Mondays (`HEARTBEAT_WEEKDAY`): active and kept counts, plus the last seven days' runs, fetched, scored, new roles and average duration.
- **Footer.** An italic line with the run duration, the top five sources by fetch count and scoring throughput and cost (`cost unknown` when the provider reported no usage). The cost covers matching, the employer-description re-score pass of step 8 and the target news model call of step 10; it is unknown if any call in any was. It is added only when the digest is actually sent.

### Target news

`daily_targets.targets_step` runs after the reply scan, so the digest sees its result. It scans the due core targets (see [the target news watch](employer-data.md#target-company-news-watch)) and records `target_failed` and the model cost on the run result. The cost joins the run total in the footer.

The block is built from `company_events` rows that are not dismissed and have `notified_at` empty. Each entry shows the company, the event type, "published <date>", the source basis and host, the verified sentence (stripped of links, markup and mention characters, cut to 160 characters), how many people you know there and a cockpit link. At most 8 are shown (`MAX_TARGET_NEWS`), with an "and N more" line. Delivery follows the job rule: each event is stamped `notified_at` only for the ones rendered, and only after the send succeeded, so a failed send or the overflow leads the next digest. When scans failed in this run, one line says "N target scans failed" rather than nothing. A day with only target news is not a quiet day.

### Dream list

`daily_dream.weekly_jobs` returns the roles for the section, and none on any other weekday (`due_today` with `discovery.search_firm_weekday`; there is no setting of its own). `pending_dream_jobs` applies the same rules as `pending_jobs` (active at the source, `review_state` surfaced or queued, no kept or archived cluster member), plus `dream_far=1`, the same exception rule as matching (`DreamRule`: current dream membership, a configured board with an identity basis), a score at or above `alignment_threshold` and `dream_notified_at` empty. A role that was archived, went stale, was turned away by the comp gate or has a decided cluster is therefore never announced. Best score first, at most 15 (`MAX_DREAM_JOBS`) with an "and N more" line.

Delivery state is `jobs.dream_notified_at` (migration 60), separate from `notified_at`. The renderer's slice (`rendered_dream_jobs`) is exactly what gets stamped, and only after `send` returned, so a failed send stamps nothing and overflow leads the next week. The send is one call to the sender; if Discord accepts early chunks of a long message and fails a later one, the whole section is re-sent, so a duplicate is possible and accepted, as for the job digest. A dry run stamps nothing. A day with only a Dream list is not a quiet day. Dream roles that are remote or near are not `dream_far` and stay in the daily list with their badge.

A role in the section that a dream category found names the category under its line (`Dream: <label>`, defanged for Discord); `pending_dream_jobs` selects the label with a subquery over the current hits.

### Asks to follow up

An outreach ask (`outreach_asks`, migration 65; see [network](network.md#outreach-asks)) is due when `status` is `waiting`, `follow_up_on` is set and not after today, and `follow_up_notified_on` differs from `follow_up_on`. `outreach_asks.rendered_due_followups` returns at most 10, most overdue first, then job score (NULL last), then id. That one list is both rendered and stamped: after `send` returned, each row gets `follow_up_notified_on = follow_up_on` by compare-and-set on the date seen at selection, so a date moved in between stays armed. A failed send and a dry run stamp nothing. Moving the follow-up date re-arms the reminder; a waiting ask with no date is never due. A line names the person, the kind, the role and company with days overdue, and links the job page; an ask not about one job shows the person's company from the network data and links `/network`. Names and titles are defanged for Discord. A day with only a due ask still sends.

### Recruiters to touch

`recruiter_status.due_reminders` (see [network](network.md#recruiter-status)) selects recruiters whose status, stored or automatic, is `spoke` or `active`, whose last touch is at least `notify.recruiter_touch_days` old (default 42, 14 to 120), and whose `recruiter_reminded_for` differs from that last-touch date. A `spoke` recruiter must also have a last touch within `notify.recruiter_lookback_days` (default 365, 90 to 1,095): the reminder covers recruiters touched in the last year, plus any you mark active at any age. This keeps the roughly 60 recruiters with old message history from flooding the first digest. Do not contact and not contacted are never included, nor is a recruiter with no last touch date. Warmest first, at most 5, each line naming the person, the firm and the days since the last touch with a link to `/network`. An unanswered ask is not a reminder here: its follow-up date belongs to [asks to follow up](#asks-to-follow-up).

The rendered list carries `(contact id, last touch)` pairs, and after `send` returned each row gets `recruiter_reminded_for` set to exactly that date. It is not compare-and-set: a touch logged while the digest was being sent has a newer last touch, which no longer equals the stamp, so the next cycle re-arms on its own. A new ask or message re-arms the reminder the same way. A failed send stamps nothing, whether the sender raised at once or a Discord message of several parts failed on a later part; a dry run stamps nothing. A day with only a due recruiter still sends.

### Recruiter mail

`mail_triage.pending_ids` selects messages with `status='new'` and no `notified_at` (see [discovery](discovery.md#recruiter-mail-from-one-label)). The digest carries one line, the count and a link to `/recruiter-mail`: no sender name or subject, because they are untrusted text and the channel is shared. After `send` returned, the rows that were counted get `notified_at` by compare-and-set (`WHERE notified_at IS NULL`); a failed send and a dry run stamp nothing, and a row decided before the digest is not counted. The count is news by itself: a day whose only item is recruiter mail still sends. A label missing on the server shows as a note, and a failed read as a source error. A message the reader gave up on after three failed runs shows as a note ("1 recruiter-label message couldn't be read; open it in your mail"), stamped after the send like the count.

### Quiet applications

`web/stage_lists.followup_due` decides the Follow-ups due block, the Applied page flags and the job page reason, so the three always agree. It is unstamped by design: a due job repeats daily while it stays due. An explicit `next_action_due` always wins: due once it has arrived, and nothing before. Without one:

- A `screen` or `interview` job is due once it has been quiet for `notify.quiet_after_days` (default 21, 7 to 90): "no update in N days: follow up, or mark it ghosted".
- An `applied` job keeps its 7-day reason ("applied N days ago, no update logged"). At or past the quiet threshold the reason adds ", or mark it ghosted".
- Quiet is measured from the later of `jobs.stage_changed_at` and the newest `application_events.at` (the `latest_event_at` the Applied rows carry). Backdated events older than the stage change do not reset it, a same-day or future date is not quiet, and a malformed date is skipped.
- Silence is not proof, so nothing is ever marked ghosted automatically; choosing ghosted on the job page writes the `ghosted` event. The order is unchanged (`followups_due`).

### Weekly pace line

On the first run on or after `notify.pace_weekday` (default `mon`, or `off`) the digest carries one line for the previous full ISO week, Monday to Sunday (`daily_pace.py`): `applied` events, `asked_on` dates in `outreach_asks` and events whose kind is in `channel_counts.REPLIED_EVENT_KINDS`. Those are events, not distinct applications, and the line says "reply events". Plain counts: no targets and no rates. The stamp is the reported ISO year-week in `digest_stamps` (migration 69, `name='pace'`), written after a send that carried the line, so a delayed run still reports the right week and no week is reported twice. A failed send and a dry run stamp nothing; with `notify.mode` set to `off` the stamp is written after the no-op send, like the other blocks. The line is something to say only on its weekday: a later run in the week adds it to a digest that goes out anyway but never sends one for it alone.

**Quiet days send nothing.** With no roles, Dream list, errors, follow-ups, asks to follow up, recruiters to touch, the pace line on its day, notes, closures, replies, target news or heartbeat, the digest is empty and nothing is posted. The weekly heartbeat exists so that silence stays distinguishable from a dead job.

## Delivery

`notify.mode` decides where the digest goes (`sender_for`):

| Mode | Behaviour |
|---|---|
| `stdout` (default) | Prints the digest. Under launchd it lands in the daily log. |
| `discord` | Posts to a private channel through a bot. |
| `off` | Sends nothing. It still stamps what the digest would have carried, like the other blocks, so switching notifications back on does not dump a backlog. |

A dry run never sends, whatever the mode. Only `discord` needs credentials, so a `stdout` or `off` install never fails over missing Discord keys. The bot token and channel id are read from the environment variables named by `notify.discord_token_env` and `notify.discord_channel_env` (defaults `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID`), which come from the secrets file (see [settings and secrets](../operations/settings-and-secrets.md)). In `discord` mode a missing one stops the run with a clear error.

Discord delivery is plain REST (`notify/discord.py`): no gateway, no discord library. The digest opens with a header line (the target emoji and "blastjob"), so one channel can be shared with another notifier and the sources told apart by header. The text is split on line boundaries into messages of at most 1,900 characters, and every non-2xx response raises, after up to three attempts. A 429 waits for the server's `Retry-After` (or Discord's `retry_after`), capped at 30 seconds, before the next attempt. Those waits share a budget of 120 seconds per host per process; once it is spent, a 429 falls back to the short backoff so a hostile or stuck server cannot stall a run; other retryable failures wait 1 then 2 seconds.

**Delivery is at-least-once, by design.** The order is: record the run (`sent=0`), send, stamp `notified_at`, mark the run `sent=1`.

- `notified_at` is stamped only after the channel accepted the whole digest, so a failed post is retried on the next run.
- It is stamped only on the jobs that were actually rendered. Anything past the 25-role cut stays unannounced and leads the next morning's digest instead of being buried. `rendered_jobs` is shared by the builder and the stamper so the two cannot disagree.
- A crash between the channel's 2xx and the stamp re-sends the digest once, and a digest that fails partway through its chunks re-sends the chunks already posted. For a personal channel a rare duplicate beats a lost role, and an outbox would be more machinery than the problem deserves.

## Run records

Every run that is neither dry nor paused (including one skipped for the run lock) writes one `daily_runs` row, before the send: start and finish times, duration, `fetched` and `per_source` counts, `scored`, scoring batches and seconds, `new_jobs`, `followups`, `errors` (JSON), `sent` and `stopped_reason`. `sent` is `0` when the row is written and becomes `1` only after a successful send. A quiet day records a row that stays `0`, because there was nothing to send.

`blastjob-daily --status` prints the last row and sends nothing:

- If the last run recorded a `discovery` or `matching` error, the output leads with `LAST RUN FAILED` and the detail, because that run is not a healthy baseline.
- If the last run stopped early, it says so next: "STOPPED EARLY" (deadline or budget) or "SKIPPED" (the run lock was held).
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

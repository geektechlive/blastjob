# Events, artifacts and packet filing

**What this covers:** The application timeline, the artifact index, the job page's activity and files, the output layout, how stored paths stay valid, and how the output folder files itself. For anyone touching events, artifacts, paths or packet folders.

## Application events

`application_events` (migration 42) records what happened in an application, and when. A row has a `kind` from `events.EVENT_KINDS` (applied, screen, interview, thank_you, follow_up, recruiter_reply, offer, rejected, withdrawn, ghosted, posting_closed, filled, note), an ISO date or datetime `at`, a `note`, a `ref` (a Message-ID or a link), a `source` and `created_at`. `events.insert_event` validates the kind and the source and raises on an unknown one.

### Who writes events

| Source | Writer |
|---|---|
| `state` | `service.set_application_state`, in the same transaction as the state change |
| `closure` | `closure.close_transition`, `closure.sweep_filled` and `closure.reopen_transition` |
| `replies` | the recruiter-reply scanner; `ref` is the Message-ID |
| `mail_label` | linking a message on the Recruiter mail page to a job; `ref` is `label:<uidvalidity>:<uid>` and the event is removed again if the link changes (see [discovery](discovery.md#recruiter-mail-from-one-label)) |
| `backfill` | the history backfill (see [Backfill](#backfill)) |
| `same_role` | `same_role.reconcile_applications`: a `note` on the surviving application and one on each duplicate it retires, in the merge's transaction (see [same-role](same-role.md)) |
| `manual` | you, from the job page |

`events.insert_event` never commits; `events.add_event` commits. The first form is how a writer joins its own transaction.

### The invariants

- **A state and its event commit together or not at all.** Every write to `application_state` stamps `stage_changed_at` and records its event in the same transaction. If the event cannot be written, the move is rolled back and the error is raised.
- **Moving to the state a job already holds is not a change.** `service.set_application_state` returns False and writes nothing, so re-picking a dropdown value never re-records a send or an outcome.
- **`drafted` and the empty state write no event.** Drafting is bookkeeping about the packet, not something that happened.
- **`applied` is recorded only by the move that stamps `applied_at`.** Going back to applied later (Reopen from Closed) writes a `note` event, "Back to applied (was ...)", and a closure reopen writes "Posting reopened; back to ...". An "Applied" dated today would claim an application that never happened.
- **Only manual events can be deleted**, and only on the job they belong to: `events.delete_manual_event` runs `DELETE ... WHERE id=? AND job_id=? AND source='manual'`. Automated history is not editable.
- **Newest first means by day, then by insertion order.** The order is `substr(at, 1, 10) DESC, id DESC`. Manual entries carry a bare date and automated ones a timestamp, so comparing the full strings would put a thank-you logged today below this morning's automated event.

`events.latest_event` takes a list of job ids and returns the newest event for a whole list in one query; list rows use it.

## Artifact index

`artifacts` (migration 43) is an index of the files written for a job, one row per absolute path. Two output layouts exist on disk, and the current one shares a flat `{company-slug}-{date}/` folder across every role tailored for that company that day. So "the files for this job" is never a glob of a folder. Once a file is registered, its location is a recorded path, not a recomputed one.

`artifacts.register` is an idempotent upsert by path. A re-register refreshes `sha256`, `bytes` and `updated_at`, and fills `job_id` and `build_run_id` only while they are still NULL, so an artifact is never reassigned to another job or run. A missing file raises, and so does an unknown kind (`artifacts.ARTIFACT_KINDS`). `artifacts.kind_for` derives the kind from the filename the build wrote.

`artifacts.role_files` is the only rule for "this role's files in a packet folder". It returns:

- the role's `{role-slug}.*` sidecars,
- the resume and cover-letter documents for exactly that company and role token pair, including the `_ATS` export, refine's `.vN` rotations and anything in `quarantine/`,
- and `*.followup-{job_id}.md` drafts for this job id.

A legacy per-role folder (one with a bare `metadata.json`) belongs to its one role whole. A shared company-day folder is never taken whole. The match is on the exact token pair, so `_VP_IT` does not swallow `_VP_IT_and_Data`.

Registration from the write hooks is best-effort (`artifacts.register_paths`): a failure logs a warning and never changes what was written or the build's status. The hooks are:

- **Tailor** (`web/build.py`, after `client.finish_build`). It also writes `{role-slug}.apply_link.html` through `web/apply_file.py`, a one-link page to the cluster's best apply route from `apply_link.apply_links_for`. With no link, there is no file.
- **Refine** (`web/refine.py`). The route passes the `job_id`, and the files are re-scanned with `role_files`, not with refine's own newest-by-modification-time pick.
- **Follow-up draft** and **prep pack** (`save_prep_pack` in `core/prep.py`).

## Activity and files on the job page

`web/templates/_activity_files.html` lazy-loads `GET /jobs/{id}/activity` (`web/routes_activity.py`), so the job page route itself is untouched. The panel shows the job's events newest first, an add form, and the Files list.

- Add: `POST /jobs/{id}/events`. The kind must be one of `EVENT_KINDS`, the date must be ISO, and the note is capped at 2000 characters. Delete: `POST /jobs/{id}/events/{event_id}/delete`, manual events only. Both are checked for the CSRF token and a local Origin (see [security model](web.md#security-model)).
- The panel header says dates before a fixed cutoff were reconstructed from the record. That is one panel-wide label, not a per-event flag; events written by the backfill carry only `source='backfill'`.
- **A file is a link only when the download route would serve exactly that file.** `downloads.is_downloadable` asks `downloads.resolve_download`, the same resolver `GET /jobs/{id}/files/{name}` uses. When it would serve a different file (a same-named file in another or newer packet folder), or none, the path shows as text instead. There is no separate file-serving route for the panel, on purpose.

## Output layout

The output folder is `paths.output_dir` (default `~/Documents/blastjob/`). A tailored packet lands in one folder per company per day, `{company-slug}-{date}/`, shared by every role tailored for that company that day. Files inside carry the role in their name so roles do not collide: the role's sidecars are `{role-slug}.job_description.md`, `.fit_score.json`, `.metadata.json`, `.packet_report.json` and so on, where `paths.resolve_role_slug` keeps two titles that slugify alike from sharing a slug.

`paths.application_dir` picks the folder. A build always lands at the root of the output folder. It reuses a root `{company-slug}-{today}` folder, but if that name is already filed in a bucket (you rebuilt a submitted job today) it takes the next free `-2`, `-3` and so on, so a new packet never merges into a filed one. The post-build hook then files it by the job's state.

The interview prep pack is written role-prefixed as `{role-slug}.interview_prep.md` into the folder of the job's newest successful build, read from `build_runs.artifact_dir`. Only when the job has no build, or that folder is gone, does it fall back to today's folder. The pack streams for minutes and the packet may be filed meanwhile, so the folder is located at write time under the filing lock.

The top level of the output folder holds only packets still to send. The rest are filed whole into `Submitted/`, `Closed before applying/` and `Archive/`. A folder is moved, never copied.

## Stored paths

**No database path is ever rewritten when a packet is filed.** Instead:

- Every path written to the database goes through `paths.canonical`, which returns the ROOT form: home-expanded, with a bucket segment directly under the output folder removed. The writers are `artifacts.stored_path` (used by `artifacts.register`), the web build before it calls `client.finish_build`, `packet_sends.record_send` and `company._profile_data` for `workup_path`.
- Every reader resolves a stored path with `paths.locate`, which returns where it is now. It tries the root and then each bucket and returns the first place where the full path exists. If none does, it returns the first place the entry folder exists, so a stray empty root folder never shadows the real filed entry. If the entry is nowhere, it returns the root form, and the reader reports it missing. `paths.locate` is pure: it never creates, moves or locks anything.
- The `dir` of `packets.packet_for_job` is the located folder, and its `stored_dir` is the row. Equality checks (`artifacts.run_for_dir`, `artifacts.is_registered`, `downloads.is_downloadable` and `packets.history_job_ids`) compare root forms, so a filed folder still matches its build.
- `packet_report.json` no longer stores `artifact_dir`. A filed packet would have made it a second, stale copy of the location.

This design replaced a journal-and-heal scheme: a crash in the middle of a filing pass leaves nothing to repair, because the database never named a bucket.

## Packet filing

The output folder files itself (#20). The unit of filing is an **entry**: a top-level folder such as `acme-2026-10-16`, or a legacy `2026-04-21/<company>/<role>` tree. Any other folder at the top level is an entry too. Filing is one `os.rename` of the whole entry into `paths.BUCKETS` (`Submitted`, `Closed before applying`, `Archive`) or back to the root. Entries are never renamed or merged.

### Which jobs an entry belongs to

An entry is linked to jobs by the paths inside it: a `build_runs` folder, an `artifacts` row or a `packet_sends` path. Only for an entry with none of those (the workup and prep folders of a live application have no build row) do two name rules apply: a `*.followup-{id}.md` file names its job, and `<slugify(company)>-YYYY-MM-DD[-N]` names every kept job of exactly that company. On a recorded entry the company rule would be wrong: a later shortlisted role would pull a filed application back to the root, or an applied role would pull an archived sibling's packet into Submitted.

`unfiled-followups` and dot-folders are never filed.

### Which bucket

`packet_filing.bucket_for_job` decides one job's bucket, send evidence first:

1. A `packet_sends` row or an `applied` event means Submitted. This beats the review state: an archived merge duplicate that was sent is still a submitted packet.
2. Archived means Archive.
3. Not kept (undecided) means the root.
4. Kept with `applied_at` set means Submitted.
5. Kept with a state that claims an application (applied, screen, interview, offer, rejected, ghosted, withdrawn or filled) but no evidence is a **contradiction**. It stays at the root and is reported, never filed as Submitted on say-so.
6. Kept with state `posting_closed` means Closed before applying. A `posting_check` verdict of closed alone never files a packet away.
7. Otherwise the root.

An entry takes its most visible job's bucket, in the order root, Submitted, Closed before applying, Archive. One role still to send is never buried by a sibling role's state. An entry with no linked job goes to Archive.

### Guards

- The [filing lock](#filing-lock): filing takes it exclusive and non-blocking, and a busy lock skips the whole pass with a logged warning.
- While a `build_runs` row is running with a live lease, only the entries it may write are skipped: those linked to its job, and any entry named for today. Each is named in a warning. The post-build hook or the next daily pass files them later.
- Directories only. A symlinked entry, or one holding a symlink, is skipped.
- An existing target is a conflict: it is reported, nothing moves, and nothing is merged. An entry that exists in two places at once is also a conflict, to resolve by hand.
- Each rename is re-checked at the moment it happens.
- A job-scoped pass (the hooks) looks only at that job's recorded entries and the unrecorded ones named for its company, so a cockpit click never walks the whole output folder.
- Report mode takes no lock and writes nothing, not even the lock file.

### Where filing runs

All of these are best-effort. A failure is a logged warning, never a refused move or a failed build.

- After a stage move (`stage_moves.run_action`, through `stage_moves.file_after_move`).
- After the status, archive, unarchive, unkeep and manual posting-verdict routes.
- After a successful build and after a successful refine.
- In `blastjob-daily`, after closure (`daily_filing.filing_pass`). A dry run logs the report only.

The report also lists Submitted entries filed by job state with no as-sent record, and the entries staying at the root because they are still to send.

## Filing lock

`<output_dir>/.filing.lock` is an `flock` that keeps writers and the filer apart. A writer that located an entry and is about to write into it must not have the folder renamed out from under it, or its file lands at a path that no longer exists.

- **Writers take it shared**, around locate plus write, so they never block each other. The writers are `run_build` while it picks the folder, the web build around `client.finish_build` and registration, refine around locate and indexing, and prep, follow-up and packet release at write time. Async code uses `filing_lock.shared_async`, which waits in a worker thread so the event loop never stalls behind a filing pass.
- **It is never held across an LLM call.** A build or refine is covered for its streaming window by its running `build_runs` row, which is why the filer skips the entries that row may write.
- **Filing takes it exclusive and non-blocking** (`filing_lock.exclusive`). If any writer holds it, the pass is skipped and reported busy instead of stalling a cockpit request behind a minutes-long build.

A crashed process frees its lock by dying. The lock file is never unlinked: with shared holders, unlinking would let the next exclusive locker lock a fresh file while a reader still held the old one.

## Document renames

Indeed refuses an upload name of 90 or more characters (#19), so the document token in a packet's file names is capped at `paths.DOCUMENT_TOKEN_MAX` (40) characters. Packets built before the cap, or before an abbreviation-table change, still carry an older token.

`blastjob-daily --file-packets-report` lists the renames and entry moves it would make and writes nothing. `blastjob-daily --file-packets` first renames, then files entries by stage (see [Packet filing](#packet-filing)).

The rename step covers Shortlist jobs only: kept, never applied, so a packet still to send. An application already went out under the old name, and a closed or archived role will never be uploaded. For each such job it takes the newest packet's resume, cover-letter and ATS documents whose token is one of `paths.document_tokens` but not the current `paths.document_token`, and renames them. The company and role come from the build's own metadata, the same pair `artifacts.role_files` matches on, so the renamed files are still found.

A **family** (`{person}_Resume_-_{token}` with its `.md`, `.docx`, `_ATS.ats.txt` and `.vN` variants) is renamed together or not at all, and not at all when any member:

- is named by a `packet_sends` row, because an as-sent record must keep naming the file that was sent;
- is quarantined or named by a quarantined packet report, because a renamed held file would escape the hold;
- is named in the folder's `RELEASED.txt`, the audit trail of a quarantine override;
- would land on a name that already exists.

Skipped families are reported with the reason. The files are renamed and the `path` column of their `artifacts` rows rewritten in one transaction: a missing row is inserted, and any failure renames every file back and rolls back. A row left naming the old file when only the renamed one exists (a run killed between the rename and the commit) is repaired by pointing it at the renamed file. The step runs under the exclusive filing lock and is skipped, with a message, while a build or refine is running.

The stage SQL the renamer uses to find Shortlist jobs lives in `core/stage_sql.py`, shared with `web/stages.py` (see [pipeline stages](web.md#pipeline-stages)).

## Backfill

`blastjob-daily --backfill-artifacts-report` lists what the backfill would index and writes nothing. `blastjob-daily --backfill-artifacts` indexes it. It is idempotent. It does two things:

- **Artifacts.** For every successful `build_runs` row, the files that run wrote for its own role (`artifacts.role_files`, never a glob of a shared folder), newest run first so a re-tailored file is credited to the run that last wrote it, plus each `packet_sends` resume and cover-letter path. A missing or unreadable file is listed in its own section and skipped; the rest still register.
- **Events** (source `backfill`). An `applied` event at `applied_at` for every kept job that has one, and for every other non-silent application state an event at `stage_changed_at`. Only `posting_closed` and `filled` fall back to `posting_closed_at`; any other state with no `stage_changed_at` is listed as undated, never dated at a later closure. A state with no date on record is listed, not guessed. A job that already has an event of that kind is left alone. The report says these are reconstructed events.

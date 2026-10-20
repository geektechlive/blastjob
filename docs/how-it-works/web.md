# The web cockpit

**What this covers:** How the FastAPI + HTMX cockpit is put together: app wiring, bootstrap, the security model, navigation, background runs, pipeline stages and moves, list rows, the settings page, compare and the funnel. For anyone changing web/.

The cockpit is the only surface blastjob has: a capability with no route does not exist (see [truth guarantees](../truth-guarantees.md#web-is-the-only-surface)). Everything below lives in `src/blastjob/web/`.

## App and dependencies

`app.create_app` builds the FastAPI app. It takes two injectable callables, `load_config` and `save_config`, which default to `load_config` and `save_config` from `src/blastjob/config.py`. Tests pass their own so they run against a temp data dir and never touch a real one (see [testing](testing.md#config-and-data-dir-guard)). Whatever loader is injected, `create_app` wraps it so every load also installs the runtime settings (see [data model](data-model.md#settings-storage)).

`create_app` then builds one frozen `deps.Deps` carrying five things: `conn`, `load_config`, `render`, `nav_counts` and `radius`. Each route module exposes `register` taking the app and that object, so its dependencies are explicit and checked by mypy instead of captured from a closure. Three things differ from that pattern:

- `routes_settings.register` also takes `save_config`. It is the only route module that writes settings, so `Deps` does not carry it.
- `register` in `web/same_role.py` and in `web/manual_job.py` still takes positional callables instead of `Deps`.
- The job-level and run routes still live in `web/app.py` itself: the job page, keep, archive, status, notes, follow-up date, posting verdict, packet release, the stage action, the pipeline board, the discovery and matching run buttons, the run status routes, company unexclude and `/health`.

The `conn` callable returns a new connection on every call, and the caller closes it. This is a rule, not a convenience: it is what keeps a failed request from leaking a connection. Each `conn()` goes through `client.init_db`, which migrates once per database per process (see [data model](data-model.md#tables)).

Settings are loaded per request and per daily run, so an edit shows up without a restart. The exception is `web.host` and `web.port`, which `launch.bind_address` reads once at startup, so they take effect when the cockpit restarts.

## Bootstrap

Settings live in the database, and the database lives in the data dir, so the data dir has to be found first without a config object. `load_config` (in `src/blastjob/config.py`) does this on every call:

1. `resolve_data_dir` returns `BLASTJOB_DATA_DIR` if set, else `paths.data_dir` read straight from a legacy `config.toml` (no model, so a custom legacy data dir is still found), else `~/.blastjob`.
2. Connect and migrate through `client.init_db_at`.
3. `settings_store.import_legacy_once` runs: if the `settings` table is empty and a legacy TOML exists, it imports the file once under `BEGIN IMMEDIATE`, records an `imported_from` marker row, and leaves the TOML in place as a backup. When rows already exist this is a plain read that never waits on a write lock.
4. Load and validate the rows through the Pydantic config model, then install the runtime settings.

`client.init_db` never needs a config object, which is what breaks the config-to-database cycle.

On launch, `launch.main` also recovers builds a previous process left mid-flight (`client.recover_stale_builds`) before it starts uvicorn.

## Security model

The cockpit has no login. By default it binds `127.0.0.1`, so only your own computer can reach it. Binding anywhere else is an opt-in through `web.host` (see [LAN access](../operations/lan-access.md)), and it makes everything below matter more. A web page you merely visit must not be able to read from or change the cockpit.

Three layers enforce that, in this order.

**1. `security.LocalRequestGuard`.** It is the outermost middleware (added last, so it runs first). On every HTTP request, GETs included, the `Host` header must be loopback or a private address literal (RFC1918, link-local, unique-local); anything else gets a 421. A DNS-rebinding page always arrives under its own domain name, so this is what stops it reading pages. A write (anything but GET, HEAD or OPTIONS) whose `Origin` or `Referer` is not local gets a 403 before any body is read. The same guard adds `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin` and a Content-Security-Policy to every response (see [Content-Security-Policy](#content-security-policy)).

**2. `upload_guard.MaxBodySizeMiddleware`.** It rejects an over-cap body (413) by declared length or by counting bytes. It has to be middleware: `require_csrf` reads the form, and Starlette spools a whole upload to disk to build it, before any route code runs, so a size check inside the route can never fire first. The cap is `upload_guard.MAX_NETWORK_UPLOAD_BYTES`, sized for a full LinkedIn archive.

**3. `security.require_csrf`.** Every state-changing route depends on it. It requires the per-launch token, sent as the `x-blastjob-token` header (`web/templates/base.html` puts it on every HTMX request) or as a `_token` form field, compared in constant time, plus a local `Origin` or `Referer` when one is present. The token is new on each launch. A tab left open across a cockpit restart therefore carries a dead token; `web/templates/base.html` catches the 403, reloads the page and says why.

### Content-Security-Policy

Defense in depth for a cockpit with no login: if an escaping slip ever let untrusted job, alert or model text become markup, the policy stops it running script that holds the CSRF token. The guard makes a fresh nonce per request (`security.csp_policy`) and sends:

`default-src 'self'; script-src 'self' 'nonce-<n>'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`

`app.render` exposes the nonce to templates as `csp_nonce`. What that means when you edit a template:

- An inline `<script>` needs `nonce="{{ csp_nonce }}"`. There is one, in `base.html`.
- Inline event handlers (`onclick=` and the like), `hx-on` attributes in any spelling and `javascript:` URLs are blocked. `tests/test_web_csp.py` fails if a template has one. Put the behaviour in `web/static/cockpit.js` and mark the element with a data attribute instead (`data-after-request="reload"` or `"home"` replaces `hx-on::after-request`).
- A script inside an htmx-swapped partial is checked against the nonce of the page already loaded, not the partial's response, so partials carry no script. The run panels (`_build_panel.html`, `_followup_panel.html`) hold only data attributes, and `cockpit.js` wires them from `htmx.onLoad`. Do not use htmx's `inlineScriptNonce` setting to work around this: it stamps the page nonce on every script in swapped content, which is the hole the policy closes.
- `style-src` keeps `'unsafe-inline'` because templates use `style` attributes and htmx injects an indicator style. It has no nonce on purpose: a nonce there makes browsers ignore `'unsafe-inline'`.
- No directive allows `'unsafe-eval'`. htmx only evaluates strings for `hx-on`, `js:`-prefixed `hx-vals` or `hx-headers`, and `[filter]` expressions in `hx-trigger`, and the templates use none of them (`hx-vals` and `hx-headers` are plain JSON). Adding one needs `'unsafe-eval'`, so do not.

"Local" in the Origin check includes private LAN addresses, because a device on your LAN sends its own view of the server as the origin. That means the check cannot tell the host machine from another LAN device. Reveal-in-Finder is therefore gated separately on `request.client.host` being loopback (`packets.is_local_request`).

All untrusted job text renders through Jinja autoescaping and is fenced in prompts, never followed as an instruction. Every `href` fed by a discovery feed or by model output goes through the `safe_url` template filter (`urls.safe_external_url`), because `web/templates/base.html` carries the CSRF token and a `javascript:` link would be a full takeover. The trust level of each input is listed in [truth guarantees](../truth-guarantees.md#trust-boundaries).

## Navigation

`app.STAGE_NAV` is the one list of pipeline stages. It is derived from `stages.STAGES`, `stages.LABELS` and `stages.ROUTES`, and `web/templates/base.html` renders it as the nav bar. Everything else sits under a **More** menu: Matches, All, Compare, Pipeline, Funnel, Packets, Companies, Network, Work history, Ingest and Settings.

`app.render` injects the stage counts (from the `nav_counts` callable on `Deps`) into every full-page template that did not pass its own. Partials, whose template names start with an underscore, are skipped. It also sets `first_run` for full pages: until the candidate profile is configured (`is_configured` on the candidate model in `models/config.py`, which needs `candidate.target_titles`), the cockpit shows a banner linking to `/settings`.

## Background runs

Long tasks (discovery, matching, tailoring, ingest, research, prep, outreach, follow-ups) run as background jobs through `runtime.RUNTIME`, one process-wide `runtime.Runtime`. A `runtime.Run` holds a replayable event log. Runs continue whether or not a browser is attached.

There are two ways to follow a run:

- **Build panels** (tailor, refine, prep, outreach, research, follow-up, ingest) stream over server-sent events from `/builds/{run_id}/events`. The browser sends `Last-Event-ID` on reconnect and the stream resumes from there, so a refresh resumes the panel instead of restarting the work. If the run is gone (the cockpit restarted, or it aged out) the stream answers with one error frame that says so, which closes the panel.
- **The global banner** polls instead. `web/templates/base.html` loads `/runs/active` on every page and re-polls `/runs/{run_id}` while a run is going, so a discovery or matching run started on one page stays visible after you navigate. `app.BANNER_RUNS` lists the run names the banner tracks: discovery, matching and the scoped `score-job` run.

`runtime.Run.events` is capped at `runtime.MAX_EVENTS_PER_RUN`, dropping the oldest first. One event is appended per streamed chunk, and the cockpit is a service that never restarts on its own, so the log would otherwise grow without bound. Event ids keep increasing, so replay still works: a consumer asking for an older id gets everything still held. Finished runs are evicted after `runtime.RUN_RETENTION_SECONDS`, and `runtime.MAX_RUNS` caps the total.

### Single-flight

`runtime.Runtime.start` takes a run name, the work to do and an optional key, and hands back the already-running run instead of starting a second one, in two ways:

- **By name.** `runtime.SINGLE_FLIGHT` lists discovery, matching and ingest. Discovery and matching share one slot (`runtime.FLIGHT_GROUPS`), because both write the same rows and pay for LLM scoring. Ingest overwrites `work_history.md`, the most irreplaceable file in the system, so two concurrent ingests must not race.
- **By `(name, key)`.** When a `key` is given, a second start for the same pair returns the running run, while the same action for another subject still starts. The keys in use are:

| Run | Key |
|---|---|
| prep | the job id |
| followup | the job id |
| outreach | `job_id:contact_id` |
| research | the company key |
| enrich | the fixed string `network` |

Only a *running* run holds its key. With no key there is no deduplication. Tailor and refine are not keyed here: they exclude each other and themselves through a compare-and-set claim in the database (`client.claim_for_build`, `client.claim_for_refine`).

The scoped `score-job` run (`manual_job.MANUAL_SCORE_RUN`: scoring one manual job, a paste re-score, the employer-description lookup) is deliberately not in `SINGLE_FLIGHT`, so it still runs while a full matching pass is in flight. It is in `BANNER_RUNS`, so the existing banner shows it and there is no second progress system. A response that starts such a run sends `HX-Trigger: refresh-run-status`; `web/templates/base.html`'s `#run-status` listens for it and re-reads `/runs/active`, which makes the run appear without a page reload.

There is deliberately no global cap on concurrent LLM calls. It was considered and dropped (#24).

## Pipeline stages

Every job is on at most one stage page. `stages.stage_of` is the Python form of that rule and the `*_WHERE` SQL fragments in `stages` are the SQL form; every page, header count, board lane and chip goes through one of them. The fragments are defined in `core/stage_sql.py` so that core code (the packet filer and renamer) uses the same rule without importing the web layer, and `stages` re-exports them. Archived and plain `new` jobs are in no stage.

On top of that, `stage_lists` resolves cross-source clusters: a cluster is one role, so it shows once, in the stage its representative sits in.

- A cluster with a kept member is represented by the kept member furthest along (application progress, then fit, then the lowest id). That member's stage is the cluster's stage, so an application in progress outranks a closed twin.
- Otherwise a cluster with an archived member is decided "no" and shows nowhere.
- Otherwise the best-fit active surfaced or queued member represents it.

Header counts come from `stage_lists.counts`, which calls the same list functions the pages render, so a badge can never disagree with the list under it. `stage_lists.stage_list_cache` computes each list once so the board and the counts share it.

| Stage (route) | Membership | Sort |
|---|---|---|
| Surfaced `/` | surfaced, source active | closed postings sunk, then fit, then newest; `sort=comp` puts posted pay first |
| Queue `/queue` | queued, source active | closed postings sunk, then fit, then newest |
| Shortlist `/shortlist` | kept, application state empty or `drafted` | closed postings sunk, fit (no score last), then tailoring state, then id |
| Applied `/applied` | kept, state applied, screen, interview or offer | furthest stage first, then due follow-ups, most overdue first |
| Closed `/closed` | kept, state rejected, ghosted, withdrawn, `posting_closed` or `filled` | most recent `stage_changed_at` first, else `posting_closed_at`, else `applied_at` |

Every list sorts best fit first. Stage, build state and recency only break ties. Surfaced and Queue sink a `closed` or `likely_closed` posting to the bottom because closure never moves an undecided job; the sort is what keeps a dead posting out of the way. Kept stages carry no source filter: a kept job whose posting went stale is still your decision and stays on your board.

Matches (`/matches`) is undecided jobs only: active, new, surfaced or queued, scored at or above a browse floor, with decided clusters left out. It also collapses the same role posted in several cities under one company and title. All (`/all`) lists every active job with a plain-language disposition and reason, and does not collapse clusters. `/kept` redirects to `/shortlist` and `/followups` to `/applied`, both with a 307 that keeps the query string.

## Stage moves

Moves live in `stage_moves`. Each is a compare-and-set on stored state, so a stale page can never push a job somewhere it no longer belongs, for example removing a job from the Shortlist after another tab marked it applied.

- **Mark applied** (Shortlist to Applied): stamps `applied_at` if it was empty and snapshots which documents went out.
- **Advance** (Applied): one step along applied, screen, interview, offer.
- **Withdraw** (Applied to Closed).
- **Reopen** (Closed): back to Applied if you had applied (`applied_at` is set), otherwise back to the Shortlist.
- **Remove** (Shortlist only): returns the job to where the gate would put it by its stored score. A stale posting goes to `new`, a missing score goes to the Queue, a score at or above the threshold goes to Surfaced, a score within the band below it goes to the Queue, and anything lower goes to `new`. A job in a hiring process or closed cannot be removed: that is an application, not an accidental Keep.

Every write to `application_state` stamps `stage_changed_at` and records an application event in the same transaction (see [events](events-and-filing.md#application-events)). A move that took also re-files the job's packet folders, best-effort (see [packet filing](events-and-filing.md#packet-filing)).

List-row actions post `from_list=1`. A successful move answers an empty 200, so the row's `outerHTML` swap removes it from the list. A refused compare-and-set answers `HX-Refresh`, which reloads the page to show where the job really is. Advance is the exception on success: the row stays on Applied with a new stage, so it answers `HX-Refresh` too.

## List rows and badges

Stage lists collapse cross-source duplicates to one row with a `+N sources` badge, where N counts every cluster member, including ones the current query excludes. Matches uses `service.collapse_clusters` for the same job. Postings from staffing and recruiting agencies carry a `staffing agency` badge (`agencies.is_agency`) so they are not mistaken for the employer. Each row also carries the cluster's best apply link (see [same role](same-role.md)) and, on Applied, the newest application event.

## Settings page

`/settings` edits the settings that live in the database (see [data model](data-model.md#settings-reference)). Both mailboxes are editable there, through `web/mail_settings.py`. The rules:

- The mail form holds environment-variable **names** only, never values. A name that is not name-shaped is rejected, not cleaned up, and the rejected value is never echoed back. Otherwise a pasted app password would become a permanent plaintext setting.
- Mail variable names follow a naming rule (`mail_names.name_problem`; the rule itself is in [settings and secrets](../operations/settings-and-secrets.md#mail-accounts)). Without it, a client of the unauthenticated cockpit could aim the IMAP login, with a host of its choosing, at some other secret. The rule is enforced when the form is saved and reported by `blastjob setup doctor`; it is never applied at load time, so an install saved under an older name keeps scanning.
- Presence of a secret renders as a boolean, never a value, length or prefix. The cockpit has no auth, so anything it renders is readable by anything that can reach it.
- The page shows the *resolved* account for each mailbox and marks the fields that are inherited rather than set (a blank personal-mailbox field falls back to the alert account).
- The **Test connection** button posts to `/settings/mail/test/{role}`, where role is `alert` or `personal`. It runs a read-only login probe against the *saved* settings, never a submitted host, and reports login-rejected versus unreachable with the server's reply redacted.
- `mail_settings.status` and `mail_settings.probe` load the secrets file lazily, because the web process otherwise reports working credentials as missing.

## Compare

`GET /compare?state=surfaced|queued|shortlist|applied|closed&source=` renders a side-by-side table through `compare.compare_rows`, with no LLM call: comp, location, company-profile facts, strengths and gaps, and computed red flags, including aggregator postings. `state` is a pipeline stage name. The rows come from the stage lists in `web/stage_lists.py`, so Compare applies the same membership and cluster rules as the page it mirrors: a job hidden on Surfaced because a sibling is kept or archived is hidden here, and a kept job whose posting went stale still shows. Rows are re-ranked by alignment. The old `state=kept` of earlier links is read as `shortlist`; an unknown value falls back to `surfaced`.

## Funnel

`/funnel` places every active job in the stage it is actually in. It recomputes that from the pipeline's own functions (`geo.assess_geo`, `prefilter.classify_scope`, `gate.decide`) rather than from a stored reason, so it cannot drift from the pipeline. There is no `drop_reason` column to backfill. The cost is one pass over the active jobs per render.

It exists because the matching run counted geo and prefilter rejections and then threw the counts away, and the daily record stores only fetched, scored, new and sent. A funnel that was silently binning a large share of everything discovered looked healthy in the log. The page also flags any source with ten or more postings, none of which ever reached the alignment threshold (`funnel.dead_boards`), which catches a board token that points at the wrong company.

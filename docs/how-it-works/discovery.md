# Discovery

**What this covers:** How postings are found and stored: sources, identity and refresh rules, snapshot versus incremental adapters, each adapter's quirks, recruiter replies, finding boards and manual postings. For anyone changing `discovery/` or debugging coverage.

Discovery only fetches and stores. Deciding whether a posting is worth a look is [matching](matching.md); deciding whether it is still open is [closure](closure.md); recognising the same role under two names is [same role](same-role.md).

## Sources

Every source is optional and every default is empty or off, so a fresh install fetches nothing until you configure it.

| Source | Adapter | Key needed | Stored `source` |
|---|---|---|---|
| Greenhouse, Lever, Ashby boards | `discovery/greenhouse.py`, `discovery/lever.py`, `discovery/ashby.py` | none | `greenhouse:{token}`, `lever:{token}`, `ashby:{token}` |
| Workday career sites | `discovery/workday.py` | none | `workday:{tenant}` |
| Dayforce candidate portals | `discovery/dayforce.py` | none | `dayforce:{client}` |
| Adzuna | `discovery/adzuna.py` | optional free key in the secrets file | `adzuna` |
| LinkedIn and Indeed alert email | `discovery/mailalerts.py` | an IMAP app password | `linkedin`, `indeed` |
| Retained search firms | `discovery/searchfirms.py` | `JINA_API_KEY` | `searchfirm:{slug}` |
| Apple careers | `discovery/apple.py` | `JINA_API_KEY` | `apple` |

ATS boards are listed in `discovery.target_boards` (company, ATS, token). Adzuna runs only when `discovery.enable_aggregators` is on, alert email only when `discovery.mail_alerts_enabled` is on, and Apple only when `discovery.apple_queries` is set. The Apple source exists because one user asked for it; it reads the public search pages through the Jina reader.

There is no scraping and no browser automation. Every source is a public JSON endpoint, an email the user receives, or a page read through the Jina reader. `discovery.exclude_companies` (a case-insensitive whole-word match on the company name: "Meta" drops "Meta Platforms" but not "Metavante") drops postings before they reach the database.

The source is namespaced per board (`workday:{tenant}`, `greenhouse:{token}`), so one board's outage can never stale-mark another board's postings.

## Identity and refresh

Adapters fetch concurrently and tolerate a failure in any one source. Each posting is normalized to a `RawPosting` and upserted by identity `(source, external_id)` (`db/client.py`, `upsert_job`).

A refresh touches only **source-owned** columns: company, title, url, the description, location fields, posted comp and `cluster_id`. It never touches the user's review, build or application state, so decisions survive rediscovery. Three more guards each exist because of a real overwrite:

- An empty description means "presence only". It refreshes `last_seen` and never blanks a stored description. A presence stub also carries no location, so it cannot overwrite a good one.
- An empty company never replaces a stored company.
- A whole-card alert row (see [LinkedIn whole-card layout](#linkedin-whole-card-layout)) never refreshes its title, because its "title" is the whole card text.

Each posting is saved in isolation. One bad row is recorded as a source error and the run continues; it never ends the run. A new posting joins the cluster for its fingerprint, but a stored posting keeps its cluster on refresh; see [same role](same-role.md#clusters).

## JD quality classes

A description has one of five classes, lowest first: presence stub (empty), snippet, full, employer, pasted. A snippet is anything under 600 characters (`MIN_FULL_JD_CHARS`); `is_jd_snippet` in `db/client.py` is the test, and a pasted description is never a snippet however short.

The refresh rule compares classes, never lengths:

- A lower class never replaces a higher one. A snippet cannot clobber a full description, and nothing a source sends replaces a pasted one.
- The same class does replace, so a revised full posting still lands.
- Text that differs only in whitespace is no change and costs no re-score.

When the text changes on an undecided row, its score is cleared so the daily `only_unscored` pass scores it again. A kept row keeps its score; see [pasted descriptions](matching.md#pasted-descriptions). The employer class is described in [employer data](employer-data.md).

## Snapshot vs incremental

An adapter is one of three shapes, and its silence only means as much as the shape promises.

| Shape | Adapters | A posting missing from a successful fetch |
|---|---|---|
| Full snapshot (`snapshot = True`, `stale_after_days = 0`) | Greenhouse, Lever, Ashby, Dayforce | Is gone. It is stale-marked at once. |
| Windowed snapshot (`snapshot = True`, `stale_after_days = 14`) | Workday, Adzuna | Proves nothing. It ages out by `last_seen` after 14 days unseen. |
| Incremental (`snapshot = False`) | alert email, search firms, Apple | Means nothing, ever. The pipeline never stale-marks for it. |

Why the middle shape exists: Workday caps each query at 100 relevance-ranked hits and Adzuna reads one page, so the set a run sees rotates. Treated as a full snapshot, a rotating window marked live roles gone every day and produced fake "new" and "gone" churn. Treated as purely incremental, closed roles would live forever. So a windowed posting is only a candidate after 14 quiet days, and where the adapter has a detail endpoint (Workday `confirm_open`) it must confirm with an explicit 404 or 410 before anything is marked. A timeout, a 5xx or an unmappable URL counts as still open. At most 25 candidates are probed per source per run (`MAX_CLOSURE_PROBES_PER_SOURCE`). A posting that turns out to be still open gets its `last_seen` reset so it is not probed again every run.

A "successful" fetch can still be wrong, so stale-marking has guards (`_short_snapshot_reason` in `discovery/pipeline.py`):

- A source that returns 0 postings while 3 or more are active retires nothing (`MIN_ACTIVE_EMPTY_GUARD`).
- A full snapshot (not a windowed one) that lists fewer than half of 3 or more active postings retires nothing (`MIN_ACTIVE_HALF_GUARD`, also 3).
- An adapter that knows it read only part of a board sets `partial_listing_note`, and nothing under it is marked. Dayforce does this when it hits its page cap, and it fails the source outright if an empty page arrives short of the server's own total.
- If any adapter reporting a given source failed this run, that source is not swept.

Each skip is recorded as a note on the run and appears in the daily digest. For an authoritative source, stale means closed (see [closure](closure.md#verdicts)), which is why these guards are strict.

## Workday

The token is `tenant/wdN/site` (a malformed token fails at adapter construction, not at 3am). The adapter POSTs the listing once per title query, up to 5 pages of 20, and the queries are the ones in `discovery.adzuna_queries`. A GET for the detail runs only for postings with no stored description (`known_jd_ids`); the others become presence stubs. A posting whose detail fetch failed, or whose stored location is a "5 Locations" placeholder, is retried on the next run. Titles in a configured drop family are skipped before the detail fetch, with the same rule [matching](matching.md#title-prefilter) applies.

The storage identity is the requisition id when the listing carries one, else the posting path. It is built only from what the listing returns, so "fetch detail only for unknown postings" needs no detail fetch. An identity that depended on a detail-only field would duplicate a posting after a failed-then-successful detail fetch.

A requisition open in several places stores its primary location plus `additionalLocations` as "A; B; C" in `geo_location_raw`, with count placeholders such as "5 Locations" dropped. [Geo](matching.md#geo) judges each segment on its own, and same-role clustering places the row at its primary location only.

Rows stored before `additionalLocations` was read are repaired with `blastjob-daily --refetch-workday-locations-report` (lists geo-rejected Workday rows that have extra locations; writes nothing) and `blastjob-daily --refetch-workday-locations` (stores the full location list and re-scores the changed rows).

Known limitation (#42, accepted): two boards on one Workday tenant share the source `workday:{tenant}`. A requisition missing from both listings is probed through the board configured last, and the stored URL follows the last board saved. Requisitions are tenant-wide, so both boards update one row.

## Dayforce

Dayforce needs no key, but its search endpoint wants an anonymous CSRF handshake: `GET jobs.dayforcehcm.com/api/auth/csrf`, then echo the token in an `x-csrf-token` header on `POST /api/geo/{client}/jobposting/search`. This is the public search box's own anti-CSRF step, not a login. A bare POST gets a 403.

The description ships with the listing, so there is no detail fetch and `known_ids` never produces a stub. The token is `clientNamespace/jobBoardCode`, for example `exampleclient/CANDIDATEPORTAL`, with no host. Search queries are ignored on purpose: the unfiltered sweep returns the whole board in a handful of pages of 25. It is a full snapshot, guarded as described above.

## Alert email

`discovery/mailalerts.py` reads LinkedIn and Indeed job-alert mail over IMAP with an app password. The settings hold only the names of the environment variables; the values live in the secrets file. This is the sanctioned LinkedIn feed: no browser automation and no unofficial API. The adapter is incremental (`snapshot = False`), so it is never stale-marked.

- **Selection.** Messages are chosen by header only (sender domain, or an alert-looking subject) because some alerts arrive through a relay that rewrites the From header. The sender domain is that of the parsed From address (exactly, or as a subdomain), never the display name, so `"linkedin.com jobalerts" <x@example.com>` is not a LinkedIn alert; for an Apple Hide My Email relay (`privaterelay.appleid.com`, exact) the original domain folded into the local part counts too, and a fold on any other domain is ignored. The LinkedIn `jobalerts` / `jobs-noreply` marker is read from the address, not the display name. This is a routing hint, not an authenticity check. The lookback is `discovery.mail_lookback_days` (14).
- **Ledger.** Bookkeeping is a per-message ledger in `mail_ledger`, keyed by Message-ID, because a bare UID watermark would skip or replay mail. The ledger row is written in ONE transaction with that message's postings, each posting under its own SAVEPOINT. A message is `ok` if any posting saved and `error` (retried) only if none did.
- **Statuses.** `ok` and `ignored` are never fetched again. A recognized alert that parses to 0 postings is a parser failure, not a success: it logs a warning, dumps its HTML to `<data_dir>/mail-debug/` (one file per message, at most 20), and is recorded as `empty`. An `empty` message is retried every run until it ages past the lookback window, so a template restyle heals once the parser is fixed.
- **Transport.** IMAP is TLS-verified (`open_imap` uses a default SSL context), with a 30 s socket timeout and a 300 s deadline for the whole fetch. Past the deadline the connection is closed and the messages already read are kept.
- **Indeed parsing** handles click-tracking wrappers (`cts.indeed.com`, `pagead/clk`, `rc/clk/dl`), template chrome, salary lines and text/plain alerts. Parsers are tolerant (an anchor pattern plus nearby text) because the vendors restyle without notice.
- **Enrichment.** Alert text is a snippet. A posting's full description is fetched afterwards: LinkedIn through the Jina reader when a key is set, the others from the page's JSON-LD. Both are fail-soft, and the snippet stays otherwise.

## LinkedIn whole-card layout

In this layout (`discovery/linkedin_cards.py`) one `<a>` wraps the entire card, "Title Company · City, ST $pay Easy Apply", with no delimiter between title and company. Only a split based on structure is applied:

- The text before " · " ends with a company already in the database (`known_company_names`, loaded once per run), or
- the LinkedIn guest page header, read through the Jina reader (`guest_page_company`), confirms it: the title line plus the company line on the next line must rebuild the card text exactly. blastjob itself never connects to linkedin.com.

A vocabulary-only split (the longest prefix ending in a title word) is never applied at ingest. "Director of IT Acme Technologies" shows why. In that case the title is the text before " · " with pay and badges stripped, and the company stays empty.

A later title-only link for the same job id replaces a whole-card link, but only when it carries the "Company · Location" line. A bare "See job" anchor never does. The guest-page reader and pay parsing live in `discovery/linkedin_guest.py` (re-exported by `mailalerts`).

Rows stored before this fix are repaired by `discovery/linkedin_repair.py`:

- `blastjob-daily --repair-linkedin-titles-report [--accept ids]` lists the repairs and writes nothing.
- `blastjob-daily --repair-linkedin-titles [--accept ids]` applies them.

Splits proven by a cluster sibling's company or another row's company are applied. Vocabulary splits are "needs confirmation" and applied only for the ids passed with `--accept`. Repaired rows are then reclustered. The report stages the repair inside a SAVEPOINT, plans the recluster on that data and rolls back, so it predicts exactly what the apply run will merge.

## Recruiter replies

`discovery/replies.py` runs in `blastjob-daily` after closure. It is **surface only: it never drafts and never sends.** It reports that a reply exists and who it is from, and the user answers in their own mail client.

- **Which mailbox.** It reads the user's personal mailbox (`discovery.personal_mail_*`, each blank field falling back to the alert account). That is often a different account from the alert feed, because recruiters write to the address on the resume. Pointing the first version at the alert mailbox made it unable to see a reply: 58 messages fetched, none matched.
- **What gates it.** It runs when the personal mailbox has credentials. It is deliberately not gated on `mail_alerts_enabled`, which governs a different account's job feed; turning that feed off must not stop watching for a recruiter's answer.
- **Sender gate.** Before matching, the header is dropped if the parsed From address is at an alert domain (LinkedIn or Indeed; exact or subdomain, or folded in by an Apple Hide My Email relay, by the same rule as the alert selection above) or looks automated (`no-reply`, `notifications@`, `mailer-daemon`). A display name that merely mentions an alert domain does not drop a recruiter's reply.
- **Matching.** A sender must match an applied-to job: the sender address appears verbatim in the job's `app_notes`, or the sender's domain (TLD dropped, squashed) equals the job's `company_key` exactly. Webmail domains and very short domain labels never match. The match is made at the header, so unrelated personal mail is never body-fetched.
- **Window and dedupe.** 14 days (`REPLY_LOOKBACK_DAYS`). `mail_ledger` rows with source `reply` mean a reply is reported once.
- **Dry runs.** `blastjob-daily --dry-run` passes `record=False`. Stamping the ledger on a dry run would mark a reply seen, and the real run would never surface it.
- **Events.** A recording run writes one `recruiter_reply` application event per surfaced reply (source `replies`, ref the Message-ID) in the ledger's transaction; a dry run writes none. See [events and filing](events-and-filing.md).

## Recruiter mail from one label

The reply scanner above sees only mail that matches a job you already applied to, so a new recruiter's first message never reaches blastjob. `discovery/label_mail.py` closes that gap without widening what the app can see: it reads **exactly one label** in the personal mailbox, the one you name in `discovery.recruiter_label` and file mail under by hand. Empty means off. It is surface only: nothing is replied to, moved, labelled or flagged, and no model reads the mail.

- **The one folder.** It selects the quoted label read-only and nothing else: no other folder is selected, listed or searched. Host and credentials are the reply scanner's (`replies.personal_account`); an empty label or missing credentials return before any connection. A label the server does not have is a warning in the log and a note in the digest ("Recruiter label not found in the mailbox"), never a crash and never an all-clear.
- **What the label may name.** `label_names.py` refuses the inbox (any case), any `[Gmail]` or `[Google Mail]` system folder, and the personal and alert mailbox values, so the setting cannot be used to read more than the mail you filed. It also refuses a name that is not plain printable ASCII (no accents, tabs or line breaks), contains a double quote, backslash, `*` or `%`, is empty after trimming, or is over 100 characters; non-ASCII would need an encoding blastjob does not use. The rule runs when settings are saved (the form, `setup set`, an import) and again when they load: a bad stored value loads as off with one warning that does not repeat the value.
- **Identity and the watermark.** A message is identified by mailbox, UIDVALIDITY and UID. The reader keeps a watermark per UIDVALIDITY (`mail_triage_cursor`) and each run takes the UIDs above it, oldest first, at most 50, so the messages already in the label are read on the first run and a larger backlog drains over several. A message that cannot be fetched stops the sweep and is retried next run; one that has vanished is passed. If the server renumbers the mailbox (a new UIDVALIDITY), a message whose Message-ID is already recorded is not recorded again, and the watermark still moves past it. The reply scanner's `mail_ledger` is not touched, so a message both scanners see is fine.
- **Size and time.** The size and the server's receive time (`INTERNALDATE`) are fetched first, in a request with no literal so they parse reliably. The header fetch is capped at 64 KB. A message up to 2 MB is then fetched whole; a larger one, or one whose size the server did not give, is recorded from its headers only, with the snippet "Too large to preview here; open it in your mail." If the server gives no receive time, the row uses the time it was recorded and the page says "time not given by the server". Every body fetch is `BODY.PEEK`, so nothing is marked read.
- **Hostile mail.** Only the first 20,000 characters of a body are looked at, before any pattern runs, and markup is stripped by a linear parser (`replies._snippet`, shared with the reply scanner), so a crafted body cannot stall the daily run. Result words inside a parenthesised comment of `Authentication-Results` are ignored, and that header is read only up to 4,096 characters.
- **A message that never fetches.** A fetch failure stops the sweep and counts one failed run for that message. After three consecutive failed runs the reader gives up on it, moves the watermark past it and the next digest says once that a recruiter-label message could not be read and should be opened in your mail.
- **What is stored.** Per message: sender name and address, subject (300 characters at most), a plain-text snippet (500 at most), the receive time, the `Date` header (untrusted, display only) and the three sender-check results. The body is never stored. There is no sender gate: every message in the label is taken, and an automated-looking sender is marked, not hidden.
- **Sender checks.** Only the topmost `Authentication-Results` header is read, and only when its authserv-id is `mx.google.com`; Gmail adds its own header on top, and a lower header (or a top one naming another server) can be forged. Anything else gives `unknown`. The page shows "DMARC passed", "Sender checks failed" (any failed check) or "No DMARC result", and says plainly that this checks the sending domain, not the person. It is advice and never hides a message.
- **Address match.** The sender address equals, case-insensitively, a network contact's stored email. Job contacts have no email column, so they do not take part. An address is easy to forge, so the green "Known sender" badge shows only when DMARC also passed; otherwise the page says "Address matches a contact".
- **Daily.** `daily_label.label_pass` runs next to the reply scan, and like it after the model cutoff because it makes no model call. A dry run reads but records nothing. A failure is a source-error line and does not sink the run. See [daily](daily.md#recruiter-mail).
- **The page.** `/recruiter-mail` lists new messages first, then those decided in the last 60 days. Each decision is a protected POST that carries the version it was rendered with; a stale one gets a 409 and changes nothing. Deciding an already decided row needs the explicit **Change**. Linking to a kept job records one `recruiter_reply` event with source `mail_label` at the receive time, ref `label:<uidvalidity>:<uid>`; changing the link or dismissing removes the event this feature made, in the same transaction as the decision. Linking to a recruiter in the network records nothing else. No link from a message is rendered. See [events and filing](events-and-filing.md).

## Retained search firms

`discovery/searchfirms.py` is a weekly, incremental adapter over the sites listed in `discovery.search_firms`. Retained searches rarely post to ATS boards, so the firm's own opportunities page is often the only public trace. It uses Jina search (`site:{domain}` plus title and region terms, at most 3 queries per firm). It runs only on `search_firm_weekday` (default Monday), the source is `searchfirm:{slug}` and `snapshot = False`.

`blastjob-daily --sweep-search-firms [--no-score]` runs it on demand, outside the weekly gate. It sends nothing and writes no `daily_runs` row.

Expect low yield. Most firms do not publish assignments in a searchable way, and practice or marketing pages are false positives, filtered by `_NOT_POSTING`. A firm that has never produced a posting is reported as a note on the run, not an error, so a dead channel does not pass for a working one.

## Category searches

Some interests are categories, not companies: "Example robotics firms" cannot be a company watch. A **category search** (migration 73; added on the **Category searches** section of `/targets`) is a saved role search of up to three terms, one per line. At most 10 categories exist, each label is at most 60 characters and each term at most 80. The weekly run (`discovery/category_search.py`, called from `daily_steps.category_step`) is part of the daily run only: it runs on `discovery.search_firm_weekday`, after discovery and before matching so what it finds is scored the same day, and it makes no model call.

For each enabled category it searches two ways.

- **Adzuna.** Only when aggregators are enabled and the Adzuna keys resolve. One `AdzunaAdapter` per category (`build_category_adzuna`), its terms as the queries. It is not a snapshot, so it never stale-marks. Results keep `source='adzuna'` and Adzuna's own id, so a posting found by the main search and by two categories is one job with two hits, and saving refreshes `last_seen` like the main search, which keeps the main snapshot's 14-day TTL from retiring a category-found row. A dream category sends no place and searches the whole country; any other category uses `discovery.adzuna_where` and the commute radius. Titles must pass the configured leadership filter (`workday.title_wanted` with `discovery.title_keep_terms` and `title_drop_terms`) before anything is saved.
- **Jina web search.** Only when `JINA_API_KEY` resolves. One query per term, `"<term>" ("CIO" OR "VP of IT" OR "Director of IT" OR "CTO") job`, so the term scopes every title. A result is kept only if its URL passes `urls.safe_external_url`, is not LinkedIn (blastjob never reads LinkedIn, see [truth guarantees](../truth-guarantees.md#never-contact-linkedin)), passes `looks_like_category_posting` and its title passes the same leadership filter. That classifier is separate from the search-firm one: it accepts known ATS hosts (Greenhouse, Lever, Ashby, Workday, Dayforce) and URLs with a job-path hint (`/job`, `/careers/`, `/positions/`, `/opening`), and refuses job aggregators and article paths (`/news/`, `/blog/`). A URL with a backslash, whitespace or control character, or an `@` in its host part, is refused before any host check, as are IP-literal hosts, `localhost` and `.local`, `.internal` and `.lan` names (LinkedIn and its `lnkd.in` shortener were already refused). A kept page is read through the Jina reader and saved as `source='category-web'` with the canonical URL as its external id: lowercase host, no fragment or trailing slash, tracking parameters (`utm_*`, `gclid`, `fbclid`, `ref`, `source`) dropped, and identity parameters kept (`gh_jid`, `jobId`, `job_id`, `id`, `req`, `reqId`, `requisitionId`, `jk`, `posting_id`, any case), so two postings that differ only by an id stay two. A page already stored, under any source, gets the hit and is not read again or duplicated, unless that job is archived: a job you dismissed gets no hit, from either half. The place comes from `searchfirms.infer_geo` on the page text; when it finds none the place stays unknown and the role follows the ordinary rules (review), never a guessed location. The company field is only a label taken from the address (the board slug on Greenhouse, Lever or Ashby, else the host), not a claim about the employer.

**Caps per run**, each logged and shown as a note on the run when it bites: 10 search terms in total (`MAX_SEARCHES_TOTAL`), a window that bounds Adzuna and Jina alike (each category's Adzuna adapter is built with only its planned terms, so at most 10 Adzuna queries and 10 Jina searches), 5 page reads per category and 20 in total, 20 new jobs per category. With more (category, term) pairs than searches, the starting offset moves with the ISO week (`plan_searches`), so every term is searched over the weeks.

**Hits.** `category_hits` holds one row per (job, category) with the category's `terms_version` when it was found, so a job found by two categories has two rows. A hit counts (badge, dream exception) only for an enabled category whose current `terms_version` equals the stored one. `terms_version` moves when the terms or the dream flag change, so editing those hides the old hits until a later search finds the job again; a label edit, disabling and re-enabling do not lose them. Deleting a category removes its hits and keeps its jobs.

**Atomic and edit-safe.** For each category the jobs and their hits are written in one transaction (`BEGIN IMMEDIATE`, one savepoint per result, one commit). The run remembers each category's id, `terms_version` and `enabled` and, if any changed while it ran, drops that category's results, including the job rows they would have created. A failing category (Adzuna error, Jina error) is recorded under `category-adzuna:<slug>` or `category-web:<slug>` like any adapter error, redacted, and the others still run.

**Trust.** `category-web` is a source nothing treats as authoritative: it gives no closure evidence (it is not in `closure_links.AUTHORITATIVE_PREFIXES`), it is not an aggregator snippet (`employer_jd.AGGREGATOR_SOURCES`), and its apply link is the page itself under the label "web search result": the same limited trust as a search-firm page. Its text becomes `jobs.raw_jd` from any host with a job-like path, so treat it as untrusted: the scorer fences and labels a description as data (see [matching](matching.md)), but the resume and cover-letter drafter does not (tracked separately). A dream category's geo exception is described in [matching](matching.md#the-dream-list-exception).

## Finding boards

`discovery/resolve.py` maps a company name to its public board token. Run:

```
uv run python -m blastjob.discovery.resolve "Acme Corp"
```

It probes Greenhouse, Lever and Ashby with slug variants, and Workday and Dayforce through Jina search plus one confirming POST (`probe_workday`, `probe_dayforce`). Without `JINA_API_KEY` it probes only the first three and says so. Add the confirmed boards to `discovery.target_boards`.

Treat a hit with suspicion. Jina candidate extraction is noisy, so open a sample posting and check the company and location before trusting it. A board token once pointed at an unrelated, similarly named company and delivered dozens of irrelevant postings a day. The `/funnel` dead-board check now names that case: a source with 10 or more postings, none of which ever scored at or above `alignment_threshold`. Most employers in a probe batch resolving to nothing is normal.

## Dream list boards

A company on the dream tier of the target list has its board read with two limits (`discovery/dream_boards.py`, built in `pipeline.build_adapters` from `target_store.dream_keys`). Workday boards are queried by the candidate's title keep-terms (`discovery.title_keep_terms`, at most 8) instead of `adzuna_queries`, which are tuned for an aggregator, and read at most 3 pages per query and fetch at most 100 descriptions (`DREAM_MAX_PAGES`, `DREAM_MAX_POSTINGS`; Dayforce gets the same caps through `max_pages` and `max_wanted`). Greenhouse, Lever and Ashby list a whole board in one call, so `CappedBoardAdapter` wraps them: it keeps the postings whose title the prefilter wants and takes at most 100 of them. The filter runs before the cap, so the cap cannot drop a leadership role in favour of an engineer. A capped read is a windowed snapshot (it ages postings out after 14 days), because one run's absence proves nothing.

**Adding a board** is done from the company's page, not by editing settings (`web/routes_boards.py`, `discovery/board_finder.py`). **Find job board** runs the `resolve.py` probes in a worker thread, one search at a time (`asyncio.to_thread`), because they are blocking and Dayforce's probe calls `asyncio.run`. The cockpit answers other requests meanwhile. For each board found it gathers identity evidence:

- `domain_link`: a Jina read of the careers pages on the domain you entered (`/careers`, `/jobs`, `careers.<domain>`) links to this exact board.
- `ats_name`: Greenhouse's board record reports a `name`, and its `company_key()` equals the company's. A name that only contains the company's ("Acme Staffing" for "Acme") is shown but is not evidence, so the board needs your tick. blastjob reads an organization name only from Greenhouse; for the other systems this basis is never used.
- Neither: the board is shown "not verified" with its sample postings, and is saved only when you tick that it belongs to the company (`owner_confirmed`).

Board URLs in a careers page count only when the URL itself is on the ATS host, not when it sits in another site's query string. The search result is kept on the server for 30 minutes (`FINDINGS_MAX_AGE_S`); an older one is gone at confirm and the page says to search again. Only one search runs at a time: starting a second shows "A search is already running" instead of queuing. A board already listed under a different company shows a notice naming that company. **Use this board** posts only the ats and token; the basis comes from the kept result, so a field sent by the browser changes nothing. The route reloads the latest settings, skips a board already present by normalized (ats, token), appends a `TargetBoard` with its `identity_basis` and saves with the revision check. `StaleSettings` shows "try again". The settings page keeps each board's basis when it is saved (`settings_form.parse_boards` matches by company, ats and token). Without a Jina key, Workday and Dayforce are not searched and the page says so; an empty result is shown as a coverage gap, never as an all-clear. Sample postings are for your judgement and are never evidence by themselves.

## Manual postings

`GET/POST /jobs/new` (`web/manual_job.py`) adds a posting by hand, for channels no adapter covers (a screenshot, a forwarded link). Company, title and description are required. It inserts one `jobs` row with `source='manual'`, stamped as a pasted description, and a deterministic sha256 `external_id` (of the URL when one is given, otherwise of company, title and text). Submitting the same posting again lands on the same row instead of duplicating it.

It then starts an ordinary scoped `run_matching(..., job_ids=[id])` pass, so the same gate, geo, prefilter and compare-and-swap rules apply; nothing is special-cased. That run is named `score-job`, kept off the single-flight list so it still runs while a full matching pass is in flight, and shown in the global run panel. A URL that fails `safe_external_url` is dropped, not stored, with a visible note on the job page.

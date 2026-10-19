# Posting closure

**What this covers:** How blastjob decides a posting is closed, and why only proof moves a job: verdicts, employer links, re-checks, state moves, shadow mode, careers-page links and their trust levels, and safe fetching. For anyone changing closure code.

The rule behind all of it: **only proof moves state.** A posting is `closed` only when a source that can prove absence says so. Adzuna ageing out is `likely_closed` and moves nothing, a failed probe is no evidence, an ambiguous employer link is no link, and a manual verdict or manual state change always beats automation. Falsely closing a live job is the costly error, so every step here leans toward "no link" and "no evidence".

## When closure runs

Closure (`core/closure.py` and `core/closure_links.py`) runs inside `blastjob-daily` after discovery and matching, so it sees today's `source_state`. A failure is reported like a source error and never sinks the run. The result per job lives in a handful of `jobs` columns (`posting_check`, `posting_checked_at`, `posting_closed_at`, the `employer_link_*` fields, `closed_prior_state`). Every automated state write is a compare-and-swap guarded on `posting_check_manual=0`.

The pass never awaits network I/O while a write is pending: it commits before the first probe and after every network-derived write, because the cockpit writes to the same database.

## Verdicts

Each job has a `posting_check` verdict:

- `verified_open`: confirmed still posted by the employer's board or page.
- `closed`: a source that can prove absence says it is gone.
- `unverifiable`: no evidence is possible (an agency posting, an intermediary, a limited-trust page, a dropped link).
- `likely_closed`: Adzuna aged the posting out and there is no link. A badge only; it never moves a job.
- empty: not checked.

A job from an authoritative source (`workday:`, `greenhouse:`, `lever:`, `ashby:`, `dayforce:`) gets its verdict locally, with no request: `closed` once stale, else `verified_open`. That is safe because those pipelines stale-mark only after a full listing or a confirming 404 (see [snapshot vs incremental](discovery.md#snapshot-vs-incremental)).

Jobs from LinkedIn, Adzuna, manual entry, search firms and Apple cannot prove anything themselves; their silence means nothing. They need an employer link.

## Employer links

An employer link points a non-authoritative job at the same posting on a board that can prove closure. It is found in this order, and only one that passes is stored:

1. **A cluster sibling** that is an authoritative posting, or **a company and title match already in the database**. Either counts only when the postings are strictly the same metro, both remote, or the user confirmed the pair as the same role (`role_clusters.sibling_is_proof`), the lifetimes overlap (a candidate last seen before this job first appeared is an earlier posting), and exactly one sibling qualifies.
2. **Intermediaries** (`agencies.is_agency`) are `unverifiable` with no request made.
3. **Otherwise `discovery/resolve.py` finds the company's boards** (see [finding boards](discovery.md#finding-boards)). A board counts only if its slug or tenant IS the company key (`board_matches_company`; an abbreviation tenant is rejected on purpose, since no link beats a wrong one). The title must then match exactly after normalization, or be the single candidate at or above `TITLE_MATCH_MIN_RATIO` (0.92, uncalibrated). Two plausible candidates mean "don't know", so no link.
4. **Last, an employer careers page** (see [careers-page links](#careers-page-links)), but only when no board matched and none was ambiguous, and a Jina key is set.

`closure_links.normalize_title` expands abbreviations (`vp` to "vice president"), the opposite direction from the shared title normalizer in [same role](same-role.md#title-normalizer), and its similarity ratio is calibrated on that form. They are deliberately separate: changing it could falsely close a live posting.

A row with an empty `company_key` is skipped before any network call. Its verdict is untouched and the search cap is not spent.

## Re-checks

A linked posting is re-checked on every run, within the caps:

- **Workday:** `confirm_open`, a detail GET where only a 404 or 410 counts as closed.
- **Greenhouse, Lever, Ashby:** membership in the full board listing.
- **Dayforce:** membership in the listing, unless the listing hit its page cap, in which case absence proves nothing.
- A failed probe keeps the prior verdict.

A stored sibling link is re-checked with `sibling_is_proof` every run. Once it fails (the user said "not the same role", or the cluster moved), the link is dropped, the verdict becomes `unverifiable`, no state moves, and a close that link had caused is reopened through `reopen_transition`. A description adopted over the dropped link is reverted with it; see [employer data](employer-data.md).

A "not the same role" decision clears the employer links between the pair (`role_clusters.clear_links_between`). The network search never re-links a pair the user rejected.

## State moves

What a `closed` verdict does depends on the job, and only when `discovery.closure_auto` is on:

| Job | Effect |
|---|---|
| Kept, state empty or `drafted` | Moves to `posting_closed`. The prior state is saved in `closed_prior_state`; a reopen restores it, unless the user moved the job in the meantime. |
| Kept and `applied` | A badge now. Moves to `filled` after the posting has been closed for 21 days (`FILLED_AFTER_DAYS`, `sweep_filled`, which also records the `filled` packet outcome). |
| Kept and `screen`, `interview` or `offer` | Never moves. A posting can close because they are interviewing you. |
| Not kept | Only `posting_closed_at` is stamped. Nothing archives them. |

A later `verified_open` verdict restores a closed job (`reopen_transition`). A manual state change beats a restore.

Every automated move writes an application event with source `closure`: `posting_closed`, `filled`, or a `note` on reopen. A reopen is a note, never the restored state as its own event, because an "Applied" dated today would claim an application that happened weeks ago. Shadow mode moves nothing, so it writes none. See [events and filing](events-and-filing.md).

## Manual verdicts

The job page has **Posting closed** and **Still open** buttons (`POST /jobs/{id}/posting/{closed|open}`). A manual verdict always applies, whatever `closure_auto` says, and automation never overturns it (`posting_check_manual=1`). The generic status route refuses to set `posting_closed` or `filled` directly.

## Shadow mode

`discovery.closure_auto` defaults to `false`: shadow mode. Verdicts and links are written, but `application_state` is not. After a few weeks of `blastjob-daily --closure-report` looking right, switch it on with:

```
blastjob setup set discovery.closure_auto true
```

`blastjob-daily --closure-report` is the shadow run. It records verdicts and links, forces no state transition whatever the config says, sends nothing, writes no `daily_runs` row, and prints the kept jobs with what each would become the first run after the switch (shadow-stamped closures fire in bulk that day). `blastjob-daily --dry-run` runs closure in shadow too. A shadow run reports a description it would revert (`would_revert_jd`) instead of reverting it.

## Which jobs are checked

The network phase covers kept jobs plus active surfaced and queued jobs, so you see a dead posting before keeping it. Jobs with a stored link are re-checked first, then jobs still needing a link; within each group the order is `posting_checked_at` (never-checked first), then stage (kept, queued, surfaced), then `alignment_score` descending. Stage and fit only break ties, so nothing starves.

Caps per run: 15 link searches and 40 probes (`MAX_LINK_PROBES_PER_RUN`). A row skipped by a cap is not stamped, so it sorts first next run.

Surfaced and Queue sink `closed` and `likely_closed` rows to the bottom, like Shortlist, and a `verified_open` row carries a quiet "open · checked <date>" badge. A row whose employer link is the user's manual one is skipped entirely; see [manual employer link](same-role.md#manual-employer-link).

## Careers-page links

For employers on no board we can list, the posting's own page can still prove it open or gone. The link is `page:<host>` (`core/page_links.py` for trust and checks, `core/page_fetch.py` for the network). It is only a link type, never a discovery source, and it is never authoritative.

**Finding one.** One Jina search for `"<company>" "<title>"`. The first 3 result URLs that are off `AGGREGATOR_HOSTS` (`linkedin.com` is on it) are fetched. A candidate counts only if its page holds exactly one JobPosting in its JSON-LD and that posting's canonical URL is the URL fetched. A multi-role page, or a self-reported URL we never requested, would not prove this role closed. An unreadable candidate aborts the whole search, because it might have been a second match. Exactly one trusted, unexpired posting across the candidates becomes the link; otherwise there is none.

**What makes a page trusted** (`trusted_employer_page`):

- The host is not an aggregator.
- An ATS host binds the page to the company: the page's own host, or a parsed `<form action>` host, never a substring. `<script src>`, `<link href>` and `<a href>` never count. The host must be a known ATS family whose tenant starts with the compacted company key.
- The page names ONE `hiringOrganization` (`hiringOrganization`), and it is this company.
- Exactly one JobPosting on the page matches the title (exact, or a single candidate at or above 0.92, with the region folded to a two-letter code).

**Checking one** (`page_links.check_page`, reached through `LinkDeps`, never `check_link`) re-verifies all of that on every run:

- A 404 or 410 from the same host, with no cross-host redirect, is `closed`.
- A 200 where the linked JobPosting is still trusted is `verified_open`, unless its `validThrough` is past, which is no evidence.
- Anything else, and any cross-host redirect even on a 200, is no evidence.

## Trust levels

Anyone can put an employer's ATS `<form action>` on a page, so a page bound only that way is not the employer's own (#23). Two trust levels (`core/employer_domains.py`, `core/page_links.py`) are recomputed at use time from the stored page URL and the current settings, so old rows follow the rule too.

- **FULL** when the page host is the company's own ATS tenant host, or is (a subdomain of) a domain listed for that company in `discovery.employer_domains`, keyed by `company_key`. Entries refuse job boards and aggregators, ATS hosts and shared hosts such as `github.io`.
- **LIMITED** when a page binds only through a `<form action>` ATS host on another domain. A limited page is no evidence either way (verdict `unverifiable`), its text is never adopted as an employer description, and neither its URL nor its host is ever shown. The apply link uses the ATS tenant root (`https://<tenant-host>/`), which `probe_page` reads off the fetched page once and stores; after that the page is not fetched again. A lookalike tenant shows nothing.

The fetched page URL lives in `employer_link_external_id`; `employer_link_url` is the URL shown. To adopt a page's description, the tenant must be exactly the company key (optionally plus digits), a stricter test than closure's prefix rule; that is covered in [employer data](employer-data.md).

## Safe fetching

A search-chosen URL is a request-forgery vector, so every page request goes through `page_fetch.safe_get`:

- Redirects are followed by hand, at most 5.
- Every hop is checked before it is requested: http or https only, not an aggregator, and the host resolves only to public addresses.
- The connection is made to the address that was validated. The Host header and the TLS SNI and certificate check keep the original name. There is no second DNS lookup, so no rebinding window; `trust_env=False`; no connection reuse.
- The body is capped at 2 MB.
- A refused hop is no page, never an error.

`allow_aggregators=True` is used only by `discovery/enrich.py`, for Adzuna and Indeed pages. It admits job boards but never LinkedIn (`LINKEDIN_HOSTS`). The rule is in [never contact LinkedIn](../truth-guarantees.md#never-contact-linkedin).

## Known risk

Accepted: with `closure_auto` on, one trusted 404 or 410 moves a kept, unapplied job straight to `posting_closed`. A false 410 from an employer's own ATS is unlikely, the move is reversible, and a later `verified_open` restores it (`reopen_transition`). Applied jobs only get a badge and wait 21 days; screen, interview and offer never move.

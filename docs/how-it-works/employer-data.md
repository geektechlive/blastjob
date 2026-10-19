# Employer data

**What this covers:** The data blastjob gathers about employers: full job descriptions taken from the employer's own posting to replace aggregator snippets, company profiles and compensation signals, and the people found for addressed outreach. It is for anyone changing `core/employer_jd.py`, `core/company.py`, `web/research.py` or `web/people.py`.

## Employer descriptions

Aggregator postings (`adzuna`, `linkedin`, `indeed`, the values in `employer_jd.AGGREGATOR_SOURCES`) usually arrive with a snippet of about 500 characters. Adzuna's API only ever returns a snippet, and its pages sit behind a human check that blastjob never tries to pass. So the full description is taken from the employer's own posting instead, and only from a match that the [closure](closure.md) trust rules already accept.

A row is a candidate (`employer_jd.candidates`) when all of these hold:

- Its source is an aggregator.
- Its stored description is under 600 characters (`MIN_FULL_JD_CHARS`) and was not pasted by you.
- It has no employer text yet.
- It is kept, or undecided and still active at the source. Archived rows never qualify.

`employer_jd.run_upgrade` works through these steps, cheapest first:

1. **Local revalidation** of descriptions already adopted from a posting blastjob holds. The proof is re-run and a miss reverts the text (see [Undo](#undo)). No network.
2. **Free copy** from a held authoritative board posting (`link_still_proof` passes, or a stored employer link points at it), full text only.
3. **Network lookup**, opt-in and request-metered (see [Cost limits](#cost-limits)): a stored link is re-verified and its text read; an unlinked row is searched with `closure_links.resolve_match`, and the link found is saved for closure.
4. **Re-verification** of adoptions proven only over the network (a posting blastjob does not hold), a few per run, oldest first. A refusal reverts the text; a clean check moves the row to the back of the queue.

What a candidate text must pass before it is adopted:

- **Fresh trust revalidation.** `closure_links.verified_description` re-checks, on current data, that the board still belongs to the company and that exactly this posting is the one confident title and region match. A posting id reused for another role is `title_drift`. Other refusals are `company_mismatch`, `gone` and `manual`. A user-supplied employer link is display-only and is never fetched or copied from.
- **Careers pages.** For a `page:` link, `page_links.page_description` re-runs every page trust check and returns that trusted posting's own text, never the first posting on the page. Only a full-trust page can supply text (see [trust levels](closure.md#trust-levels)). For adoption the page's ATS tenant must be the company key itself, optionally followed by digits; closure's own page links keep the looser prefix rule.
- **A plausible description.** `normalize_employer_jd` turns HTML into text, rejects anything under 600 characters (`short_text`) and caps the result at 60,000 characters. `looks_like_jd` then requires at least one word of the job title (stopwords such as "of" and "the" ignored) in the first 2,000 characters, otherwise the status is `not_a_jd`.
- **No rejected pairing.** A posting you marked "not the same role" for this job is never used as a source (`rejected`).

Network lookups skip agencies and rows with no company before any request is spent, because an agency's client is never named.

Every row ends with a status. The ones you will meet in a report:

| Status | Meaning |
|---|---|
| `upgraded`, `revised` | Text stored; `revised` means the held posting's text changed. |
| `would_upgrade`, `would_revise`, `would_revert` | The same outcomes in a run that writes nothing. |
| `reverted` | The proof behind an adopted text was gone and the old text came back. |
| `no_match` | Nothing trustworthy was found. Backed off for 14 days. |
| `transient` | A network or search error. Backed off 1, 2, 4, 8 days, capped at 14. |
| `short_text`, `not_a_jd` | A match was found but its text failed the plausibility checks. |
| `title_drift`, `company_mismatch`, `gone` | The fresh revalidation refused the match. |
| `sibling_not_full` | The held sibling has only a snippet, and no network step is running. |
| `no_free_copy` | A free-only run found nothing to copy. |
| `manual_link`, `rejected`, `no_proof` | Your manual link, your "different" verdict, or a local proof that no longer holds. |
| `agency`, `no_company`, `unscored`, `below_floor`, `applied_role`, `backoff`, `closure_searched` | Skipped before any request. See [Cost limits](#cost-limits). |
| `cap` | The request budget ran out. |
| `lost_race` | The row changed during the fetch; nothing was written. |
| `error` | An unexpected exception on that row; the run carried on. |

## Provenance

An adopted description records where it came from, so it can be shown, re-checked and undone. The columns on `jobs` (migrations 45 to 52):

- `jd_source` is `employer:<link source>`, for example `employer:greenhouse:acme`.
- `jd_source_ref` is the posting's id (a `page:` link stores its URL).
- `jd_fetched_at` is when the text was taken. Re-verification uses it to pick the oldest first.
- `jd_prior` holds the aggregator text the adoption replaced, kept for undo. A refused aggregator refresh updates it, so an undo restores the latest aggregator text, not a stale one.
- `jd_lookup_at`, `jd_lookup_status` (`found`, `no_match`, `transient`) and `jd_lookup_failures` drive the retry backoff described above. A `found` row is never looked up again unless its text is reverted.
- `score_stale` marks a kept row whose description changed under its score.

Descriptions have a quality class (`db/client.py`, `jd_class`): stub, snippet, full, employer, pasted. The class `JD_EMPLOYER` sits above full and below pasted, so no aggregator refresh replaces an employer text, and a text you paste always does. See [discovery](discovery.md#jd-quality-classes) for how refreshes compare classes.

A changed description means the old score no longer describes the job. An undecided row's score is cleared, so the next scoring pass picks it up. A kept row keeps its score but is flagged `score_stale`, and is re-scored on a following run. Each run re-scores the oldest ten stale kept rows (`rescore.stale_kept_ids`), so a long backlog drains over several days.

## Writes

An adoption is one compare-and-set. `set_employer_jd` takes a `JdSnapshot` read before the network call (text, company, title, review state, source state, and the employer link fields) and updates the row only if none of it moved and you have not pasted a description. The local proof is re-checked in the same `BEGIN IMMEDIATE` transaction. If you split, archive or edit the row while the fetch ran, the status is `lost_race` and nothing is written.

The first adoption moves the replaced text into `jd_prior`. A revision from the same source keeps the original `jd_prior`.

`set_employer_jd` and `revert_employer_jd` never commit. Their callers own the transaction, and `run_upgrade` commits before every network await so no write lock is held across a request.

Saving the employer link is best-effort and never gates the text: a row with a manual posting verdict cannot store a link, and a proven text still lands.

## Undo

A description adopted over a link is reverted, restoring `jd_prior`, when the link stops being proof:

- **A "not the same role" split.** `role_clusters.clear_links_between` drops each job's link to the other and reverts a description adopted from the other job's posting, both ways. The cockpit then re-scores the affected rows.
- **Closure drops the link.** `_drop_link` in `core/closure.py` reverts the text adopted over that very link. In a shadow closure run (`blastjob-daily --dry-run` or `--closure-report`) it only reports `would_revert_jd`.
- **Local revalidation each run.** The proof the adoption used is re-run: the same held posting still reached by the sibling or company-and-title route (`db_link`) or by a stored link that still passes `link_still_proof`, and the held posting still being this role on its current title and company. A held posting that was retitled or reused for another role is reverted, never "revised". If the held posting's text changed and is still a full description for this role, the new text is adopted as a revision.
- **Network re-verification.** Adoptions proven only over the network (page links, boards blastjob does not hold) are re-judged with `verified_description` in the network step, oldest first, ten per run (`REVERIFY_PER_RUN`). A refusal reverts. An error or blank text is no evidence and changes nothing.

When the employer-description step does the revert, it also clears the lookup record, so a later search may try again.

## Cost limits

Network work is metered in request units, not requests. `closure_links.RequestBudget` charges each outbound call its worst-case HTTP fan-out, and when a budget cannot cover the next call the run stops with `cap`. The weights (from `core/closure_links.py`): a company board probe 9 units, a Workday probe 7, a Dayforce probe 14, a careers-page search 19, a single page fetch 6, a board fetch 1 (a Dayforce listing is more). A lookup that uses the Jina-backed probes therefore costs about 31 units. Retries inside one request are not counted, and a cached board is still charged in full, so the figure over-counts rather than under-counts.

| Where | Budget |
|---|---|
| Daily run | `discovery.employer_jd_daily_requests`, default 300 (about ten Jina-backed lookups) |
| `blastjob-daily --employer-jd` | `--max-requests`, default 1000 |
| Cockpit button | 40 units, one lookup, with a 10-minute cooldown per job and at most two running at once |

Rules that decide who is looked up at all:

- Network lookups run for kept rows and for undecided rows scoring at least `discovery.employer_jd_min_score` (default 60). An unscored undecided row waits for its score. Agencies and empty company keys never qualify.
- A search runs once per run for identical (company, title, region) inputs and the answer is shared. Each row still verifies and compare-and-sets on its own, and an error is never reused.
- Daily and backfill runs skip undecided copies in a cluster that holds an application (`applied_role`), since they are hidden behind that application. No lookup is recorded, so a later split re-enables them. Kept rows and the cockpit button are never skipped for this reason.
- The backoff windows from [Provenance](#provenance) apply to daily runs. The backfill command and the cockpit button ignore them.

## In the daily run

Two steps, both in `daily_employer_jd.py`, both fail-soft: an exception rolls back, lands in the run's errors under `employer_jd`, and the run carries on.

- **Free copies** run after discovery and before matching, so a copied row is scored once, on the full text. They are local only: no request and no model call. They always run; there is no setting for them.
- **The network step** runs after closure, sharing closure's `default_deps()` so a board fetched by one is cached for the other. It skips rows closure already searched this run. Rows whose text changed or reverted, plus stale kept rows, are re-scored in one batch (`rescore.rescore_jobs`).

The network step is off until you run `blastjob setup set discovery.employer_jd_lookup true`. A dry run writes nothing in either step. A run that stored, reverted or failed on anything adds one line to the digest's notes. See [the daily run](daily.md#what-a-run-does) for where these steps sit in the sequence.

## In the cockpit

On a job whose description is an employer's, the description block says "full description from the employer's posting" with the board name (or the page host) and the fetch date, linked to the posting when blastjob knows its URL. On an eligible snippet job it offers a **Find the employer's posting** button, which posts to `/jobs/{id}/employer-jd`.

The route is CSRF and origin checked. It refuses a row that already has a full description, is not from an aggregator, is from an agency, or has no company. It also refuses when the job was looked up in the last ten minutes or two lookups are already running. The run is named `score-job`, so the global run panel shows it, and it is single-flight per job: a second click returns the running one, so a double-click cannot spend the budget twice. The lookup uses the 40-unit budget, ignores the score floor and the backoff, and re-scores what changed.

A packet built before the employer text arrived is flagged stale.

## Commands

`blastjob-daily --employer-jd-report [--min-score N] [--max-requests N]` runs the lookups and lists the descriptions it would store. It writes nothing, but it does spend requests. `blastjob-daily --employer-jd` stores them, then re-scores the changed rows and prints the count, the requests used and the model cost. Neither writes a `daily_runs` row or sends anything.

- `--min-score` defaults to `discovery.employer_jd_min_score`.
- `--max-requests` defaults to 1000 request units (`EMPLOYER_JD_BACKFILL_REQUESTS`).
- The backfill runs the network step whether or not `discovery.employer_jd_lookup` is on, and ignores the retry backoff.

## Company profiles and comp signals

A company workup is stored as data, so numbers stay comparable and queryable.

- `company_profiles` has one row per normalized company name (`company.company_key`, so "ACME WIDGET CORP, Inc." and "Acme Widget" share a row). Typed columns hold figures you compare across companies (employees, revenue, assets, net income and similar). JSON columns hold leadership, tech stack, recent events and sources. `extra` holds anything without a column yet.
- `comp_signals` has every compensation observation for a job. The kinds (`COMP_KINDS`) are `aggregator_estimate`, `posted_range`, `market_band`, `asked_minimum`, `employer_response`, `verbal_range`, `offer`, plus `current_salary` and `prior_salary`, which are not tied to a job. Notes are capped at 500 characters.
- `negotiated_floor(job)` is the highest minimum you asked for that an employer responded to without pushing back. It returns nothing if you never asked, if no employer has responded (an unanswered number typed into an application form is an ask, not a floor), or if any response note signals rejection ("pushback", "balked", "too high" and similar, after an explicit "no pushback" is set aside). The interview prep pack uses it as the figure to anchor on, which is why a missing agreement must read as missing.

In the cockpit, `/companies` lists the profiles, `/companies/{key}` shows one, and a kept job's page has a snapshot and a form for adding a signal.

**Research.** The **Research company** button on a kept job (**Refresh research** once a profile exists) runs `web/research.py` as a background run, single-flight per company:

1. Jina search over several query shapes, then up to four primary-looking pages read (`core/jina.py`, `MAX_PAGES`). LinkedIn results are dropped first ([why](../truth-guarantees.md#never-contact-linkedin)).
2. One model call fills the profile schema from that material only. The prompt forbids recalled knowledge, and a field it cannot support is null.
3. Output is checked. Sources and leadership entries are kept only if their URL is one of the fetched ones. Each single value (founded, headquarters, employees, revenue and the like) is kept only if it appears, as written, in the text of the page it cites (`core/cited_values.py`). `website` gets a scheme check instead of the allowlist.

Needs `JINA_API_KEY` in the secrets file.

**What research may overwrite.** On an existing profile, `company.refresh_research_facts` writes only the research-owned columns (`RESEARCH_OWNED`: the facts the web can re-prove), and only where the new value is non-empty, so a thinner re-run never erases a stored fact. It fills `strategy` and `summary` only while they are empty. It never touches `workup_path`, `culture_rating`, `culture_notes`, `risks`, `questions` or `extra`, which you curate. Because stored facts are never erased, the citation check stops new unverified values but does not purge one an earlier run stored.

The save is a compare-and-set on the `updated_at` read when the research started. If the profile changed meanwhile, nothing is saved and the run says so; run it again.

Long-form markdown workups live in the application folder, and the profile row points at them through `workup_path`. The path is stored in root form and resolved with `paths.locate`, because the folder may be filed into a bucket (see [events and filing](events-and-filing.md#stored-paths)).

## People and outreach

For a kept job, **Find people** runs `people.find_people`:

1. Jina web search (`web/people.py`): one query for the company's leaders in your target titles, one for recruiters, one for the leadership team. The first two are scoped to LinkedIn profiles through the search engine's `site:` operator. blastjob only reads result titles, URLs and snippets, and never requests a linkedin.com page.
2. A model call extracts people strictly from those results: name, title, role (`hiring_manager`, `recruiter`, `executive`, `peer`), the result URL and a one-line reason.
3. `people.parse_contacts` drops any entry whose URL is not one of the search results, and any duplicate URL. This is the one defense against an invented person. At most five are kept.

People are stored in `contacts` (migration 10) with the source URL, so you can verify them before writing. A re-search never duplicates a person or resets **contacted**: a (job, URL) pair already stored is left alone.

**Draft note to** a contact calls the outreach draft with that `contact_id`. The prompt addresses the person by first name and shapes the ask to their role: a recruiter gets a crisp fit summary and availability, a hiring manager a peer-level note. With no contact it opens "Dear Hiring Manager" and says it is unaddressed. Outreach is single-flight per job and contact. It is still draft only: you copy the text into LinkedIn or email yourself. It needs `JINA_API_KEY` for the search step, and the model call for the note.

The people list is separate from your own connections. For warm paths from your LinkedIn export, see [your network](network.md).

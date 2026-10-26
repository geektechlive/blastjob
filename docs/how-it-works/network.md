# Your network

**What this covers:** How your own LinkedIn data export becomes warm paths: what is imported and what is never kept, how names are matched, the alumni signal, how seniority is classified, and the optional title enrichment pass. It is for anyone changing `core/network.py`, `core/network_views.py` or `core/network_enrich.py`.

Everything except enrichment is deterministic SQL and regex: no model call and no network call. Enrichment is the one model step, and it never sees a person (see [Enrichment](#enrichment)).

## Importing the export

You download your data from LinkedIn's own export page and upload it on the network page (`/network`), which posts to `/network/import`. blastjob never scrapes LinkedIn and never contacts it. The cockpit accepts a ZIP of the full archive or the bare `Connections.csv`, and decides by the file's first bytes, not its extension. `network.import_archive` also accepts an unzipped folder.

| File in the archive | What blastjob keeps |
|---|---|
| `Connections.csv` | One row per connection in `network_contacts`: name, profile URL, email if shared, current company and position, connected date. Always imported first. |
| `Recommendations_Received.csv` | A flag (`recommended_me`) on the matching connection. |
| `messages.csv` | Per connection, only the date of the latest message and a message count. |
| `Company Follows.csv` | The company names you follow (`company_follows`), deduplicated by company key. |

File names are matched case-insensitively on the base name, at any depth, with spaces and underscores treated alike. A missing optional file is skipped silently, and unrecognized files are listed in the result.

**Message bodies are never stored or logged.** The `messages.csv` row model declares only the sender, recipient and date columns, so message text is never read into a model object. Each row is reduced in memory to a (connection, latest date, count) aggregate and discarded. Do not keep the row around "to optimize" anything.

Re-importing a newer export updates company, position and seniority by profile URL (or by lower-cased full name when the URL is blank, and only against rows that also have no URL, so two different people are never merged). It never overwrites notes you wrote by hand.

Size limits guard against decompression bombs: the upload cap is 400 MB, a single archive member is capped at 200 MB decompressed, and the whole archive at 400 MB. The caps are checked against bytes actually read, not the size the ZIP header declares. A member over a cap is skipped and listed as oversized, and the rest of the import goes ahead. Archive folders refuse symlinks that resolve outside the folder.

`Company Follows.csv` feeds one more view, `/network/follows`: followed companies the pipeline has never seen. It only presents candidates and never writes a board token to settings.

## Name matching

`Recommendations_Received.csv` and `messages.csv` carry names, not profile URLs, so the only join is the normalized full name (lower-cased, whitespace collapsed).

- **An ambiguous name credits nobody.** If two connections share a name, neither gets the recommendation flag or the message aggregate. Crediting every same-name connection once put the wrong person at the top of the warm list as someone who had "already vouched for you". A missing signal is better than a false one.
- **A name that matches no connection is skipped.** It is never inserted as a new contact.

The results report how many names were ambiguous or skipped.

## Alumni signal

`candidate.former_employers` lists the places you worked. A job at one of them is the strongest warm signal, and it is matched on the normalized company key (`company.company_key`, including `discovery.company_aliases` for legal-name versus brand-name differences). `network.alumni_employers` computes the set at call time from the candidate settings you pass in (the web layer passes the loaded config's `candidate` on every list and the job page), so a fresh install with an empty list and a filled-in one both behave without a restart.

Connections at a job's company are found by company key. `network.warm_paths` orders them: recommenders first, then executive, director, manager, individual contributor, then most recently messaged, then name. A VP who recommended you outranks a VP who never spoke to you. The job page shows them under "People you know here", each with the source URL from your export so you can verify before writing.

The daily digest uses the same count to move warm-path roles ahead of colder, higher-scoring ones; see [the daily run](daily.md#digest-contents).

Three more views are built from the same rows by `network_views`, none of which invent anyone:

- **Recruiter desk** (`/network/recruiters`): a connection is listed when the title rule matches (`is_recruiter`), when the enrichment labelled them `agency` or `exec_search` (so "Managing Partner" at a search firm appears), or when you marked them a recruiter. Groups are `exec_search` first (retained search matters most for an executive seat), then `agency`, then `in_house`, ranked by warmth within a group, with a flag when their employer has a live posting. The group is your label if you set one, else the enrichment label, else the firm-name keyword rule (only for a contact the enrichment has not labelled, or labelled `none`).
- **Your own label** (`POST /network/contacts/{id}/recruiter`, a select on every desk row, and **Mark a recruiter**, a search over all connections at `GET /network/contacts`): choose exec search, agency, in-house, "not a recruiter", or automatic. It is stored in `network_contacts.recruiter_override` (migration 56), wins over both the title rule and the enrichment label, and an import or a re-enrichment never touches it. The title reader can miss (a firm's president labelled `none`), which is why the override exists.
- **Dormant ties** (`/network/dormant`): executive and director connections you have actually messaged, ranked so a live opening at their employer wins, then by warmth.
- **Advocates**: everyone who recommended you, most recently messaged first.

`network_views.warmth_score` is a seniority bonus plus a capped message-volume term, scaled by recency that decays linearly to zero over two years. The weights are arbitrary but ordered, so they rank rather than measure.

## Seniority classification

`network.classify_seniority` is deterministic. It reads the position text, takes the longest matching keyword (ties go to the more senior tier) and falls back to individual contributor. Tiers are `exec`, `director`, `manager` and `ic`. Matching is on whole words, and each guard exists because a plain match went wrong on real data:

- A word-boundary `vp` cannot match inside "SVP", "EVP" or "AVP", so those are explicit keywords. Without them, senior executives were filed as individual contributors.
- A bare "owner" would promote every "Product Owner" (an Agile role) to executive, so that one phrase is excluded.
- "Partner" means executive at a law or consulting firm, but "Talent Acquisition Partner", "HR Business Partner" and similar titles are not executives. A recruiter would otherwise rank top of the intro list, which is backwards.

`network.is_recruiter` matches recruiter, talent acquisition, talent partner, executive search, headhunter and staffing titles.

## Recruiter status

Each recruiter-desk row shows a relationship status and a last touch (`core/recruiter_status.py`, columns from migrations 66 to 68 on `network_contacts`).

- **Status.** One of not contacted, reached out, spoke, active or do not contact. A status you set (`POST /network/contacts/{id}/recruiter-status`, `recruiter_status`) always wins; "automatic" clears it. Without one, the status comes from interaction facts only, never from how an ask ended: `spoke` when they answered any ask (replied, helped or declined) or the LinkedIn data shows messages; `reached_out` when asks exist but none was answered and there are no messages; `not_contacted` otherwise. An old answered ask therefore keeps `spoke` even when a newer ask is still waiting. The automatic status never yields active or do not contact; only you set those. Do not contact needs a reason of at most 120 characters (`recruiter_status_reason`); the reason is dropped when you choose another status.
- **Last touch.** The later of the newest `asked_on` over the contact's asks (dates validated by [outreach asks](#outreach-asks): ISO, never in the future) and the date part of `last_message_at`. The row says whether it came from "ask" or "LinkedIn message"; a tie reads as the ask. A recruiter with neither has no last touch.
- **Notes.** `POST /network/contacts/{id}/asks` logs a job-less ask of kind recruiter (channel, date, note) through the same `log_ask`, which now also rejects a job-less recruiter note to anyone who is not an effective recruiter. A job-less ask's status, follow-up date and note are changed from the desk row too. Both routes answer 404 for a contact who is not an effective recruiter, and sit behind the CSRF, Origin and Host checks.
- **Reminder.** A `spoke` or `active` recruiter is announced in the digest when the last touch is old enough ([daily](daily.md#recruiters-to-touch)). Nothing is sent to the recruiter.

## Outreach asks

A job page lists the people you know at the company (`network_views.warm_paths_for_job`) and the people found for it (`contacts`). Each gets a **Log ask** form that records an ask in `outreach_asks` (`core/outreach_asks.py`, routes in `web/routes_asks.py`). An ask is a record, not an application stage, and logging one sends nothing.

- **Fields.** Kind (intro, referral, recruiter, advice), channel, date asked (today by default, never in the future), a note of at most 300 characters, and a follow-up date that defaults to a week after the ask. A follow-up date before the ask date is rejected; a backfilled ask whose whole timeline is past is fine. Status is waiting, replied, helped, declined or no reply; a closing status clears the follow-up date.
- **One recipient.** Exactly one of a network contact or a job contact. `validate_recipient` is the single check, used by logging and by the draft route: a network contact tied to a job must be on that job's current warm path, a job contact must belong to the job. The route answers 400 when both ids are sent, even the same number.
- **Not about one job.** `job_id` may be empty for a network contact and kind recruiter or advice. The recruiter desk uses this for [notes to a recruiter](#recruiter-status).
- **Double submit.** Each form carries a one-use `request_token`; a replay returns the existing row and writes nothing.
- **Contacts.** Logging an ask to a job contact also sets `contacts.contacted_at` if it was blank.
- **Newest ask.** A person's latest ask is by date asked, then id, so an older ask entered later is not shown as the newest.
- **Reminders.** A due ask appears once in the daily digest ([daily](daily.md#asks-to-follow-up)).
- **Draft.** `POST /jobs/{id}/outreach` also takes `network_contact_id` and a required `kind`. The recipient block says the facts come from the LinkedIn connections export and holds name, position, company, and whether the person recommended you and when you last messaged, nothing else, plus a Request line for the kind of ask. The page is draft-only like every other draft.

## Enrichment

A regex cannot read "AVP Strategic Sales (Enterprise, Client Sales)", which is a senior sales person, and for a referral the function matters more than the tier. The optional enrichment pass (the **Read the titles** button on the network page, `/network/enrich`, `network_enrich.run_enrichment`) has a model classify titles: tier, function, recruiter kind, whether the title plausibly owns budget or headcount, and a suggested canonical company name.

Constraints that make it safe and cheap:

- **Titles only.** The model receives distinct title and company strings, never a name, URL or message history. A pair shared by many contacts is classified once, and the result is mapped back locally by string.
- **The deterministic classifier is the floor.** Where an enrichment verdict exists, every view uses it (warm-path order, the summary counts, the Dormant ties filter and the warmth score); otherwise they use `classify_seniority`. The finer tiers map back onto the four the cockpit ranks on.
- **`canonical_company` is a suggestion, never an identity join.** If the model mapped an ambiguous employer to the wrong brand, a real person would be joined to a target employer and the cockpit would recommend a warm introduction that does not exist. Only deterministic aliases or your own approval affect warm-path matching.
- **Resumable.** A pair is pending while any contact holding it has no enrichment stamp, so a re-import classifies only the new ones and a crash resumes where it stopped. Titles go in batches of 40; a batch that fails or returns nothing parseable is skipped and reported, and the run continues. A malformed model element is skipped, never written.
- **Unreadable means unknown.** The prompt tells the model to answer individual contributor, function "other" and an empty company rather than guess.

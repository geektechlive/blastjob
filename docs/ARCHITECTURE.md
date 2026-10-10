# blastjob architecture

`CLAUDE.md` is the working reference for conventions and commands. This is the
deeper one: how a job travels from a board to an interview, where the trust
boundaries sit, what the app does and does not guarantee about truth, and why
each of the load-bearing rules exists. Most of these rules were learned from a
real failure; the failure is described so the rule is not "simplified" away.

## What the app is for

It is the user's personal recruiter. Not a job board and not a resume tool: the
thing whose job is to get them interviews while staying factual. That framing
settles priority arguments, because a recruiter who fails step one is useless no
matter how polished the rest is:

1. **Know the whole market.** Miss no role they could take.
2. **Know the candidate, and advocate without inventing.**
3. **Work the network for warm introductions.** The real edge over a job board.
4. **Prepare them.**
5. **Chase.**

Coverage bugs therefore outrank UI work: a role that is never surfaced cannot be
applied to. The worst bug this app ever had was a location parser that silently
rejected most of the jobs it discovered, while the run log ("fetched N, scored M,
new K") looked healthy. Of the jobs wrongly rejected, the interesting point was
never that they were all good; it was that the user never got to decide.

**The web cockpit is the only surface.** A CLI may be a second way in, never the
only one; a capability with no route does not exist for the user. Shared `core/`
code is held to the standard a *web* caller needs.

**Draft-only, always.** Nothing is ever submitted, sent, posted or auto-applied.
At senior levels a few targeted, approved applications beat volume, and anything
that acts on the user's behalf against a job site risks their account. The same
reasoning rules out browser automation and unofficial APIs: LinkedIn and Indeed
job-alert emails are the sanctioned feed, and **no code path ever requests a
linkedin.com URL** (the closure and page-link code filter it before any fetch).

**No required API keys.** All LLM work can run through a logged-in `claude` CLI;
the ATS boards are public JSON; location matching is offline (no maps API).
Every key (Anthropic, OpenAI, Adzuna, Jina, Discord, IMAP) unlocks an optional
extra.

## The pipeline

```
   boards / aggregators / alert email        the user's LinkedIn export
              |                                          |
      discovery/*  ---> RawPosting                 core/network.py
              |            |                             |
       normalize.py --> upsert by (source, external_id)   |
              |         source-owned cols only           |
              v                                          v
          jobs table  <----------- warm paths ----- network_contacts
              |
      matching/pipeline.py
        geo.py        three-way: listed town / other state / UNKNOWN
        prefilter.py  configured drop terms: job FAMILIES, never title variants
        scorer.py     batched LLM alignment, reuses an unchanged JD's score
        gate.py       surfaced / queued / new
              |
        review in the cockpit  (Keep / Archive-with-reason / Unkeep)
              |
      core/build.py  -> resume + cover letter + sidecars in one folder
              |          build_runs.artifact_dir records WHERE
              v
      verify the packet (core/packet_gate.py) -> packet_report.json
      core/scoring.py -> fit_score.json  (groundedness + JD alignment)
              |
      web/fitscore.py -> the verdict, in the cockpit, before it is sent
              |
        apply -> track -> follow up (core/followup.py) -> closure checks
```

## Discovery

### Identity and refresh

A posting is `(source, external_id)`. A refresh touches **source-owned columns
only** and never the review or build state the user has set. Two rules learned
the hard way: an empty description means *presence only* and must not blank a
stored JD, and **a presence stub must not overwrite a field it does not know**.
The Workday adapter skips the detail fetch once a JD is stored and returns a stub
built from the listing payload; that stub used to carry the listing's
placeholder location ("5 Locations") and no remote flag, so every run
overwrote an accurate location and reset remote to false. Rows that hold a
placeholder now get exactly one detail fetch to self-repair.

`source` is namespaced per board (`workday:{tenant}`, `greenhouse:{token}`), so
one board's outage cannot stale-mark a sibling's postings. A source-wide
identity would have let one failed board make another board's jobs disappear.

Workday's storage identity is built from what the *listing* returns, so "fetch
detail only for unknown postings" can be decided without a detail fetch. An
identity that depended on a detail-only field would have duplicated a posting
the first time a detail fetch failed and later succeeded.

Dayforce needs no key but performs an anonymous CSRF handshake (fetch a token,
echo it in a header). That is the public search box's own anti-CSRF step, not a
login. Its JD ships in the listing, so there is no detail fetch and no stub.

### Snapshot vs incremental

An adapter that returns everything currently open is a *snapshot* and its absent
ids can be stale-marked. An adapter that returns one query window (Adzuna's
first page, Workday's page cap per search term, alert email, a search-engine
result set) is **not**, and ageing there is by `last_seen` over multiple runs
with a per-source TTL. Getting this wrong in either direction is harmful: as a
snapshot it marks live roles gone every day (a relevance-ranked, rotating window
produced a steady stream of fake "new" and fake "gone" rows), and as pure
incremental it keeps closed roles alive forever.

### Alert email

Alert mail is read over IMAP with an app password whose env-var *name* is in
settings and whose value is in the secrets file. Each message is recorded in a
ledger keyed by Message-ID (a bare UID watermark breaks when a mailbox changes
and either skips or replays mail). A recognized alert that parses to zero
postings is a parser failure, not success: it logs a warning and saves the body
to `mail-debug/` so the layout can be fixed, instead of being marked done and
lost.

LinkedIn's whole-card layout wraps title, company and location in one link with
no delimiter between title and company. A split is applied only on structure (a
company already known to the DB, or the guest page confirming it); a split
guessed from title vocabulary is never applied automatically, because a company
whose name ends in a title word would be split wrongly with full confidence.

### Recruiter replies

The reply scanner reads the user's *personal* mailbox, which is often a
different account from the alert feed. Pointing it at the alert mailbox made the
first version structurally unable to see a reply. A sender must match an
applied-to job at the header (verbatim address in the job's notes, or the
sender's domain equal to the company key), so unrelated personal mail is never
body-fetched. It surfaces only; it never drafts or sends. A dry run never stamps
the ledger, because stamping would mark a reply seen and the real run would
never surface it.

### Retained search firms and other low-yield feeds

Senior roles often move through retained search, which rarely posts to ATS
boards. The search-firm adapter queries firm sites through a web search API on a
weekly cadence. Expect low yield: most firms do not publish assignments in a
searchable way, and practice or marketing pages are false positives that need a
filter.

### Finding boards

`python -m blastjob.discovery.resolve "<Company>"` probes Workday, Dayforce,
Greenhouse, Lever and Ashby. Web-search candidate extraction is noisy: **open a
sample posting and check its location before trusting a hit.** A board token
once pointed at an unrelated company with a similar name and delivered dozens of
irrelevant postings a day; the `/funnel` dead-board check (ten or more postings,
none ever near the threshold) now names that case automatically.

## Matching

Cheap to expensive: geo filter, title prefilter, batched LLM alignment score,
gate. Matching makes **no LLM call until the candidate profile is configured**
(`candidate.target_titles`), so a fresh install cannot spend money scoring
against nobody.

### Geo is three-way

Each location is a listed town (measured from home against the commute and
extended radii), an affirmative other state or country (far), or UNKNOWN (review
queue). **An unplaceable location is never a hard reject.** The original bug
tokenized on whitespace, so a location written `USA-XX-City` hid its state code
and was declared out of state; worded "Remote"/"Virtual" locations and
multi-location placeholders were rejected the same way, and the rejection reset
the row so the next run rejected it again, forever. Tokenizing is now
delimiter-agnostic, with an address-word guard so a street name that happens to
be a state name is not that state.

Adding a town to the table can only move a job from UNKNOWN to a measured
distance. Add only towns the user would consider: a far town with a centroid
becomes a new hard reject, while the same town left unlisted lands in the
review queue. That asymmetry is deliberate.

### Prefilter drops families, not variants

The deterministic prefilter removes whole job *families* the user never wants
(configured drop terms). It never tries to judge title *variants* ("Head of",
"Lead", "Principal"), because that judgement is exactly what the LLM scorer is
for, and a regex that guesses wrong drops a real fit silently.

### Scoring and the gate

The alignment rubric is written for the user's seniority: a plain rubric
over-penalizes senior candidates for missing hands-on tool names that the role
does not actually need from them. The default threshold is 78, chosen because
genuine market fits landed in the high 70s; it is **uncalibrated for a new
profile**. A visible borderline band (threshold minus 5 up to the threshold) goes
to the review queue. Five was measured, not guessed: it is the observed range of
the scorer's run-to-run variance on the same posting.

**Unknown comp surfaces, badged; it is never a reject.** Senior roles rarely
publish pay, so rejecting unknown comp would empty the list. Only a figure the
employer actually posted, below the user's floor, may hard-reject.

Archive takes a **reason code** (`level`, `domain`, `geo`, `comp`, `company`,
`other`) plus a note, not free text alone. `level`/`domain` archives are fed back
to the scorer as calibration examples (the most recent 15); `company`
auto-archives future postings from that employer before any LLM call; `geo` and
`comp` are already deterministic gates. Free-text reasons in a prompt are an
unversioned policy change ("too far this week" would quietly depress good roles).

Matching writes are compare-and-swap on `review_state` (only from `new`,
`surfaced`, `queued`): a Keep or Archive clicked while a scoring batch is in
flight always wins over the score that lands afterwards.

### Two scores, never confused

The pre-generation **alignment score** says whether a role fits the user. The
post-generation **groundedness score** (`FitScore`) says whether a generated
resume is supported by the work history. An earlier design used the second as a
fit signal, which measures hallucination, not fit. The JD-overlap number in the
fit report measures phrasing overlap and is labelled as such.

## Review and the pipeline stages

**A cluster has one review state**: kept beats archived beats machine states.
The bug that produced this rule: a kept posting went stale, an active sibling in
the same cluster crossed the browse floor after a re-score, and the role
reappeared with Keep / Archive buttons, so a role the user had kept looked
unkept (and clicking Keep would have made a second application). A decided
cluster drops out of the review lists, and the `+N sources` badge counts every
member, including ones the current query excludes.

Stages are exclusive: every job is on at most one stage page, and every header
count is derived by calling the same list function the page renders, so a badge
can never disagree with the list beneath it. Every list sorts best fit first;
stage only breaks ties.

### Same role, different route

One job shows up several times wearing different hats: an aggregator syndicating
one requisition to many cities, an agency reposting an employer's role, a repost
of a role already applied to. All title comparisons go through ONE normalizer.

- **Auto-cluster** only on narrow, structural evidence (same company key, same
  normalized title, and either both non-authoritative within a short window or
  different sources in the same metro). Two requisitions on one authoritative
  board never merge, because a board posting in a cluster becomes closure proof
  for its siblings. Agencies and aggregators never auto-merge.
- **A repost of an application in progress joins it** when exactly one side is an
  in-progress application, the other has no application history, both are
  non-authoritative, and the metro matches strictly. A same-source repost weeks
  later once surfaced as a "new" role and was nearly applied to twice. A *closed*
  application's repost is never merged (it may be a new requisition, and merging
  would bury it in Closed); it is a suggestion instead.
- **Agency to employer is a suggestion with evidence, never a merge.** Only the
  user links two postings. An agency's client is never named beyond "possibly
  the same role as <a posting already in the DB>". A "not the same role" verdict
  is never overturned by the fingerprint, the daily run or a backfill.
- **A 'same' merge makes one application**, atomically: merge, decision and
  reconcile run in one transaction; the member furthest along survives; every
  other application is cleared on both its state and its applied date and
  archived as a duplicate. A failed compare-and-swap rolls back everything.
- **company_key** handles the legal-vs-brand split between a LinkedIn company
  field and a job board ("The Example Company" vs "Example"), and drops a
  trailing division segment, but never collapses "Group", "Holdings" or "Brands"
  words that distinguish real companies. Stored keys move only through a
  hand-run rekey that reports conflicts.

### One apply link per role

The employer's own board posting first, then a board link closure found, then
an employer careers page or a manual link, then LinkedIn, Indeed, Adzuna, other,
and an agency last. Every URL goes through `web/urls.safe_external_url`.

## Posting closure: only proof moves state

A posting is `closed` only when a source that can prove absence says so.

- An authoritative own source (Workday, Greenhouse, Lever, Ashby, Dayforce) is
  `closed` once stale, else `verified_open`.
- Any other job needs an **employer link**: a cluster sibling in the strict same
  metro, a company and title match already in the DB whose lifetime overlaps,
  or a board found by the resolver, linked only on an exact normalized title or
  a single strong candidate on a board whose tenant IS the company key.
  Intermediaries are `unverifiable`, with no request made.
- Adzuna ageing out with no link is `likely_closed`: a badge, never a move.
- A failed probe is no evidence and keeps the prior verdict. An ambiguous link is
  no link. A manual verdict or manual state change always beats automation.
- Kept but unapplied jobs move to `posting_closed` (reversible; reopen restores
  the prior state unless the user moved it); applied jobs get a badge and move to
  `filled` only after 21 days; screen, interview and offer never move.
- **Shadow mode is the default** (`discovery.closure_auto = false`): verdicts and
  links are written, application state is not, until the user turns it on.

**Employer careers pages (`page:` links)** are the last fallback, and the trust
test is strict because a page can lie: the host is not an aggregator; an ATS host
(the page host or a parsed `<form action>` host, never a script or link tag,
never a substring) is a known ATS family whose tenant starts with the company
key; the page names one hiring organization and it is this company; and exactly
one JobPosting matches the title (two plausible matches mean we do not know).
Re-checking a page link re-verifies company, title and region. A 404/410 counts
only from the same host with no cross-host redirect. Every fetch follows
redirects by hand, checks every hop before requesting it (http/https only, not
an aggregator, public addresses only), and caps the body, because a fetched URL
chosen by a search engine is an SSRF vector.

**A manual employer link is display-only.** It is the preferred apply link and is
never closure evidence: a typo or a dead page must not "prove" a live job closed.

## Trust boundaries

Everything below is untrusted until proven otherwise. The app's hard rule is that
**a fabrication is worse than a gap**.

| Input | Trust | Defence |
|---|---|---|
| Board JSON / job text | untrusted data | Pydantic at the boundary; rendered through Jinja autoescaping; fenced in prompts, never followed as instruction |
| Any URL reaching an `href` | untrusted | `web/urls.safe_external_url`: http/https only, control characters stripped before the scheme check |
| LLM-authored people or sources | untrusted | allowlisted to URLs actually fetched (`research.parse_profile`, `people.parse_contacts`): no source URL, no entry |
| Company research | untrusted | distilled in an isolated call into span-verified facts that carry polarity; the cover letter consumes those facts, never the raw text |
| The user's LinkedIn export | the user's own data | never scraped; message bodies are never stored, only who and when |
| Titles sent for enrichment | the user's data, minimised | **distinct title/company strings only, never a name, URL or message history** |
| Model comp figures | a guess | provenance `inferred`, magnitude-checked, may never hard-reject |
| Model canonical company | a suggestion | stored, but never used as an identity join |
| Settings that reference secrets | names only | mail, Discord, Jina and Adzuna fields hold env-var NAMES; values live in the secrets file; the form rejects anything not name-shaped, and presence renders as a boolean, never a value, length or prefix |

`base.html` carries the per-launch CSRF token on `<body>`, which is why a
`javascript:` href would be full account takeover rather than a nuisance. The
cockpit has **no authentication**. It binds 127.0.0.1 by default; LAN access is a
supported opt-in (`web.host`), and that makes closing these properly matter more,
not less. Reveal-in-Finder is gated on `request.client.host` being loopback,
because the Origin check accepts private addresses and cannot tell the host
machine from another device on the LAN.

### The network

The user's own LinkedIn data export (connections, recommendations received,
message metadata, company follows) is imported, never scraped. **Message bodies
are never stored or logged**; only the counterparty, the most recent date and a
count survive parsing. Recommendations and messages are matched to connections
by normalized name, and **an ambiguous name credits nobody**: crediting every
contact who shares a name once presented the wrong person as a top warm
introduction with a fabricated "already vouched for you". A name that matches no
connection is skipped, never inserted. Seniority classification is
deterministic, and word boundaries matter: `\bvp\b` misses SVP/EVP, and a bare
"owner" promotes every Product Owner to executive.

## Factuality guarantees

What the app actually promises, stated precisely so nobody over-trusts it:

- **Groundedness is model-assisted review, not a guarantee.** Every claim is
  checked by a model, and then each `evidence_quote` is verified in Python to be
  an exact substring of `work_history.md`, so a hallucinated quote cannot mark a
  fabricated claim grounded. An empty quote supports nothing. Below the floor a
  packet is badged *needs review*; it is never blocked, because the decision is
  the user's. **The floor (85) is uncalibrated** and the UI says so. Hand-reviewing
  about twenty claims would turn it into a measurement.
- The measured failure shape is **JD terms imported into the summary and skills
  where the history says something narrower**, industry re-framing, and
  JD-borrowed qualifiers ("multi-site", "global team").
- **What that layer cannot catch**, and which is therefore handled elsewhere:
  derived figures (a total silently summed from several true numbers) and
  completion-tense drift where the cited quote is genuine.
- **Numbers in a prep pack** are marked `[confirm]` unless they match a trusted
  figure as a **whole token**: a substring test cleared `$150` against
  `$150,000` and `3%` against `13%`.
- **Comp provenance is a trust level**, not a format: only `posted` is the
  employer's published number, and only `posted` may hard-reject a role. An LLM's
  reading of a JD was once relabelled `posted`, so a guess could be quoted to a
  recruiter and silently reject a real opening. Where a figure's scale is
  ambiguous ("180" and "240" from "$180K - $240K", or an hourly range) the app
  returns **no signal** rather than a number: a missing salary is recoverable and
  a wrong one is not.
- **Credentials are quoted, never paraphrased.** Coursework is not the degree,
  acting or interim is not the permanent title, and a certification in progress
  is not a certification; each would be an upgrade. A verification report once
  upgraded a correctly stated credential: the layer whose job is factuality was
  the one strengthening the claim.
- **Draft-only, always.** Nothing is ever sent or auto-applied.

## Packet verification

Every build verifies the packet it just produced and records the verdict in
`{role-slug}.packet_report.json` (`core/packet_report.py` is the vocabulary,
`core/packet_gate.py` decides).

- **Verify the artifact, not the markdown.** The ATS receives the `.docx`, so the
  checks run on text read back out of the generated file (contact block, one
  email and phone, role/title/date association, banned characters, encoding,
  size, page estimate). Markdown checks are only a cheap pre-pass.
- **Every position keeps its own title and dates.** Collapsing a promotion into
  one stacked heading was rejected: parsers cannot tell which title held which
  years, and it can read as years at the senior title. The fix for empty roles
  is at least one bullet per position, with an early-career tail exemption.
- **FATAL is calibrated to the boards actually applied through**, not to one
  parser vendor whose own docs call its codes risk signals. Fatal means banned
  characters (a logged ATS rejection), encoding damage, a recent role with no
  bullets, a total that does not reconcile, or a cover-letter claim the research
  contradicts. Vendor-derived rules are minor. A checker that cries fatal on
  compliant formatting teaches the user to ignore it, which is worse than no
  checker.
- **A low groundedness score never blocks.** It is uncalibrated and fires on
  honest stretch applications; it produces a loud, specific warning listing the
  unsupported claims instead. Mechanical fatals do gate: a failed packet is
  written to `quarantine/`, not beside sendable files, and acknowledging it
  promotes an immutable copy. Otherwise "not ready" is just a badge next to a
  working download.
- **No computed totals.** A total may appear only if that exact figure is
  verbatim in `work_history.md`. "The sum equals the named addends" would have
  passed the motivating defect, where a stated savings total was one true figure
  plus an unrelated one: arithmetic equality says nothing about whether two
  figures share a metric, period or owner.
- **Research is a trust boundary.** Scraped text is distilled in an isolated call
  (no work history in it) into facts with a source URL, an exact span and a
  polarity. "Considered entering a market and abandoned it" cannot become
  "serves that market". Calling raw text "untrusted" in a prompt is not a control.
- **The cover letter is scored too**: claims about the candidate against the work
  history, claims about the employer against the research facts. It is new and
  uncalibrated and over-flags; treat its number as a prompt to read.
- **The third bucket exists** (`core/coverage_gap.py`): requirements the history
  proves that the resume never used. It is read-only, and the only automatic
  remedy is inserting the user's own sentence verbatim. An LLM repair would turn
  "piloted a scheduling tool" into "led the scheduling rollout", attach the
  genuine quote, pass every check, and lie. "The aggregate score did not drop" is
  not a guardrail either: a repair can drop one weak claim and add one invented
  one.
- **Prose checks are advisory.** Thresholds are relative to the user's own
  writing (no sourced universal threshold exists); job titles, company names,
  certifications and required domain phrases are exempt from JD-overlap checks;
  corpus-derived "AI vocabulary" is informational only. The app does not optimize
  against AI detectors (they false-flag non-native writers and are not what a
  recruiter uses) and does not simulate an ATS pass/fail score.
- **Do not encode folklore.** Popular statistics ("75% of resumes are
  auto-rejected by ATS"), fixed bullet counts, filename conventions and
  board-specific formatting rules with no primary documentation were checked and
  rejected. A rule must trace to a primary source or a logged failure.
- **As-sent attribution.** When a job is marked applied, hashes of the resume,
  cover letter and JD are captured idempotently, so a later rebuild cannot
  reattribute an outcome to a document that was never sent. Reporting is
  descriptive only (what was sent, where each application stopped); **no style
  ranking at any sample size**, because seniority, referrals, timing and company
  dominate style.

## Application events and artifacts

`application_events` is the job's timeline. A state change and its event are
written in one transaction; setting the state a job already holds is not a
change and writes nothing. "Applied" is recorded only by the move that stamps
the applied date; a reopen is a note, because an "Applied" event dated today
would claim an application that never happened. Only manual events can be
deleted. Backfilled events say they are reconstructed.

`artifacts` indexes every file by absolute path. `artifacts.role_files` is the
only rule for "this role's files in a packet folder"; it never globs a shared
company-day folder. Registration hooks are best-effort and never change a
build's status. **Two output layouts exist on disk**; always read the stored
`build_runs.artifact_dir` and never recompute a path from today's slugs. A file
links to the download route only when that route's resolver would serve exactly
that path.

## Daily run and notifications

The daily run calls the same `run_discovery` / `run_matching` the cockpit does,
then closure and the reply scan, then sends one digest (Discord, stdout or off,
per `notify.mode`). It takes an online SQLite backup first, every time. A `PAUSE`
file in the data dir is the kill switch.

Delivery is **at-least-once by design**: `notified_at` is stamped only after the
channel accepts the message, and only on **exactly what was rendered**, so
overflow past the item cut leads the next morning rather than being buried. The
posting URL comes before the cockpit link because the digest is usually read on
a phone, where `127.0.0.1` is the wrong machine. Quiet days send nothing except a
weekly heartbeat, so silence is distinguishable from breakage.

## Observability

`/funnel` places every job in the stage it is actually in, recomputed from the
pipeline's own functions rather than a stored reason, so it cannot drift. It
exists because the run summary counted geo and prefilter rejections and threw
the counts away. It also flags boards with ten or more postings that have never
once reached the threshold.

`daily_runs` records each autopilot run; `blastjob-daily --status` prints the
last one.

## LLM layer

### Prompt caching

Three cache breakpoints per resume generation call: the system prompt
(constant), `work_history.md` (changes only after ingest), and the template.
**Never interpolate per-call data into a cached block**; the job description and
research go in a trailing uncached block. The candidate profile text is constant
per install, so it may live in a system prompt. A cached block is only reusable
under an identical prefix: two calls with different system prompts cannot share
a "warm" work-history block, and paying the cache-write premium for a block that
can never be read is pure cost.

### The claude CLI provider

`claude -p` is a full agent, not a completion endpoint. The provider hardens it
so it behaves as a plain model: the prompt goes in on stdin (work-history
frontmatter begins with `---`, which argv parses as a flag; stdin also dodges
argument-length limits), the caller's system prompt replaces the default plus an
output-only rule (otherwise it narrates), tools are denied, dynamic sections are
excluded, it runs in a neutral directory, and stdout noise is filtered.

## Data

- Data dir: `BLASTJOB_DATA_DIR`, default `~/.blastjob`. This is the only setting
  not stored in the database.
- Settings: the `settings` table in `<data_dir>/blastjob.db`, one row per dotted
  key with a JSON value. A legacy `config.toml` is imported once and left in place.
  Runtime settings (company aliases, agency names, alert-subject words, the secrets
  path) are re-applied on every `load_config()`, so an edit made while a daily run
  is in flight can take effect mid-run: settings are not transactionally isolated
  from a running job.
- State: `blastjob.db` (SQLite, WAL) plus `work_history.md`, `templates/`, logs
  and `backups/`; an online backup is taken before every daily run.
- Output: `paths.output_dir`, default `~/Documents/blastjob/`.
- Secrets: the file at `paths.secrets_file` (default `<data_dir>/secrets.env`),
  never in the repo and never in settings.

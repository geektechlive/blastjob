# Truth guarantees

This page states what blastjob promises about facts, and what it does not, precisely
enough that nobody over-trusts it. It also covers how every packet is verified before you
see it. It is the canonical statement of the anti-fabrication rules: anyone changing the
tailoring, scoring, research, closure or same-role code reads it first and obeys it, and
anyone sending a packet can use it to decide how far to trust one.

Each rule below says how it is enforced. There are three kinds of enforcement, and they
are not equally strong:

- **Deterministic check.** Code verifies it on every run (the function is named). This is a
  guarantee, within the limits the rule states.
- **Prompt rule.** The instruction is in the model's system prompt and nothing checks the
  output for it. The model usually obeys; nothing proves it did.
- **Model-assisted review.** A second model call checks the first, and Python re-checks
  what it can. This narrows the hole, it does not close it, and its scores are
  uncalibrated.

## Core principle

**A fabrication is worse than a gap.** Everything that enters the app (board feeds, job
text, alert mail, fetched pages, model output) is untrusted until proven otherwise. When a
fact cannot be verified, the app shows nothing, "unknown", or a visible warning. It never
guesses.

Three rules follow from that and apply everywhere:

- **A model never upgrades a claim on its own word.** Verification may only downgrade, or
  upgrade when Python itself finds verbatim evidence.
- **A model's canonical company name is a suggestion, never an identity join.** The network
  enrichment pass stores it (`core/network_enrich.py`) but warm-path matching uses only
  deterministic keys and the user's own aliases. A wrong brand mapping would otherwise
  invent a warm introduction.
- **A check that did not run is never an all-clear.** See
  [Packet verification](#packet-verification).

Contributors: read this page (`docs/truth-guarantees.md`) before touching the tailor,
scoring, research, closure or same-role paths. The mechanics live in
[Tailoring](how-it-works/tailoring.md) and [The LLM layer](how-it-works/llm.md).

## Trust boundaries

| Input | Trust | Defence |
|---|---|---|
| Board JSON, job text, alert mail | untrusted data | Pydantic at the boundary; rendered through Jinja autoescaping. In scoring and prep prompts the job text is fenced (`<JOB>`, `<MATERIAL>`) and the fence tag is defanged; in the resume, letter, refine and fit-score calls it is plain text under a heading, constrained only by prompt rules |
| Any URL reaching an `href` | untrusted | `urls.safe_external_url` (see [Safe links](#safe-links)) |
| Model-authored people or sources | untrusted | allowlisted to URLs actually fetched (see [Never invent a person](#never-invent-a-person)) |
| Company research | untrusted | anchored to the employer, every fact span-verified against the page it cites, distilled in an isolated call (see [Research is a trust boundary](#research-is-a-trust-boundary)) |
| The user's LinkedIn export | the user's own data | imported, never scraped; message bodies are never stored, only who, when and how many |
| Titles sent for enrichment | the user's data, minimised | distinct title and company strings only, never a name, URL or message history (`core/network_enrich.py`) |
| Model comp figures | a guess | provenance `inferred`, magnitude-checked, may never hard-reject (see [Comp provenance](#comp-provenance)) |
| Model canonical company | a suggestion | stored, never used as an identity join |
| Settings that reference secrets | names only | settings hold env-var NAMES; values live in the secrets file; the mail form rejects anything not name-shaped, and presence renders as a boolean, never a value, length or prefix ([Settings and secrets](operations/settings-and-secrets.md)) |

## Groundedness

**Rule: groundedness is model-assisted review, not a guarantee.** The fit-score call
(`scoring.score_resume`) has a model list the resume's claims, each with an
`evidence_quote` from the work history. `scoring.verify_claims` then checks in Python that
each quote is an exact (whitespace-insensitive) substring of `work_history.md`. A
hallucinated quote therefore cannot mark a fabricated claim grounded, and a quote shorter than
16 characters (after whitespace is collapsed) supports nothing, since "led" or "AWS" sits in
almost any history (`scoring._quote_supported`, `scoring.MIN_SPAN_CHARS`, shared with research
fact spans). An empty quote is the shortest case. A model `null` in any fit-score field becomes
that field's default (`models/fit_score.py`), so a null quote is an empty quote and fails.

Employment dates are checked deterministically rather than left to the model
(`resume_dates.verify`, called from `scoring.parse_score`): a date range on the resume that
contradicts the work history becomes an ungrounded claim.

What it cannot catch, and why the rule says "review":

- **Only claims the model listed are checked.** A claim the scorer never lists is never
  verified.
- **A genuine quote can carry a stronger claim.** Substring evidence is not entailment.
  Completion-tense drift ("began work to consolidate" cited for "consolidated") passes.
- **Derived figures pass this layer.** A total summed from several true numbers has true
  quotes behind each part. That is caught by [no computed totals](#packet-verification)
  instead.

The measured failure shape is JD terms imported into the summary and skills where the
history says something narrower, industry re-framing of a past employer, and qualifiers
borrowed from the posting ("multi-site", "global team"). The resume prompts forbid each of
these (`prompts._TRUTH_BOUNDARY`, a prompt rule).

**Rule: the floor is uncalibrated and never blocks.** Below `GROUNDEDNESS_FLOOR_DEFAULT`
(85, in `core/scoring.py`) a packet is badged "needs review" in the cockpit
(`web/fitscore.py`). It compares groundedness, not the overall score, and it is never a
block: the gate turns it into a specific READY-with-reasons listing the unsupported claims
(`packet_gate.gate`). The number was chosen by inspection and never validated against a
human-reviewed claim set; any UI that shows it must say so. Hand-reviewing about twenty
claims would turn it into a measurement.

## Comp provenance

**Rule: comp provenance is a trust level, not a format.** Only `posted` is the employer's
published figure, and only a `posted` figure below the floor may hard-reject a role
(`gate.decide`, comparing against `VERIFIED_COMP_PROVENANCE`). An `inferred` figure (the
model's reading of the JD) below the floor sends the role to the review queue, never the
bin. Unknown comp surfaces the role, badged. Enforced by a deterministic check.

Why: a model's reading of a JD was once relabelled `posted`, so a guess could be quoted to a
recruiter and silently reject a real opening. Gate details are in
[Matching](how-it-works/matching.md#comp-floor).

**Rule: an ambiguous magnitude is no signal.** `scorer.sanitize_comp` checks the pair of
model-reported bounds as a whole:

- both bounds between $10,000 and $5,000,000: kept as annual figures;
- both between 100 and 999: read as thousands shorthand ("180" for $180K) and scaled by 1,000;
- anything else returns no signal: an hourly-looking figure (1-99), the ambiguous
  1,000-9,999 band, an implausible magnitude, bounds on different scales (85 and 110 could
  be "$85-110/hr" or "$85K-$110K"), or an out-of-order pair.

A wrong salary is worse than a missing one: a missing figure surfaces the role badged, a
wrong one buries it or gets quoted in a negotiation.

## Credentials

**Rule: credentials are quoted, never paraphrased.** Coursework toward a degree is not the
degree, acting or interim is not the permanent title, and a certification in progress is not
a certification. Each would be an upgrade.

Enforcement is a **prompt rule only**. The resume, cover-letter and refine prompts carry it
(`prompts._TRUTH_BOUNDARY`), and the fit-score and cover-letter scorer prompts carry
`prompts._CREDENTIAL_RULE`, which tells the reviewer to mark a strengthened credential
ungrounded. No deterministic check compares credentials. A verification report once upgraded
a correctly stated credential: the layer whose job is factuality was the one strengthening
the claim, which is why the reviewer prompt has the rule too.

The setup interview adds one more example for the user correcting their own history: a
pending application is not a granted one ([SETUP.md](../SETUP.md)). Nothing the app writes
can be more accurate than `work_history.md`.

## Prep-pack numbers

**Rule: a prep-pack number is marked `[confirm]` unless it matches a trusted figure as a
whole token, never a substring.** `mark_unverified_numbers` (`core/prep.py`) marks money, percentages,
scaled magnitudes ("12 million"), comma-grouped counts and bare numbers of four or more
digits (which covers years). Two- and three-digit bare integers are left alone on purpose,
so a 30/60/90 plan does not fill with markers. A substring test once cleared `$150` against
`$150,000` and `3%` against `13%`. Enforced by a deterministic check.

Percent forms (`30%`, `30 percent`, `30 per cent`, `30 pct`, `thirty percent`) share one key
in both the prep pack and the packet figure check (`core/percent_figures.py`), so a
reformatted true figure is verified and an invented one is flagged whichever way it is
spelled. How the trusted text is assembled is in
[Tailoring](how-it-works/tailoring.md#interview-prep-pack).

## Never invent a person

**Rule: model-authored contacts and sources are allowlisted to URLs actually fetched.** The
Find people pass (`people.parse_contacts`) and the company Research button
(`research.parse_profile`) drop any person or source whose URL is not one the app fetched:
no source URL, no entry. Every list that renders as a link is allowlisted. This is the one
defence against an invented person, and it is a deterministic check.

In the network import, an ambiguous name credits nobody: crediting every contact who shares a
name once presented the wrong person as a warm introduction who had "already vouched for
you". See [Your network](how-it-works/network.md#name-matching).

## Research is a trust boundary

**Rule: research text never shares a context window with the work history, and the resume
and the cover letter consume verified facts, never raw research.** Scraped or recalled text about an
employer is distilled in an isolated call whose context holds the research and nothing else
(`core/research_facts.py`: `research_facts.distill_research`, and the page-research call
in `core/packet_research.py`). Each fact carries a source span, a polarity (affirmed, denied,
considered, abandoned, unknown) and a provenance. A fact survives only if its span is
verbatim in the text it cites (`research_facts.span_verifies`, minimum 16 characters).
Calling raw text "untrusted" inside a prompt is not a control; this structure is.

What the resume and the letter see (`research_facts.facts_block`) is each fact's verbatim span with its
polarity, provenance, confidence, source host and page date. The model's own restatement of
a fact is never rendered. Polarity is why "considered entering aerospace and abandoned it"
cannot become "serves aerospace":

- the prompt tells the model only an affirmed fact supports an affirmative claim (a prompt
  rule);
- the cover-letter scorer then enforces it: an affirmed fact can support a claim
  (`research_facts.supports`), and a non-affirmed fact on the same topic downgrades an
  otherwise supported claim to unsupported (a MAJOR finding). That part is deterministic,
  but its topic match is a word-overlap heuristic with an uncalibrated threshold
  (`SUPPORT_OVERLAP_MIN`).

Provenance is read off the text and defaults to unknown. Only facts verified against a page
the app fetched are `web_search`; recall from training data is labelled "recalled from
training data, NOT verified" in the letter prompt. How pages are found, anchored to the
right employer, dated and cited is in
[Tailoring](how-it-works/tailoring.md#company-research).

## Only proof moves state

**Rule: a posting is closed only when a source that can prove absence says so.** An
employer's own ATS board going stale is proof; Adzuna ageing a posting out of its query
window is only `likely_closed` (a badge) and moves nothing; a failed probe is no evidence and
keeps the prior verdict; an ambiguous employer link is no link. A manual verdict or a manual
state change always beats automation: every automated write is a compare-and-swap guarded on
the manual flag (`core/closure.py`). The mechanics are in
[Posting closure](how-it-works/closure.md#verdicts).

## Duplicates are suggestions

**Rule: a possible duplicate is a suggestion with evidence; only the user links two
postings.** An agency's client is never named beyond "Possibly the same role as" a posting
already in the database. A "not the same role" decision is recorded and blocks every later
merge of that pair: the fingerprint, the daily run and the recluster backfill never overturn
it (`core/role_clusters.py`). The rules for automatic clusters and suggestions are in
[Same role, different route](how-it-works/same-role.md#same-or-different-decisions).

## Geo is three-way

**Rule: an unplaceable location is never a hard reject.** A location is classified as a
listed home-state town, an affirmative other state or country, or `UNKNOWN`; `UNKNOWN` goes
to the review queue (`geo.assess_geo`, `gate.decide`). With no home state set nothing can be
"another state", so an on-site job is placed only by a town listed in settings and otherwise
goes to review. A remote job is kept when `remote_ok` is on. Deterministic. See
[Matching](how-it-works/matching.md#geo).

## Presence stubs

**Rule: a presence stub must not overwrite a field it does not know.** An adapter that skips
the detail fetch for a posting it already holds sends a stub that refreshes presence
(`last_seen`) and nothing else (`workday.presence_stub`). The Workday stub once carried the
listing's placeholder location ("5 Locations") and no remote flag, so every run overwrote an
accurate location and reset remote to false. Rows holding a placeholder now get one detail
fetch to repair themselves. Likewise a query-limited adapter is not a snapshot: it ages
postings by `last_seen`, never by absence. See
[Discovery](how-it-works/discovery.md#identity-and-refresh).

## Safe links

**Rule: every href from a feed or a model goes through `urls.safe_external_url`.** Only an
absolute `http://` or `https://` URL passes; anything else renders no link. The check runs
on a copy with control characters, TAB, LF and CR removed, because browsers ignore those
inside a scheme (`java\tscript:` executes). Deterministic (`web/urls.py`).

Why it matters more here than in most apps: `web/templates/base.html` carries the per-launch CSRF token on
`<body>`, so script running in the cockpit's origin could read it and drive every
state-changing route. A `javascript:` href would be full takeover, not a nuisance. The
cockpit has no authentication and can be opted onto the LAN (`web.host`), which raises the
stakes rather than lowering them. See [The web cockpit](how-it-works/web.md#security-model).

## Output paths

**Rule: never recompute a packet path; read the stored one.** Two output layouts exist on
disk (legacy per-role folders and today's per-company-per-day folders), and packets get filed
into bucket folders. So code reads `build_runs.artifact_dir`, resolves every stored path with
`paths.locate`, and writes every path through `paths.canonical`. Recomputing from today's
slugs lands in an empty new folder. See
[Events, artifacts and packet filing](how-it-works/events-and-filing.md#stored-paths).

## Never contact LinkedIn

**Rule: blastjob's own code never connects to a LinkedIn host, never logs in and never
automates a browser. Public LinkedIn pages may be read only through Jina, and only where a
feature needs them; never add a direct fetch.** (Owner decision, 2026-10-19.) Alert email is
the sanctioned LinkedIn feed, and no unofficial API is used.

The direct-connection half is deterministic. Every fetch blastjob makes itself from a URL
found in a page or a search (closure, careers-page links, employer descriptions, JD
enrichment) refuses `linkedin.com`, `lnkd.in` and `linkedin.cn` before any request
(`page_fetch.LINKEDIN_HOSTS`, refused even for callers that allow other job boards).

The Jina half is a scope rule. With a Jina key, blastjob's process connects only to Jina,
and Jina searches or reads on its behalf. Two features need LinkedIn content and may use it:
alert-mail enrichment (the public guest page of a job named in an alert email) and people
search (search results only). Company research does not need it and filters LinkedIn
results out of every search (`jina.is_linkedin_url`).

**Current behaviour.** These are the paths that reach LinkedIn content through Jina today:

- **Alert-mail enrichment.** The alert-mail adapter asks Jina's reader (`r.jina.ai`) to read
  the public guest page of each LinkedIn job named in an alert email, for the full
  description, posted pay and, for a whole-card alert, the company
  (`linkedin_guest.enrich_linkedin_via_jina`).
- **Find people.** The people search sends Jina search queries restricted to
  `site:linkedin.com/in` and uses only the search results; it reads no profile page
  (`web/people.py`).

Company research is not on that list. Packet research (`core/packet_research.py`) and the
cockpit's Research button (`web/research.py`) drop every LinkedIn result before choosing
pages (`jina.is_linkedin_url`, which tests the parsed host, never a substring), and
`jina.pick_pages` skips LinkedIn as a second guard.

Without a Jina key none of these run.

## Draft-only

**Rule: nothing is ever submitted, sent, posted, messaged or auto-applied on the user's
behalf.** Outreach notes, follow-ups and prep packs are drafts shown in the cockpit
(follow-ups and prep packs are also saved as files), and a follow-up offers a `mailto:` link
that opens the user's own compose window. The only messages the app
sends are to the user: the daily digest to their own channel.

Why: at senior levels a few targeted, approved applications beat volume, and anything acting
on the user's behalf against a job site risks their account.

## Web is the only surface

**Rule: a capability with no cockpit route does not exist.** A CLI may be a second way in,
never the only one, and a new capability needs a route.

## Packet verification

Every build, and every refine, verifies the packet it just produced and records the verdict
in `{role-slug}.packet_report.json`. `core/packet_report.py` is the vocabulary (`Finding`,
`PacketReport`, `GateVerdict`, severities FATAL, MAJOR and MINOR), `core/packet_verify.py`
runs the deterministic checks and the hold, and `packet_gate.gate` decides. Nothing in
`packet_verify` calls a model; the model scorers run first and their results are passed in.

**Verify the artifact, not the markdown.** The ATS receives the `.docx`, so character and
text checks (banned characters, encoding damage, one email and one phone, references and
personal data, page estimate) read text extracted from the exported `.docx` when it exists
(`resume_format.check_structure` with `docx_lines`). Role, heading, date and skills structure
can only be read from the markdown, so those checks always read it. Without a `.docx`, the
markdown is a cheap pre-pass for everything.

**Every position keeps its own title and dates.** Collapsing a promotion into one stacked
heading was rejected: parsers cannot tell which title held which years, and it can read as
years at the senior title. The resume prompt requires each title its own heading, dates and at
least one accomplishment (a prompt rule). The structure check then enforces bullets: a role
that ended within the last 20 years with no bullets is FATAL (`empty_recent_role`). Older
roles are an early-career tail and may be title-only.

**FATAL is calibrated to the boards actually applied through, not to a parser vendor.** The
defaults assume Workday, Dayforce, LinkedIn and Greenhouse. Textkernel sits behind none of
them, and its own docs call its codes risk signals, so every Textkernel-derived rule is MINOR
(recalibrate if your boards differ). FATAL is reserved for failures with evidence in their
path. The FATAL findings in the code are:

| Code | What it means |
|---|---|
| `banned_characters` | `< > [ ] { } " \` in the resume (a logged Workday rejection) |
| `encoding_damage` | a Unicode replacement character (U+FFFD) or a control character |
| `empty_recent_role` | a role that ended within 20 years has no bullets |
| `role_not_in_history` | an employer not in the work history, or dates for an employer that do not reconcile with it |
| `unverified_total` | a money or percent figure not stated verbatim in the trusted text |
| `cover_letter_employer_claim_refuted` | the research explicitly contradicts a claim about the employer |

A checker that cries fatal on compliant formatting teaches the user to ignore it, which is
worse than no checker.

**No computed totals.** `packet_gate.check_arithmetic` flags every money or percentage figure
whose exact normalized value does not appear, as a whole token, in the trusted text. No
summation is performed on either side: "the sum equals the named addends" would have passed
the motivating defect, a stated savings total that was one true figure plus an unrelated
second one. Notation is normalized (`$470K`, `$470,000` and `$470 thousand` are one figure)
but magnitude is exact. For the resume the trusted text is `work_history.md`. For the cover
letter it is the work history plus the verified source spans of the company facts, never a
fact's model-written statement. So a confidential posting, which gets no research, ships no
employer figure in its letter without a FATAL.

**A low groundedness score never blocks.** It is uncalibrated and fires on honest stretch
applications. It produces a loud, specific warning listing the unsupported claims instead.

**Mechanical fatals gate, and quarantine is per role and fails closed.** A FATAL finding
quarantines the artifact it names: a resume fatal holds the resume and its `_ATS` export, a
letter fatal holds only the letter, and a fatal with no recognised artifact holds everything
(`packet_report.artifacts_to_quarantine`). Held documents move into `quarantine/` inside the
packet folder; the sidecars (job description, research, scores, report) stay as evidence.
Otherwise "not ready" would be just a badge beside a working download.

- The packet folder is shared by every role tailored for that company that day, so quarantine
  and release work per role stem, never per folder. Releasing from the cockpit frees only the
  job's own role.
- The report names the files about to be held before they move, so the download route refuses
  them even if a move fails (`quarantine.held_names`).
- If a targeted file is still public afterwards, `QuarantineIncompleteError` is raised, the
  report is marked `quarantine_failed`, and the build ends with `PacketNotQuarantinedError`:
  it is recorded as FAILED, never tailored, though its folder is still recorded.
- Release is the user's acknowledged override: it moves the held documents back and appends a
  dated line with the user's reason to the append-only `RELEASED.txt`
  (`quarantine.release`).

**A verification crash is not a verdict.** If gating itself raises, the report says
`verification_failed` and not sendable, nothing new is quarantined (one broken checker would
otherwise hold every packet), and a hold from the previous report is carried forward, never
lifted. A checker that crashes is recorded in `checks_failed` with a MAJOR finding.

**A scorer that did not run is never an all-clear.** A missing resume score, an unscored
letter, a requested letter that failed to generate, and research with no cited source each go
in `checks_skipped`, and the gate shows a visible "not checked" reason for each. It is not
blocking, because a missing call is not evidence of a defect, but silence must never read as
"checked and fine".

**The cover letter is scored too** (`core/cover_letter_score.py`). Claims about the candidate
are checked against the work history with the same quote-substring rule as the resume. Claims
about the employer are checked against the research: a clause-scoped negation cue in the
quoted research makes a claim refuted (FATAL); a distinctive term the research never mentions,
or a downgrading fact, makes it unsupported (MAJOR). One derivation is allowed: an age computed
from a quoted founding year. This is model-assisted review. It is new and uncalibrated, its
negation and term rails are hand-made, and it over-flags: treat its number as a prompt to
read, not a verdict.

**The third bucket exists** (`core/coverage_gap.py`): requirements the work history proves,
with a verified quote, that the resume never used. It is read-only. The gap is shown with the
user's own sentence quoted verbatim for them to add; nothing is inserted automatically. An LLM
repair would turn "piloted a scheduling tool" into "led the scheduling rollout", attach the
genuine quote, pass every check, and lie. "The aggregate score did not drop" is not a
guardrail either: a repair can drop one weak claim and add one invented one. Any future
automated repair must pass a claim-level diff first.

**Prose checks are advisory.** Every `prose_check` finding is MINOR. The rules are absolute
vocabulary and pattern rules with a documented basis (stale resume cliches, LLM-elevated
vocabulary as information only, em dashes, formulaic openings and closings, phrases echoed from
the job description); there is no relative-to-baseline threshold. Job titles, company names,
certifications and other capitalized proper-noun phrases, and any phrase the work history
itself contains, are exempt from the JD-echo check. The
app never computes an "AI-written" probability (detectors false-flag non-native writers and are
not what a recruiter uses) and does not simulate an ATS pass/fail score.

**Do not encode folklore.** Popular statistics ("75% of resumes are auto-rejected by ATS"),
fixed bullet counts, filename conventions and board-specific formatting rules with no primary
documentation were checked and rejected. A rule must trace to a primary source or a logged
failure, and each `Finding` names its source so the rule can be re-argued against its basis.

**As-sent attribution** (`core/packet_sends.py`). When a job is marked applied, the resume,
cover letter and job description are hashed at that moment and written once, idempotently, so a
later rebuild cannot reattribute an outcome to a document that was never sent. Outcomes are a
separate append-only log. Reporting is descriptive only (what was sent, where each application
stopped); there is no style ranking at any sample size, because seniority, referrals, timing
and company dominate style.

# Tailoring

This page covers how blastjob turns source documents into a work history, and a kept job
into a packet: ingest, the build pipeline, company research, document names, build claims,
refine, the interview prep pack and follow-ups. It is for anyone changing the tailor path.
Read [Truth guarantees](../truth-guarantees.md) first: the rules there (groundedness, no
computed totals, research as a trust boundary, packet verification and quarantine) are
stated once, in that page, and this one links to them. Prompt caching and the providers are
in [The LLM layer](llm.md).

## Ingest

Route `/work-history/ingest` (`web/routes_settings.py`) takes uploaded `.pdf`, `.docx`,
`.doc`, `.txt` and `.md` files and runs `ingest.run_ingestion` (`core/ingest.py`) as a
background run. Only one ingest runs at a time. Before it starts, the route backs up the
current `work_history.md` to `<data_dir>/backups/` (`workhistory.backup_work_history`).

1. `folder.labeled_chunks` reads each source file and wraps it in `<file path="...">` tags. The
   DOCX reader takes body, table and header/footer text (`core/docx_text.py`). A file that
   fails to read, or reads as empty (a scanned PDF has no text layer), is reported as a
   `[SKIP]` line. If every supported file was skipped, the error says none had extractable
   text; "No supported files found" means nothing with a supported extension was there.
2. One streaming call with `INGEST_SYSTEM` returns a JSON object matching `MasterHistory`.
3. `ingest._parse_history` pulls the JSON out of the response with a regex and Pydantic
   validates it into `MasterHistory`.
4. `ingest._serialize_history` writes one deduplicated, canonical `work_history.md` (YAML
   frontmatter plus `##` sections), via a temp file and a rename. This file is the source of
   truth for every later step; nothing the app writes can be more accurate than it, so the
   user reads and corrects it by hand.
5. A second call with `TEMPLATE_SYSTEM` produces the standard and ATS templates in one
   response, split on an `===ATS===` delimiter, and writes `templates/standard.md` and
   `templates/ats.md`. If either template comes back empty, ingest raises rather than
   overwrite a good template with an empty file.

## Build pipeline

`build.run_build` (`core/build.py`) is the only build path. The cockpit's tailor route
reuses it (see [Build claims](#build-claims)). In order:

1. **Read inputs.** `work_history.md` and the selected template (`templates/ats.md` in ATS
   mode, else `templates/standard.md`) from the data dir. No work history is an error.
2. **Company research** (skipped for a confidential posting). See
   [Company research](#company-research).
3. **Company facts.** Cited research already is a list of verified facts. Fallback research
   is distilled into facts in an isolated call (`research_facts.distill_research`). Either
   way the facts, not the raw text, are what the resume and the cover letter get.
4. **Resume.** `caching.build_resume_messages` assembles the cached message list (system
   prompt `RESUME_SYSTEM` or `RESUME_ATS_SYSTEM` with the candidate profile appended, the
   work history, the template, then the job description and the company facts block as a
   trailing uncached block), and one streaming call writes the resume markdown. An empty resume
   fails the build.
5. **Folder and names.** Under the shared filing lock, the build picks today's
   `{company-slug}-{date}/` folder (`paths.application_dir`), the document stems (see
   [Document names](#document-names)) and a collision-safe role slug
   (`paths.resolve_role_slug`). The folder is shared by every role tailored for that company
   that day.
6. **Export** the resume to every selected format via `exporters/` (md, pdf, docx, ats).
   PDF and DOCX export are fail-soft.
7. **Sidecars**, role-prefixed: `{role-slug}.job_description.md` and, when there is
   research, `{role-slug}.company_research.md`.
8. **Cover letter** (when requested; fail-soft): written from the work history, the resume,
   the job description and the facts block, wrapped in a letterhead built from the work
   history's frontmatter (never asked of the model), and exported.
9. **Cover-letter score** (`cover_letter_score.score_cover_letter`, fail-soft) written to
   `{role-slug}.cover_letter_score.json` and `.md`.
10. **Fit score** (`scoring.score_resume`, fail-soft), written to
    `{role-slug}.fit_score.json` and `.md`: groundedness plus JD alignment.
11. **Coverage gaps**: requirements the history proves but the resume never used
    (`coverage_run.coverage_gaps_for`, one model call per distinct job description and work-history text, cached
    in `<data_dir>/coverage_cache/`; editing the history misses the cache), written to `{role-slug}.coverage_gaps.md`.
12. **Verify, gate and hold.** The deterministic checks read the exported `.docx`, which is
    why export comes first (the [timeline checks](#timeline-checks) read the resume text). Held documents move to `quarantine/`. See
    [Packet verification](../truth-guarantees.md#packet-verification).
13. **Metadata**: `{role-slug}.metadata.json` records company, role, provider, formats and
    this build's cost and tokens (`build_cost_usd` is `null` when any call's cost is unknown,
    never a partial sum; the legacy `cost_usd` key stays numeric), plus the scores.
14. If a held document could not be moved out of the public folder, the build raises
    `PacketNotQuarantinedError` after writing the metadata, so the role slug stays claimed
    and the caller records the build as failed.

The model calls in one build: page research (plus a page-match judgment when the job has no
anchor), or the fallback research call and its fact distillation; the resume; the cover
letter and its score; the fit score; and coverage analysis (skipped when cached). Every call's
cost is recorded as it happens, so a build that fails partway keeps its real spend.

## Timeline checks

Groundedness proves a claim is quoted from the work history; it never asked whether the
claim fits the dates of the role it sits under. `core/resume_timeline_checks.py` does, with
regex over text and no model, as its own verify check named `resume_timeline` (a crash there
is recorded as a failed check and cannot hide the structure checks). One role shape,
`TimelineRole` in `core/resume_timeline.py`, comes from three parsers: the work history, a
markdown resume (`### ` title, then an employer line) and the plain-text ATS resume (an
`EXPERIENCE:` section; the 80-column wrap is joined back into its bullet). The claim
patterns live in `core/resume_timeline_claims.py`.

| Code | Severity | Fires when |
|---|---|---|
| `duration_exceeds_role` | MAJOR | A bullet states an elapsed span ("within five years", "over 60 months", "a 5-year build", "5 years of") longer than the role plus 3 months. Plan horizons ("3- and 5-year roadmap"), ranges, fractions and "years of experience" are skipped. |
| `result_after_role_end` | MAJOR | A bullet cites a year (or month and year) after the role ended. A year equal to a year-only end never fires; plan, roadmap, budget, version and future years are exempt; "by 2019" is exempt only if the bullet has plan words. |
| `promotion_line_dropped` | MAJOR | A `Promoted from X to Y` bullet in the work history is not the first bullet of that role on the resume, or does not name both titles. |
| `promotion_titles_merged` | MAJOR | Two titles at one employer share one heading (a stacked layout, or one role whose dates span both). |
| `short_role_growth` | MINOR | A role under 6 months holds a growth or revenue figure ("from one to five", "$1M"). Advisory: "if the dates are right, expect the question". |
| `timeline_not_checked` | MINOR | A role's employer or dates cannot be read, the range is reversed or in the future, or the work history heading carries several ranges. A role not checked is never an all-clear. |

A bullet may span titles only by saying so: "Across both roles" allows this role plus the
better of its two same-employer neighbours; "across roles", "over my N years at" or "over my
time at" allows the contiguous same-employer chain. Spans are real intervals, merged when
they overlap or sit within a month of each other, so two roles with a gap between them never
add up to the hull.

None of these is FATAL: `core/packet_report.py` reserves FATAL for failures seen in the real
application path, and a regex duration check will have false positives. So they never change
whether a packet is sendable. Instead the packet page shows a **Check before sending** box
listing them, and the build and refine logs print the same lines. Revisit this if a packet
carrying one of these findings is ever sent. The same date checks run on `work_history.md`
(`check_history`); the work-history page lists them as **Date consistency notes** under the
editor, computed from the text on screen (on load, after a save, on a conflict and on an
error), read-only, and never editing anything. A history heading with more than one date
range is not guessed at: split it into one heading per title.

The drafting prompts carry the same rules (`RESUME_SYSTEM`, `RESUME_ATS_SYSTEM`,
`REFINE_SYSTEM`): keep the promotion line first, say "Across both roles" for a claim that
covers both, never move a dated result under a role that ended earlier, never stack titles.
Refine puts structure above feedback: a request to stack titles keeps separate headings
(the model has no channel for a change note, so the revision just ignores that part).

## Company research

Packet research is built from pages the app actually fetched, with a cited source for
every fact (`core/packet_research.py`, #59). It needs a Jina key; without one it falls back
(below).

**Gathering.** At most three Jina searches (`packet_research.MAX_SEARCHES`), the employer's
own domain first when one is known and the job's state in each company query, then at most
`jina.MAX_PAGES` (four) page reads, read concurrently. Searching and reading must finish
inside `GATHER_DEADLINE_S` (45 seconds); the model calls that follow are not counted
against it. Search-result snippets are never page text and can never back a fact. LinkedIn
results are dropped before pages are chosen ([why](../truth-guarantees.md#never-contact-linkedin)).

**Pages must be about THIS employer** (`core/research_match.py`). Common names collide: a
trial once returned eight "verified" facts about a different company of the same name in
another state. The job context comes from `research_context.job_research_context`: the
job's city and state, and the links that are the employer's own (an authoritative board
posting, a closure or manual employer link, the role's best apply link unless it is an
agency's; an agency posting contributes none). A page is used when:

- it is on a strong employer domain: `discovery.employer_domains` for this company, or the
  host of the job's own posting or apply link. Aggregator hosts and any multi-employer ATS
  host (tenant subdomains included) never count;
- it is on a website named in the job description and also names the company (a weaker
  anchor, because JDs link to benefits and EEO sites); or
- it names the company with the job's US state, by name or as a two-letter code in an
  address shape, within 300 characters (`research_match.NEAR_CHARS`) of the mention.

"<Company> of <Place>" and "<Company> at <Place>" name a different company and do not count
as a mention. A city counts only with its state: "Springfield, IL" is not Springfield in
another state. Only when the job has no anchor at all (no usable location, no domain) does an
isolated model call judge each page against the job description; only an explicit
"same employer" admits a page, a failed or unparseable judgment admits none, and the packet
report notes `company_match_judged`. Pages fetched but none anchored add
`company_match_unconfirmed` and fall back.

**What the model returns is checked.** The admitted pages are sanitized
(`research_facts.strip_instructions`), fenced, and sent with the company name and nothing
else to one isolated extraction call. In Python, a fact survives only if:

- the URL it cites is a page that was read, and its span is verbatim (at least 16
  characters) in that page's text, both sanitized and original;
- its statement is not instruction-shaped; and
- every figure in its statement is a figure in its span, and every figure in its time scope
  is on its page.

The single values it asks for (founded, headquarters, employees, revenue) are kept only if
each appears as a whole figure in the page it cites (`core/cited_values.py`); otherwise they
are dropped, never guessed.

**The letter gets source sentences.** The research sidecar opens with
`research_facts.CITED_MARKER` and lists the verified values, the quoted spans with their
source host and date, and the sources. No model-written sentence goes in it, because refine
re-distills it and a model sentence there would come back as a "verified span". Neither
the resume nor the cover letter reads this sidecar: both get the facts block, which is each
verbatim span with its polarity, provenance, confidence, host and date, never the model's
restatement (`research_facts.facts_block`). With no verified fact the block says so and
tells the model to make no claim about the company. Refine's resume revision gets no
research at all; only its letter re-score reads the sidecar, and re-distills it first.

**Dates** (`core/page_dates.py`) are publish dates only, found without guessing: the
reader's published time, a published-time meta tag or schema.org `datePublished`, a date in
the URL path, or a dateline near the top of the page. Modified or last-updated stamps never
count. With none, the source is "undated". "Over 2 years old" is computed in code
(`research_facts.source_label`), and the prompt allows a dated figure only with "as of
<date>" phrasing and never as current.

**Fallback.** No key, no readable page, no anchored page, no verified fact, or any failure
falls back to `providers.make_research_call`: on the `anthropic` provider a call with the
`web_search` tool, on `claude-cli` and OpenAI a completion from training data with a
disclosure footer (see [Research call](llm.md#research-call)). The packet report then notes
that company facts have no cited source (`company_research_sources`), or that the company
could not be confirmed. Research is advisory and never fails a build.

## Document names

Documents are named `{Person}_Resume_-_{token}` and `{Person}_Cover_Letter_-_{token}`, with
`_ATS` appended for the ATS export. `paths.document_token` caps the company+role token at
`DOCUMENT_TOKEN_MAX` (40) characters: it drops company legal suffixes and role
parentheticals, abbreviates title words, drops filler and trailing company words, and only as
a last resort cuts into the role with a short hash so two roles at one company never share a
name. A token that already fits is unchanged. Indeed refuses an upload name of 90 or more
characters (#19); the cap keeps every document at or under 80 for a person slug of up to 18
characters.

Every matcher (artifacts, refine, quarantine, the packet renamer) accepts
`paths.document_tokens`: the current capped token, the tokens earlier abbreviation tables
produced, and the legacy uncapped token, so packets built before the cap still resolve.
`blastjob-daily --file-packets` renames older documents to the current token; see
[Events, artifacts and packet filing](events-and-filing.md#document-renames).

## Build claims

The cockpit's tailor route (`web/build.py`) runs `build.run_build` as a background run.
A job is claimed first with a compare-and-swap (`client.claim_for_build`): `build_state`
moves from none or failed to `tailoring` and a `running` row with a 20-minute lease is
written to `build_runs`, in one transaction, so a browser refresh or a second click cannot
double-start a build. The claim also refuses while a live refine holds the job. At startup
`client.recover_stale_builds` (called from `web/launch.py`) resets every `running` row left
by a previous process, or by this one with an expired lease, so a crash never leaves a job
stuck in `tailoring`. `client.finish_build` records the outcome and the packet folder in
`build_runs.artifact_dir`. What happens after a build (artifact index, apply-link file,
filing) is in [Events, artifacts and packet filing](events-and-filing.md).

## Refine

Refine revises one role's resume from the user's feedback and re-verifies it exactly as a
build would (`run_refine_detailed` in `core/refine.py`, route wrapper `web/refine.py`). A revision is not
trusted because the user asked for it.

- **It targets the job's own role**, located by company and role among the folder's
  documents (`refine.locate`), never "the newest file in the folder".
- **It claims the job** (`client.claim_for_refine`: a `build_runs` row whose run id starts
  with `refine:`, 90-minute lease). Tailor and refine exclude each other. It also takes a
  per-resume `flock` (`refine_files.lease`), so two refinements of one resume cannot run.
- **It stages, then publishes atomically.** The revision is exported into a staging folder,
  verified there, then moved into place; the previous version is kept as `{stem}.vN.md`
  (under `quarantine/superseded/` when the previous version was a held one, so a failed
  resume never becomes a downloadable "older version").
- **It re-scores the resume** (one paid call), runs the deterministic checks, the gate and
  the hold like a build, and carries the coverage gaps from the previous report.
- **The cover letter is re-scored only if it changed or was never scored.** Otherwise its
  earlier findings are carried forward (re-running its arithmetic check would lack the
  research spans that made its employer figures legitimate).
- **A verification crash never lifts a hold.** An artifact the user released stays released
  while its findings are unchanged.

## Interview prep pack

`POST /jobs/{id}/prep` (the **Interview prep** button on a kept job) streams a prep pack via
`web/prep.py` to `core/prep.py`. Headless:
`uv run python -m blastjob.core.prep <job_id>`.

- **Refused on a snippet.** If only a snippet of the job description is stored, no pack is
  built: the STAR table and likely questions would read as fact about a posting seen one
  paragraph of. The user pastes the full description first.
- **Sections:** role at a glance, JD accountabilities to STAR stories, likely questions and
  answers, a 30/60/90 draft, questions to ask, gaps to pre-empt, and logistics. Stories,
  employers, dates and metrics come only from `work_history.md`.
- **`[confirm]` marking.** `mark_unverified_numbers` appends `[confirm]` to every
  figure that is not a whole-token match for a trusted figure (the rule and what counts as a
  figure are in [Prep-pack numbers](../truth-guarantees.md#prep-pack-numbers)). It runs on
  each completed line while streaming, so the browser and the saved file agree.
- **Trusted text** is the work history plus every number already on file, rendered in the
  forms a model is likely to echo by `trusted_numbers_text`: comp-signal ranges, the
  negotiated floor, the job's `comp_min`/`comp_max`, and company-profile figures
  (employees, customers, locations, revenue, total assets, culture rating).
- **Where it goes.** `{role-slug}.interview_prep.md`, written into the job's newest
  successful packet folder, read from `build_runs.artifact_dir` and located at write time
  under the shared filing lock (`artifacts.latest_packet`). Only when there is no packet, or
  its folder is gone, does it go to today's `{company-slug}-{date}/` folder. The file is then
  registered in the artifact index.

## Follow-ups

`POST /jobs/{id}/followup/draft` drafts a follow-up note via `web/followup.py` to
`core/followup.py`. Draft-only: blastjob never sends mail.

- The subject is written by code ("Following up: <role> at <company>"), never by the model,
  because a model-written subject can invent a conversation.
- The prompt gets the work history (cached), the application timeline, why a follow-up is
  due, an optional stored contact and the user's notes. The job description is left out on
  purpose.
- The draft is saved as `{role-slug}.followup-{job_id}.md` in the packet folder the route
  resolved from the stored path (located again at write time under the filing lock). With
  no packet on file it goes to `unfiled-followups/` in the output folder. The cockpit offers
  a `mailto:` link that opens the user's own compose window with the text filled in.

# Matching

**What this covers:** How a posting becomes surfaced, queued or rejected: geo, the title prefilter, alignment scoring and its fingerprint, the gate and comp floor, pasted descriptions and archive reasons. For anyone changing `matching/`.

Matching reads what [discovery](discovery.md) stored and decides which review state each undecided job lands in. It touches only jobs whose `review_state` is `new`, `surfaced` or `queued`. Anything the user has kept or archived is never re-touched.

## Pipeline order

`run_matching` in `matching/pipeline.py` works from cheap to expensive:

1. **Company exclusions.** A job from an excluded company is archived on arrival, before anything else (see [archive reasons](#archive-reasons)).
2. **Geo** (keyless). Skips the LLM for clearly far on-site roles.
3. **Title prefilter** (deterministic). Skips the LLM for configured drop families.
4. **Batched LLM alignment scoring.** Only the survivors reach it.
5. **Gate.** Turns the score, comp and geo into a review state.

Matching makes no LLM call until `candidate.target_titles` is set (`is_configured` in `models/config.py`), so a fresh install cannot spend money scoring against nobody. Geo and the prefilter still run; every survivor stays unscored, and the run reports a note that no candidate profile is set.

`MatchResult.cost` totals what this run's model calls cost (only the calls it recorded, so a shared tracker is safe); it is unknown if any call's usage was unknown. The cockpit run logs, the daily log line and the digest footer show it (see [llm](llm.md)).

The daily run scores only unscored rows (`only_unscored`). The cockpit's **Find + Score** does the same, while **Re-score changed** also redoes scores whose fingerprint is old (see [score fingerprint](#score-fingerprint)). **Re-score all** is `run_matching(rescore_all=True)`: no prior score is replayed, so every candidate that passes geo and the prefilter goes to the model (byte-identical postings still share one call). The cockpit asks for confirmation first, naming `pipeline.count_candidates` jobs, and the POST (`/run/matching/all`) is CSRF-checked. Both buttons share the one `matching` run slot, and the run log's first line names the mode.

## Geo

`matching/geo.py` decides whether a posting is within commuting range of home, or remote. The decision is three-way, never boolean, because "not obviously home" is not the same as "somewhere else":

| Location | Result |
|---|---|
| Remote (and `discovery.remote_ok`) | pass, verified |
| An affirmative other US state or a foreign country, on-site | fail ("far"), verified |
| A home-state town listed in `discovery.towns` | pass or fail by haversine miles from home: within `commute_radius_miles` passes, within `extended_radius_miles` passes as conditional ("verify hybrid"), beyond fails |
| Home state, town not listed | unknown, goes to the review queue |
| Geo not configured, or the location cannot be read | unknown, goes to the review queue |

An unplaceable location must never be a hard reject. A rejected job keeps a NULL score, so it would be re-selected and re-rejected on every run, forever. Only an affirmative other-place signal may reject, so board noise such as `USA-OH-Springfield`, "5 Locations" or "Corporate Offices (0900)" lands in review instead.

The first version of this filter tokenized on whitespace, so it hid state codes inside forms like `USA-XX-City`, rejected "Remote" and multi-location placeholders, and reset rows so they were rejected forever. The current rules:

- Tokenizing is delimiter-agnostic (commas, hyphens, slashes and parentheses all split).
- A word that follows a place name as a street or county word is part of an address, not the place ("750 W Virginia St" is not Virginia).
- State abbreviations match upper case only, because "in", "or" and "me" are ordinary words.
- A home-state mention wins outright; a bare town name is the weakest evidence and is consulted last.

A requisition listed in several places arrives as "A; B; C" in `geo_location_raw`. Each segment is judged on its own text, and a segment placed within range wins, so an out-of-state primary cannot hide a home-state location. If none is placed, the whole string is judged as before, so an unplaceable segment never rescues an out-of-area posting.

### The dream-list exception

A job row is exempt from the location reject (#90) only when `core/dream_rule.py` says so: its company is on the dream list now (`target_companies.tier = 'dream'`, matched by `company_key()`), AND it came from a configured `discovery.target_boards` entry for that company (the row's `source` is that board's source), AND that entry has a non-empty `identity_basis`. A company name alone is not enough: an Adzuna, alert-mail, Apple or search-firm row can carry any name, so a far one is rejected as before, and so is a row from a board entered by hand without a basis. `DreamRule` is the one helper matching, `/funnel`, the All page and the weekly section share. The Dream badge is separate and stays name-based. Both places that apply a geo reject use one rule, `geo.is_geo_reject(verdict, dream=...)`: `run_matching` and the `/funnel` stages. The role goes on to the title prefilter and the scorer like any other, and the gate lets it through (`geo_by_id` holds a pass for it, because `gate.decide` repeats the location check). The stored `geo_status` and distance stay truthful: they still say the role is far. Only the reject is skipped. Every other gate applies: scope prefilter, a posted comp under the floor, exclusions, same-role and the threshold. An unplaceable location is not far, so it is unchanged.

`jobs.dream_far` (migration 59) records that the exception is what kept the role (`geo.is_dream_bypass`). It is written with the geo verdict on every matching pass for every row matching considers, so it is 1 only while the verdict is far and the company is on the dream list, and 0 again when the role turns remote or local, the radius changes, or the company leaves the tier. The daily pass scores only unscored rows, but it also reloads rows with `dream_far=1`, so a role that lost the exception is rejected again (its score replays for free). The Dream badge is not stored; it is computed at render time from the current list. See [daily](daily.md#dream-list) for the weekly section and [discovery](discovery.md#dream-list-boards) for how the boards are read.

Adding a town to `discovery.towns` can only move a job from unknown to a measured distance. List only towns you would consider: a far listed town becomes a hard reject, while the same town unlisted lands in review. That asymmetry is deliberate.

## Title prefilter

`matching/prefilter.py` cuts the haul to roles worth a score without gating on title variants. The configured drop terms (`discovery.title_drop_terms`) remove whole job families, never title variants such as "Head of", "Lead" or "Principal"; judging variants is the scorer's job.

- A title containing a keep term is kept, in any wording.
- A title containing a drop term is dropped, unless the description shows people-leadership scope ("direct reports", "lead a team of", and similar strict phrases), which rescues it.
- Anything else is kept and left to the LLM.
- Both lists empty keeps everything.

It errs toward keeping: a false keep costs one score, a false drop hides a real role forever. Matching is on whole words.

## Alignment scoring

`matching/scorer.py` sends survivors to the LLM in batches (`DEFAULT_BATCH_SIZE`, 15) and reuses the stored score for an unchanged posting. Reuse is keyed on employer, title and description text, so two companies that share boilerplate stay two jobs, and a description under 200 characters is never reused. Without reuse, one reseller listing scored 90, 89, 88 and 84 on four consecutive mornings and crossed the threshold in both directions.

If a batch fails or answers only in part, a per-job fallback retries the rest. A model alignment that is malformed (not a number) is treated as missing and retried, never stored as 0: a 0 reads as "scored, hopeless" and would stop the daily retry and bury the role.

The rubric is written for the candidate's seniority (`candidate.seniority`, free text matched on keywords). It picks one of three weightings:

- **Leadership:** scope, level and domain fit weigh most; named tools are secondary and transferable.
- **Hands-on / individual contributor:** the specific skills the description requires weigh heavily.
- **Neutral:** used when seniority is unset; the requirements are weighed as the description states them.

A plain rubric over-penalizes senior candidates for missing tool names the role does not need from them. The scorer reads the work history and the candidate profile from settings; it never assumes a field or level.

## Gate

`matching/gate.py` (`decide`) turns the pieces into a review state. With the default `alignment_threshold` of 78:

- Alignment at or above 78 is **surfaced**.
- 73 to 77 (the threshold minus a 5-point band) goes to the **queue**, as does conditional or unknown geo and a sub-floor *inferred* comp figure.
- Below the band the job stays `new`, with its score kept.
- A hard reject (far on-site, or posted comp below the floor) stays `new` whatever the alignment.

78 is the default because genuine market fits landed in the high 70s. It is uncalibrated for a new profile, so tune it after the first week. The 5-point band is sized to the observed run-to-run variance of the scorer on the same posting.

## Comp floor

`comp_floor_usd` defaults to 0, which disables the comp gate until a floor is set.

- Unknown or undisclosed comp **surfaces**, badged "comp unverified". Senior roles rarely publish pay, so rejecting unknown comp would empty the list.
- Only *verified* (`posted`) comp below the floor is a hard reject. Comp the LLM read out of the description is `inferred`, our own reading, and goes to the review queue instead.
- `gate.below_floor` is the one comparison rule, also used by Compare and the funnel. Only a USD (or unlabelled `$`) posted maximum is comparable. "From $X" or "$X+" stores a minimum with a maximum of 0 (unknown), so it never hard-rejects, and another currency surfaces too.

## Compare-and-swap writes

Matching writes are compare-and-swap on `review_state`, and only from `new`, `surfaced` or `queued`. The score, any inferred comp and the new state land in one conditional UPDATE. A Keep or Archive clicked while a scoring batch is in flight always wins over the score that lands afterwards. The same UPDATE is conditioned on the `raw_jd` that was scored, so a description pasted mid-batch never receives the old text's score.

## Two scores

Two scores stay distinct. The pre-generation **alignment score** says whether the role fits the candidate. The post-generation groundedness `FitScore` says whether a generated resume is supported by the work history ([truth guarantees](../truth-guarantees.md)). An early design used the second as a fit signal; that measures hallucination, not fit, and is the wrong gate signal. Never use one in place of the other.

The reporting line (who the role reports to, executive-team membership) is display only: it is read from the description by `core/reporting_line.py` ([how it shows](web.md#list-rows-and-badges)) and is never an input to the alignment score.

## Score fingerprint

Every alignment score is stamped in `score_model_version` with a fingerprint (`matching/fingerprint.py`, #29): a hash of `SCORER_SCHEMA_VERSION`, the exact scoring system prompt, the candidate profile, the `work_history.md` text and the provider and model. A single writer, `pipeline.score_columns` (also used by `rescore.score_kept`), stamps it, so no path can land a score without the fingerprint it came from. A stored score is reused only when its fingerprint matches the current one.

Without it, the old single Re-score button replayed every unchanged posting's old score after a profile, work-history or rubric edit, so no band ever moved.

- **Excluded on purpose:** the archive-reason calibration block (it changes on every archive, which would invalidate every score) and batch peers. The model behind the `claude` CLI is invisible to the fingerprint; that is accepted.
- **Who redoes old scores:** only the cockpit's **Re-score changed** and **Re-score all** buttons redo old-fingerprint scores. The daily run scores unscored rows only. The cockpit shows how many surfaced and queued scores predate the last change (`web/routes_scores.py`, `/scores/stale`).
- **When to bump:** bump `SCORER_SCHEMA_VERSION` by hand when the scoring user-content template, JD excerpting or score normalization changes. A bump makes every stored score old.

## Pasted descriptions

`POST /jobs/{id}/jd` stores a pasted full description over a snippet (`set_pasted_jd` in `db/client.py`). A score belongs to the JD it was computed from, and reuse keys on employer, title and description, so the new text must not meet the old score:

- On an undecided row the score is cleared. If the re-score fails, the daily `only_unscored` pass picks up the NULL.
- A kept row keeps its old score, because nothing ever retries a kept row and clearing it would lose the number for good if the re-score failed. The re-score drops that row's old score from the reuse table so it is not replayed.
- Nothing starts unless the text actually changed.

When the text changed, a `score-job` run (`web/paste_rescore.py`, calling `rescore.rescore_job` in `matching/rescore.py`) re-scores: undecided rows through the scoped `run_matching` (gate and compare-and-swap included), kept rows score-only (never `review_state`, no comp inference), archived rows not at all. Both score writes are conditioned on the `raw_jd` that was scored (`raw_jd`), so a newer paste wins a race. The response sends `HX-Trigger: refresh-run-status` so the run appears in the banner without a reload.

## Archive reasons

Archiving takes a reason code plus an optional note: `level`, `domain`, `geo`, `comp`, `company` or `other`. Codes, not free text, so each can be routed deterministically. A free-text reason pasted into a prompt is an unversioned policy change ("too far this week" would quietly depress good roles).

- `level` and `domain` archives feed the alignment prompt as calibration examples (the last 15; `load_rejections` in `matching/pipeline.py`).
- `company` records a standing exclusion in `company_exclusions`, a row the user can see and delete. From then on every posting from that employer (matched on `company_key`) is archived on arrival, before any LLM call. Removing the exclusion does not un-archive postings already archived under it; those are reversible one at a time.
- `geo` and `comp` are already deterministic gates, so they carry no calibration.

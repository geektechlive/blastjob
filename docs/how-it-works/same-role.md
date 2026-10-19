# Same role, different route

**What this covers:** How one job seen several times is recognised: the title normalizer, clusters, auto-clustering, reposts, suggestions and decisions, one application per role, one apply link per role, and company keys. For anyone changing clustering or apply links.

One role can show up several times wearing different hats: on the employer's board, on an aggregator, through an agency, or reposted weeks later under a new id. Treating those as separate jobs creates duplicate applications. The code groups them, and it is careful about who decides: the automatic rule merges only on strong evidence, and everything weaker is a suggestion the user confirms.

## Clusters

Cross-source duplicates are grouped into a reversible cluster (`job_clusters`, with a `cluster_id` column on each job), never merged. A cluster is a display grouping: no row is deleted, and a split puts members back apart.

A new posting joins the cluster for its fingerprint (`normalize.cluster_fingerprint`, built with the [title normalizer](#title-normalizer)), unless that cluster already holds a different requisition from the same authoritative board. Two requisitions on one board are two roles, so the newcomer gets a cluster of its own. A stored posting keeps its cluster on refresh. Re-fingerprinting on refresh would undo a split the user made, dissolve a merge they confirmed, and shunt a presence stub (which has no location, so a different fingerprint) into a fresh cluster every day. Only merges and splits move a posting between clusters.

The detail page lists every member under "Also listed as", each with a **Not the same role** undo.

## Title normalizer

Titles group through ONE normalizer, `core/titles.py` (`titles.normalize_role_title`). It lowercases, drops parentheticals, `&`, and the words of/and/the/for and remote/onsite/on-site/hybrid, and folds vice president to `vp`, senior to `sr` and information technology to `it`. "VP of Technology" and "Vice President of Technology" both become `vp technology`.

The discovery fingerprint and both same-role rules below use it, so a title that groups one way on insert groups the same way in the backfill. Closure's employer-link matcher does not use it: that matcher expands abbreviations the other way and is calibrated on its own form (see [closure](closure.md#employer-links)).

## A cluster has one review state

A cluster shows once, and it has one review state (`representative_key` in `web/stage_lists.py`):

1. If any member is kept, the kept member furthest along decides its stage. An application in progress outranks a closed one, and a closed one outranks the Shortlist, because a posting can close because they are interviewing you. Ties go to the better fit, then the oldest id.
2. Otherwise, if any member is archived, the archive hides the cluster.
3. Otherwise the best-fit surfaced or queued member represents it.

A decided cluster drops out of the review lists. The reason: a kept posting once went stale, an active sibling crossed the browse floor after a re-score, and the role reappeared with Keep and Archive buttons. Clicking Keep would have made a second application.

The **+N sources** badge counts every member of the cluster, including members the current query excludes.

## Auto-cluster

`role_clusters.auto_same_role` merges two postings automatically only when they have the same `company_key` and the same normalized title, and then one of these holds:

- Both come from non-authoritative sources (aggregators, alert mail, manual entries) and were first seen within `AUTO_WINDOW_DAYS` (7). Aggregators syndicate one requisition to many cities.
- They come from different sources and are in the same metro.

Placement rules (`role_clusters.same_metro`):

- A remote or unplaceable side counts as the same metro only when BOTH sides are non-authoritative. Any pair that includes a board posting needs strict proximity or both explicitly remote. A board posting in a cluster is closure proof for its siblings (see [closure](closure.md#employer-links)), so it joins only on real evidence.
- The same-city-name shortcut needs equal regions when both are known: Columbus, OH is not Columbus, GA. Centroid and radius matching still crosses state lines.
- A multi-location Workday row is placed at its primary location only. Geo may let it through on a later home-state location, but two postings are the same metro by where the requisition is anchored.

Never auto-merged: two requisitions on one authoritative board (two reqs, always), and agency or aggregator companies (those are suggestions).

`run_discovery` in `discovery/pipeline.py` applies the rule to groups holding a posting new this run. Merges are merge-only, cluster-level, and never cross a pair the user marked "not the same role". Two groups that each hold an application in progress are never bridged by the backfill's transitive union.

To backfill existing data:

- `blastjob-daily --recluster-report` prints the merges and the visible stage changes it would make, and writes nothing.
- `blastjob-daily --recluster` applies them.

## Reposts

A repost of an application in progress joins it. An aggregator once re-served an applied-to requisition under a new ad id weeks later; it surfaced as new, and it was nearly applied to twice. The rule: exactly one side is kept and applied, screen, interview or offer; the other has no application history (not kept, or kept with an empty application state and no `applied_at`); both are non-authoritative; and the metro is strict. There is no time window, and the same source is fine.

A closed application's repost is never merged, because it may be a new requisition and a merge would bury it in Closed. Two applications, or a loose location, are never merged either. Those become a suggestion of kind `previous`: the other posting is an application (any applied state, in progress or closed, or one with an `applied_at`), outside this job's cluster, with the same non-empty `company_key`, an equal normalized title, neither side an agency, a loose metro, and no time window.

For a `previous` suggestion the detail page says "You applied to this role on <Mon D> (<state>)" with the same and different buttons. List rows show an **applied before: <date>** chip, red only while that application is still in progress, since applying again into a live process is the costly error. **Mark applied** confirms by name, and "different" suppresses it like any other suggestion. The daily digest skips any row whose cluster holds a kept or archived member (`daily.pending_jobs`), so a merged repost is never announced.

## Suggestions

An agency and an employer posting of the same role are a suggestion, never a merge (`same_role.possible_same_role`). Applying through both can start a candidate-ownership dispute and cost the placement. The match is an equal normalized title, a strict same metro, and first seen within 60 days (`SUGGEST_WINDOW_DAYS`). Description overlap is shown as supporting evidence only, because agencies rewrite descriptions.

The detail page warns (in red when the other posting is already applied). List rows get a chip such as "possible duplicate of <company>" or "already applied: <company>", and **Mark applied** and **Advance** carry a confirmation naming the posting. An agency posting with no match says "Employer not named in this posting". An agency's client is never named beyond "possibly the same role as <a posting already in the database>". Several matching employers are shown together and nothing is linked until the user decides.

## Same or different decisions

`POST /jobs/{id}/same-role/{other}/{same|different}` records the user's verdict in `same_role_decisions`:

- **same** joins the two clusters, so the role shows once, in its furthest stage. It is refused, with the reason, if it would cross a "different" pair or put two requisitions from one board together. A merge that puts a queued posting with an archived one hides the role.
- **different** records the pair and splits it out of ANY cluster, and suppresses the suggestion forever. If the two are still reachable through another member, only the posting moves; the recorded decision blocks a merge back. A stranded duplicate is visible and splittable, while a silently re-merged pair is not.

A "different" decision also clears the employer links between the pair and reverts a description adopted over one (`role_clusters.clear_links_between`); the reverted rows are re-scored. The fingerprint, the daily run and the backfill never overturn a "not the same role" verdict.

## One application per role

A "same" merge makes one application (`web/same_role.py`). The merge, the decision record and the reconcile run in ONE `BEGIN IMMEDIATE` transaction. When the merged cluster holds more than one application (`role_clusters.has_application`: kept, with a state past `drafted` or an `applied_at`), the member furthest along survives (`stage_lists.representative_key`, the row the lists already show). A drafted packet is not an application and is never archived by a merge.

Each other application is retired as a duplicate: its `application_state` and `applied_at` are both cleared in one compare-and-swap, and it is archived with the reason `other` and the note "Duplicate of #N". The survivor's `app_notes` gets a line, "Merged #id (source, state s, applied_at d) as a duplicate on <date>". Any compare-and-swap miss rolls the whole thing back and refuses with a message.

Because the duplicate's application is cleared, withdrawing the survivor never resurrects the duplicate in Applied. A later "different" split does not restore the duplicate's application either; the survivor's note records it.

`blastjob-daily --duplicate-applications` lists kept application pairs that share a company key and a normalized title, clustered or not (read-only).

## One apply link per role

Each role shows one "apply here" link: the best URL across its cluster (`web/apply_link.py`, `best_apply_link`). Preference, best first:

1. The employer's own board posting, labelled with its board ("employer site (Workday)").
2. A board link closure found.
3. An employer careers-page link or the user's manual link. A limited-trust page contributes its ATS tenant root, or nothing until that is known (see [trust levels](closure.md#trust-levels)).
4. LinkedIn, then Indeed, then Adzuna, then any other non-agency source.
5. An agency posting, last.

Within a tier an active posting beats a stale one, then the lowest id wins. Every URL passes `web/urls.safe_external_url`, and one that fails is skipped. Stage list rows (from the members `stage_lists` already loads), the detail header and the daily digest (`apply_links_for`, one query for the whole list) all use it. How the digest uses it is described in [daily](daily.md).

## Manual employer link

The detail page's **Employer posting URL** form posts to `POST /jobs/{id}/employer-link` (`web/routes_employer_link.py`). It is CSRF and Origin checked. The URL must be http or https, must not be on an aggregator host, and must not be LinkedIn (`page_fetch.fetchable`). It is never fetched. It is stored with `employer_link_source='manual'`, and an empty submit clears a manual link only. A link closure found stays.

A manual link is **display-only**. Closure never probes it, never links over it, and never changes that row's verdict (an authoritative own source still decides its own posting). Nothing validated it, so a typo or a dead page must not "prove" a live job closed. Adding one stops closure tracking for that row, which is the conservative direction. It still wins as the preferred apply link.

## Company keys

`company_key` (`core/company.py`) is the one identity for an employer. It lowercases, drops apostrophes and a leading "The", strips legal suffixes (inc, llc, corp, corporation, company, ltd, plc, co), and collapses punctuation. It also drops a trailing division segment: a " - ", " – " or " | " delimiter followed by a segment naming a division, region or subsidiary ("Example Widgets Group - Global Division" becomes `example widgets group`). Group, Holdings, Brands and undelimited words stay, so two genuinely different companies are not collapsed.

Differences a pattern cannot cover safely, such as a legal name against a brand name, are handled only by the explicit `discovery.company_aliases` setting (a JSON object mapping a variant to the canonical name; empty by default). Widening a regex instead would risk merging two real companies.

Stored keys move only through `rekey_companies`, a hand-run function with no command or button (conflicts are reported and never overwritten), followed by a scoped recluster. It is deliberately never run on import.

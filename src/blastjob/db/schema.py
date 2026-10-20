"""SQLite schema as an ordered list of idempotent migrations.

Lives alongside the existing flat-file outputs in ~/.blastjob — it does NOT
replace work_history.md / templates / per-application folders. It only backs the
job-discovery pipeline.
"""

from __future__ import annotations

# Each entry is (version, sql). Applied in order; `schema_migrations` records
# which have run. Never edit a shipped migration — append a new one.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS companies (
            id           INTEGER PRIMARY KEY,
            name         TEXT NOT NULL,
            ats_type     TEXT DEFAULT '',
            board_ref    TEXT DEFAULT '',   -- slug / URL used to poll the board
            source       TEXT DEFAULT '',
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS job_clusters (
            id           INTEGER PRIMARY KEY,
            fingerprint  TEXT DEFAULT '',   -- JD/req fingerprint for display grouping
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id                 INTEGER PRIMARY KEY,
            source             TEXT NOT NULL,
            external_id        TEXT NOT NULL,
            company            TEXT DEFAULT '',
            title              TEXT DEFAULT '',
            url                TEXT DEFAULT '',
            raw_jd             TEXT DEFAULT '',

            comp_min           INTEGER DEFAULT 0,
            comp_max           INTEGER DEFAULT 0,
            comp_total_est     INTEGER DEFAULT 0,
            comp_currency      TEXT DEFAULT 'USD',
            comp_confidence    REAL DEFAULT 0.0,
            comp_provenance    TEXT DEFAULT 'unknown',

            geo_remote         INTEGER DEFAULT 0,
            geo_hybrid         INTEGER DEFAULT 0,
            geo_location_raw   TEXT DEFAULT '',
            geo_city           TEXT DEFAULT '',
            geo_region         TEXT DEFAULT '',
            geo_distance_mi    REAL,
            geo_commute_min    INTEGER,
            geo_status         TEXT DEFAULT 'unknown',

            alignment_score      INTEGER,
            score_model_version  TEXT DEFAULT '',
            rationale            TEXT DEFAULT '',

            cluster_id         INTEGER REFERENCES job_clusters(id),

            source_state       TEXT DEFAULT 'active',
            review_state       TEXT DEFAULT 'new',
            build_state        TEXT DEFAULT 'none',
            application_state  TEXT DEFAULT '',

            first_seen         TEXT NOT NULL,
            last_seen          TEXT NOT NULL,

            UNIQUE (source, external_id)
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_review ON jobs(review_state);
        CREATE INDEX IF NOT EXISTS idx_jobs_build  ON jobs(build_state);
        CREATE INDEX IF NOT EXISTS idx_jobs_cluster ON jobs(cluster_id);

        CREATE TABLE IF NOT EXISTS build_runs (
            id              INTEGER PRIMARY KEY,
            job_id          INTEGER NOT NULL REFERENCES jobs(id),
            run_id          TEXT NOT NULL UNIQUE,
            status          TEXT DEFAULT 'running',  -- running | done | failed
            lease_expires_at TEXT NOT NULL,
            artifact_dir    TEXT DEFAULT '',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_build_runs_job ON build_runs(job_id);
        """,
    ),
    (
        2,
        """
        ALTER TABLE jobs ADD COLUMN match_strengths TEXT DEFAULT '';
        ALTER TABLE jobs ADD COLUMN match_gaps TEXT DEFAULT '';
        """,
    ),
    # One ALTER per version: a single ADD COLUMN is atomic, so a mid-migration
    # failure can't leave a version half-applied and crash every later launch on
    # "duplicate column" (the risk in the two-statement migration 2 above).
    (
        3,
        "ALTER TABLE jobs ADD COLUMN applied_at TEXT DEFAULT '';",
    ),
    (
        4,
        "ALTER TABLE jobs ADD COLUMN app_notes TEXT DEFAULT '';",
    ),
    (
        5,
        "ALTER TABLE jobs ADD COLUMN next_action_due TEXT DEFAULT '';",
    ),
    # Daily digest: when a surfaced/queued job was pushed to Discord. Stamped
    # only after a confirmed send, so a failed post is retried next run.
    (
        6,
        "ALTER TABLE jobs ADD COLUMN notified_at TEXT DEFAULT '';",
    ),
    # Alert-email ingest ledger: one row per processed message (Message-ID),
    # so a message that failed to parse is retried and a processed one never
    # replays, independent of IMAP UIDs.
    (
        7,
        """
        CREATE TABLE IF NOT EXISTS mail_ledger (
            message_id   TEXT PRIMARY KEY,
            source       TEXT DEFAULT '',
            uid          TEXT DEFAULT '',
            status       TEXT DEFAULT '',
            postings     INTEGER DEFAULT 0,
            error        TEXT DEFAULT '',
            processed_at TEXT NOT NULL
        );
        """,
    ),
    # Why the user archived a job: a reason CODE (level/domain/geo/comp/company/
    # other) plus an optional note. level/domain feed the scorer as examples;
    # company auto-skips future postings from that company; geo/comp are
    # already deterministic gates and are recorded for the record only.
    (
        8,
        "ALTER TABLE jobs ADD COLUMN archive_reason TEXT DEFAULT '';",
    ),
    (
        9,
        "ALTER TABLE jobs ADD COLUMN archive_note TEXT DEFAULT '';",
    ),
    # Named people found for a kept job (hiring manager / recruiter / exec),
    # each with the URL it was found at so the user verifies before writing.
    (
        10,
        """
        CREATE TABLE IF NOT EXISTS contacts (
            id           INTEGER PRIMARY KEY,
            job_id       INTEGER NOT NULL REFERENCES jobs(id),
            name         TEXT NOT NULL,
            title        TEXT DEFAULT '',
            role         TEXT DEFAULT '',
            url          TEXT NOT NULL,
            why          TEXT DEFAULT '',
            contacted_at TEXT DEFAULT '',
            notes        TEXT DEFAULT '',
            created_at   TEXT NOT NULL,
            UNIQUE (job_id, url)
        );
        CREATE INDEX IF NOT EXISTS idx_contacts_job ON contacts(job_id);
        """,
    ),
    # Company workups as data, not just a markdown file: one profile per company
    # (typed columns for the numbers we compare across companies, JSON for the
    # rest), plus every compensation signal we observe for a job — aggregator
    # estimates, posted ranges, what the user asked for, how they reacted, offers —
    # so the negotiation history is queryable, not buried in notes.
    (
        11,
        """
        CREATE TABLE IF NOT EXISTS company_profiles (
            id                  INTEGER PRIMARY KEY,
            company_key         TEXT NOT NULL UNIQUE,   -- normalized name (lower, no punct)
            company             TEXT NOT NULL,
            legal_name          TEXT DEFAULT '',
            website             TEXT DEFAULT '',
            hq                  TEXT DEFAULT '',
            founded             INTEGER,
            industry            TEXT DEFAULT '',
            ownership           TEXT DEFAULT '',        -- public/private/PE/not-for-profit/coop/gov
            size_band           TEXT DEFAULT '',        -- e.g. "1,001-5,000" (LinkedIn)
            employees           INTEGER,
            revenue_usd         INTEGER,                -- annual, latest known
            revenue_growth_pct  REAL,                   -- YoY on that revenue
            total_assets_usd    INTEGER,                -- financials / balance-sheet companies
            net_income_usd      INTEGER,
            customers           INTEGER,                -- members / customers / users
            locations           INTEGER,                -- branches / offices / sites
            fiscal_period       TEXT DEFAULT '',        -- what the numbers describe, e.g. "Q1 2026"
            ceo                 TEXT DEFAULT '',
            ceo_since           TEXT DEFAULT '',
            it_leadership       TEXT DEFAULT '',        -- JSON list [{name,title,role,url}]
            tech_stack          TEXT DEFAULT '',        -- JSON list of {area,vendor,source}
            strategy            TEXT DEFAULT '',        -- prose: where the company is going
            recent_events       TEXT DEFAULT '',        -- JSON list [{date,event,source}]
            culture_rating      REAL,                   -- Glassdoor overall, 0-5
            culture_notes       TEXT DEFAULT '',
            risks               TEXT DEFAULT '',
            questions           TEXT DEFAULT '',        -- interview questions to ask (one per line)
            summary             TEXT DEFAULT '',        -- 3-5 sentence read
            sources             TEXT DEFAULT '',        -- JSON list [{label,url,accessed}]
            workup_path         TEXT DEFAULT '',        -- the long-form markdown, if any
            extra               TEXT DEFAULT '',        -- JSON: anything that doesn't fit above
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL
        );
        """,
    ),
    (
        12,
        """
        CREATE TABLE IF NOT EXISTS comp_signals (
            id           INTEGER PRIMARY KEY,
            job_id       INTEGER REFERENCES jobs(id),
            company_key  TEXT NOT NULL,
            kind         TEXT NOT NULL,   -- aggregator_estimate | posted_range | asked_minimum |
                                          -- employer_response | verbal_range | offer | market_band
            low_usd      INTEGER,
            high_usd     INTEGER,
            source       TEXT DEFAULT '', -- Glassdoor / ZipRecruiter / posting / recruiter call
            note         TEXT DEFAULT '',
            observed_at  TEXT NOT NULL,   -- ISO date the signal was seen/heard
            created_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_comp_signals_job ON comp_signals(job_id);
        CREATE INDEX IF NOT EXISTS idx_comp_signals_company ON comp_signals(company_key);
        """,
    ),
    # One row per `blastjob-daily` run: powers `--status` and the weekly
    # heartbeat totals. Without this, a slow/stuck run (e.g. the 95-minute
    # re-score incident on 2026-09-05) leaves no trace beyond a flat log file.
    (
        13,
        """
        CREATE TABLE IF NOT EXISTS daily_runs (
            id               INTEGER PRIMARY KEY,
            started_at       TEXT NOT NULL,
            finished_at      TEXT NOT NULL,
            duration_s       REAL NOT NULL,
            fetched          INTEGER DEFAULT 0,
            per_source       TEXT DEFAULT '',
            scored           INTEGER DEFAULT 0,
            scoring_batches  INTEGER DEFAULT 0,
            scoring_s        REAL DEFAULT 0,
            new_jobs         INTEGER DEFAULT 0,
            followups        INTEGER DEFAULT 0,
            sent             INTEGER DEFAULT 0,
            errors           TEXT DEFAULT ''
        );
        """,
    ),
    # The user's own LinkedIn connections export (their data, sanctioned download —
    # not scraping). Powers warm-path badges: who they already know at a
    # company, and whether a company is a former employer of their own.
    (
        14,
        """
        CREATE TABLE IF NOT EXISTS network_contacts (
            id            INTEGER PRIMARY KEY,
            full_name     TEXT NOT NULL,
            first_name    TEXT DEFAULT '',
            last_name     TEXT DEFAULT '',
            url           TEXT DEFAULT '',        -- LinkedIn profile URL; identity key
            email         TEXT DEFAULT '',
            company       TEXT DEFAULT '',        -- as written in the export
            company_key   TEXT DEFAULT '',        -- core.company.company_key(company)
            position      TEXT DEFAULT '',
            connected_on  TEXT DEFAULT '',        -- ISO date, or '' if unparseable
            seniority     TEXT DEFAULT '',        -- 'exec' | 'director' | 'manager' | 'ic' | ''
            is_recruiter  INTEGER DEFAULT 0,
            notes         TEXT DEFAULT '',
            imported_at   TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_network_company_key ON network_contacts(company_key);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_network_url ON network_contacts(url) WHERE url != '';
        """,
    ),
    # LinkedIn's full data-export archive (still the user's own sanctioned
    # download) carries three more signals beyond Connections.csv:
    # recommendations they received, message recency (never the message body --
    # see the PRIVACY comment in core/network.py), and companies they follow.
    # One ALTER per version, per the discipline above.
    (
        15,
        "ALTER TABLE network_contacts ADD COLUMN recommended_me INTEGER DEFAULT 0;",
    ),
    (
        16,
        "ALTER TABLE network_contacts ADD COLUMN last_message_at TEXT DEFAULT '';",
    ),
    (
        17,
        "ALTER TABLE network_contacts ADD COLUMN message_count INTEGER DEFAULT 0;",
    ),
    (
        18,
        """
        CREATE TABLE IF NOT EXISTS company_follows (
            id           INTEGER PRIMARY KEY,
            company      TEXT NOT NULL,
            company_key  TEXT DEFAULT '',
            followed_at  TEXT DEFAULT ''
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_company_follows_key ON company_follows(company_key);
        """,
    ),
    # 19-25: the agentic enrichment pass over the LinkedIn import. One ALTER per
    # version per the discipline above, so a mid-migration failure can never
    # leave a version half-applied. Stored on the CONTACT even though the model
    # only ever sees distinct title/company strings (see core/network_enrich.py
    # -- names are never sent anywhere).
    (19, "ALTER TABLE network_contacts ADD COLUMN enrich_tier TEXT DEFAULT '';"),
    (20, "ALTER TABLE network_contacts ADD COLUMN enrich_function TEXT DEFAULT '';"),
    (21, "ALTER TABLE network_contacts ADD COLUMN enrich_recruiter TEXT DEFAULT '';"),
    (22, "ALTER TABLE network_contacts ADD COLUMN enrich_decision_maker INTEGER DEFAULT 0;"),
    (23, "ALTER TABLE network_contacts ADD COLUMN enrich_canonical_company TEXT DEFAULT '';"),
    (24, "ALTER TABLE network_contacts ADD COLUMN enriched_at TEXT DEFAULT '';"),
    (25, "ALTER TABLE network_contacts ADD COLUMN enrich_model TEXT DEFAULT '';"),
    # 26: company exclusions as an explicit policy rather than an inference.
    # "Never this company" used to be deduced from ANY archived job carrying
    # archive_reason='company', which made it unreversible: unarchiving one row
    # left every sibling still satisfying the subquery, so future postings kept
    # auto-archiving while the user believed they had undone it. A row here is a
    # decision they can see and delete.
    (
        26,
        """
        CREATE TABLE IF NOT EXISTS company_exclusions (
            company_key  TEXT PRIMARY KEY,
            company      TEXT NOT NULL DEFAULT '',
            reason       TEXT DEFAULT '',
            created_at   TEXT NOT NULL
        );
        -- (Dated note, 2026-10-20, #35: lower(company) is NOT company_key(), so
        -- a name with a suffix, "The" or punctuation was stored under a key the
        -- matcher never computes and the exclusion silently lapsed. Migration 54
        -- re-keys these rows; this SQL is left as shipped.)
        -- Backfill from the old inference so a decision already made keeps
        -- working instead of silently lapsing. company_key is recomputed
        -- properly by the app; lower(company) is close enough for a one-off
        -- and any drift is corrected the next time the user archives.
        INSERT OR IGNORE INTO company_exclusions (company_key, company, reason, created_at)
        SELECT DISTINCT lower(company), company, 'migrated from archive_reason',
               datetime('now')
        FROM jobs WHERE archive_reason='company' AND company <> '';
        """,
    ),
    # 27: Greenhouse/Lever/Ashby moved from one shared source name to one per
    # board, so a failing board can no longer stale-mark a sibling's postings.
    # Existing rows still carry the bare name, and no adapter will ever report it
    # again -- so they could never be stale-marked (zombies), and the next run
    # would re-insert the same postings under the new source as BRAND NEW jobs:
    # re-scored at real cost, and eligible to be announced a second time.
    #
    # (Dated note, 2026-10-20, #35: the tokens below (greenhouse:acme, lever:globex)
    # are hardcoded, so the mapping is right only where those are the configured
    # boards; and Ashby is named above but has no UPDATE here, so bare 'ashby' rows
    # were never moved. Left as shipped.)
    #
    # There is exactly one configured board per ATS, so the mapping is
    # unambiguous. If a second board is ever added before this migration runs on
    # some other machine, the rows would be attributed to whichever token the
    # config lists -- acceptable, because the alternative is leaving them
    # permanently unreachable.
    (
        27,
        """
        UPDATE jobs SET source = 'greenhouse:acme' WHERE source = 'greenhouse';
        UPDATE jobs SET source = 'lever:globex' WHERE source = 'lever';
        """,
    ),
    # 28: a description the user pasted in by hand. Adzuna's API truncates at 500
    # characters, so without this a packet could be tailored to a paragraph.
    # When set, rediscovery no longer refreshes raw_jd (see upsert_job).
    (28, "ALTER TABLE jobs ADD COLUMN jd_pasted_at TEXT DEFAULT '';"),
    # 29-36: posting-closure lifecycle (core/closure.py). A kept job used to
    # sit in its list forever after the posting closed, because only snapshot
    # sources were ever stale-marked and nothing acted on it. These record the
    # verdict (verified_open|closed|likely_closed|unverifiable|''), when it was
    # checked, when closure was first proven, whether the user set it by hand
    # (a manual verdict is never overturned by automation), the application
    # state automation replaced (so a reopen can restore it), and the matched
    # posting on the employer's own board for sources that cannot prove closure
    # themselves (LinkedIn alerts, Adzuna). One ALTER per version, as above.
    (29, "ALTER TABLE jobs ADD COLUMN posting_check TEXT DEFAULT '';"),
    (30, "ALTER TABLE jobs ADD COLUMN posting_checked_at TEXT DEFAULT '';"),
    (31, "ALTER TABLE jobs ADD COLUMN posting_closed_at TEXT DEFAULT '';"),
    (32, "ALTER TABLE jobs ADD COLUMN posting_check_manual INTEGER DEFAULT 0;"),
    (33, "ALTER TABLE jobs ADD COLUMN closed_prior_state TEXT DEFAULT '';"),
    (34, "ALTER TABLE jobs ADD COLUMN employer_link_source TEXT DEFAULT '';"),
    (35, "ALTER TABLE jobs ADD COLUMN employer_link_external_id TEXT DEFAULT '';"),
    (36, "ALTER TABLE jobs ADD COLUMN employer_link_url TEXT DEFAULT '';"),
    # 37: when application_state last changed, stamped by every write that
    # moves it (web/service.set_application_state and the closure lifecycle).
    # The Closed stage sorts by it; rows closed before it existed fall back to
    # posting_closed_at, then applied_at.
    (37, "ALTER TABLE jobs ADD COLUMN stage_changed_at TEXT DEFAULT '';"),
    # 38: the user's verdict on "are these two postings the same role?"
    # (core/role_clusters.py). One row per ordered pair (job_a < job_b).
    # 'different' suppresses the pair forever: no suggestion, no auto-cluster,
    # no backfill merge. 'same' records a link they confirmed. The primary key is
    # the pair index, so this stays one statement.
    (
        38,
        """
        CREATE TABLE IF NOT EXISTS same_role_decisions (
            job_a       INTEGER NOT NULL,
            job_b       INTEGER NOT NULL,
            decision    TEXT NOT NULL CHECK (decision IN ('same', 'different')),
            decided_at  TEXT NOT NULL,
            PRIMARY KEY (job_a, job_b),
            CHECK (job_a < job_b)
        );
        """,
    ),
    # 39: as-sent attribution (core/packet_sends.py). A snapshot of exactly
    # which documents went out, hashed at the moment the user marks a job applied.
    # Immutable by design: packets get rebuilt (three were, on 2026-09-16), so
    # hashing whatever sits on disk later would attribute a screen to a
    # document that was never sent.
    (
        39,
        """
        CREATE TABLE IF NOT EXISTS packet_sends (
            id                        INTEGER PRIMARY KEY,
            job_id                    INTEGER NOT NULL REFERENCES jobs(id),
            artifact_dir              TEXT DEFAULT '',
            resume_path               TEXT DEFAULT '',
            resume_sha256             TEXT NOT NULL,
            cover_letter_path         TEXT DEFAULT '',
            cover_letter_sha256       TEXT DEFAULT '',
            jd_sha256                 TEXT DEFAULT '',
            prompt_version            TEXT DEFAULT '',
            model                     TEXT DEFAULT '',
            groundedness              INTEGER,
            jd_phrasing               INTEGER,
            cover_letter_groundedness INTEGER,
            fatal_findings_json       TEXT DEFAULT '[]',
            sent_at                   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_packet_sends_job ON packet_sends(job_id);
        """,
    ),
    # 40: append-only outcome log per send. Never updated, never deleted —
    # what happened to a packet is history, and history is not edited.
    (
        40,
        """
        CREATE TABLE IF NOT EXISTS packet_outcomes (
            id          INTEGER PRIMARY KEY,
            send_id     INTEGER NOT NULL REFERENCES packet_sends(id),
            outcome     TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            note        TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_packet_outcomes_send ON packet_outcomes(send_id);
        """,
    ),
    # 41: the three kept-stage lists (Shortlist/Applied/Closed, web/stages.py)
    # filter on review_state='kept' AND COALESCE(application_state,'') IN (...).
    # application_state has no NOT NULL constraint (even though the app never
    # actually writes NULL to it), so a plain index on the column can't serve
    # a query built around the COALESCE expression. EXPLAIN QUERY PLAN against
    # a copy of the real DB confirms SQLite picks this index for all three
    # predicates once it exists ("SEARCH jobs USING INDEX idx_jobs_kept_appstate
    # (review_state=? AND <expr>=?)"). No measurable wall-clock difference at
    # today's 23 kept rows -- review_state alone already narrows that far via
    # idx_jobs_review -- but it's the structurally correct index as that count
    # grows over years of job search.
    (
        41,
        "CREATE INDEX IF NOT EXISTS idx_jobs_kept_appstate "
        "ON jobs(review_state, COALESCE(application_state,''));",
    ),
    # 42: what happened in an application, dated (core/events.py). Written by
    # a state change (source 'state', in the same transaction as the move),
    # the closure lifecycle ('closure'), the reply scanner ('replies'), the
    # history backfill ('backfill') and the user ('manual' -- the only source a
    # delete may touch). `kind` is validated in Python against EVENT_KINDS.
    (
        42,
        """
        CREATE TABLE IF NOT EXISTS application_events (
            id          INTEGER PRIMARY KEY,
            job_id      INTEGER NOT NULL REFERENCES jobs(id),
            kind        TEXT NOT NULL,
            at          TEXT NOT NULL,
            note        TEXT DEFAULT '',
            ref         TEXT DEFAULT '',
            source      TEXT DEFAULT 'manual',
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_job ON application_events(job_id, at);
        """,
    ),
    # 43: an index of the files written for a job (core/artifacts.py), keyed
    # by absolute path. Two output layouts exist on disk and prep packs used to
    # land in a folder recorded nowhere; this is where a document's location
    # is written down instead of recomputed.
    (
        43,
        """
        CREATE TABLE IF NOT EXISTS artifacts (
            id            INTEGER PRIMARY KEY,
            job_id        INTEGER REFERENCES jobs(id),
            build_run_id  INTEGER REFERENCES build_runs(id),
            kind          TEXT NOT NULL,
            path          TEXT NOT NULL UNIQUE,
            sha256        TEXT DEFAULT '',
            bytes         INTEGER DEFAULT 0,
            note          TEXT DEFAULT '',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id);
        """,
    ),
    # 44: every setting lives here, one row per dotted leaf key
    # ("discovery.comp_floor_usd") with a JSON value (blastjob/settings_store.py).
    # Unknown keys are carried untouched; saves upsert only what changed.
    (
        44,
        """
        CREATE TABLE IF NOT EXISTS settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        """,
    ),
    # 45-52: employer-sourced job description provenance (issue #17). One ALTER
    # per version, as above. jd_source is 'employer:<link source>'; jd_prior holds
    # the aggregator text an adoption replaced (for undo); jd_lookup_* drive the
    # retry backoff; score_stale marks a kept row whose JD changed under its score.
    (45, "ALTER TABLE jobs ADD COLUMN jd_source TEXT DEFAULT '';"),
    (46, "ALTER TABLE jobs ADD COLUMN jd_source_ref TEXT DEFAULT '';"),
    (47, "ALTER TABLE jobs ADD COLUMN jd_fetched_at TEXT DEFAULT '';"),
    (48, "ALTER TABLE jobs ADD COLUMN jd_prior TEXT DEFAULT '';"),
    (49, "ALTER TABLE jobs ADD COLUMN jd_lookup_at TEXT DEFAULT '';"),
    (50, "ALTER TABLE jobs ADD COLUMN jd_lookup_status TEXT DEFAULT '';"),
    (51, "ALTER TABLE jobs ADD COLUMN jd_lookup_failures INTEGER DEFAULT 0;"),
    (52, "ALTER TABLE jobs ADD COLUMN score_stale INTEGER DEFAULT 0;"),
    # How a research run tied its pages to the employer: 'anchored' (domain or
    # location match), 'judged' (model judgment, not confirmed), '' (not recorded).
    (53, "ALTER TABLE company_profiles ADD COLUMN identity_basis TEXT DEFAULT '';"),
    # 54: re-key company_exclusions with company_key() (#35). Migration 26
    # backfilled keys with lower(company), which the matcher never computes, so
    # those exclusions silently lapsed. The re-key needs Python (company_key is
    # not SQL), so the work is `db.client._POST_MIGRATION_STEPS[54]`; this entry
    # only records that the step ran.
    (54, "SELECT 1;"),
    # 55: when a stale kept row's re-score last FAILED (#31). The daily pass takes
    # at most 10 rows, so rows that always fail would hold every slot forever.
    # Never-attempted ('') sort first, then the oldest attempt.
    (55, "ALTER TABLE jobs ADD COLUMN score_stale_attempted_at TEXT DEFAULT '';"),
]

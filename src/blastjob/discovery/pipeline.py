"""Run discovery adapters -> normalize -> dedupe/cluster -> upsert into the DB.

Design points:
- Adapters run concurrently; one source failing never sinks the run (its error is
  recorded and its postings are simply absent this cycle).
- Excluded companies (current/prior employers + recruiters) are dropped before
  they ever reach the DB.
- Identity is (source, external_id); rediscovery refreshes source-owned fields
  only (see db.client.upsert_job) so user decisions survive.
- Cross-source duplicates are grouped into a reversible cluster (display only),
  never destructively merged.
- Stale-marking is per-source and ONLY for sources that fetched successfully, so
  a transient outage doesn't wrongly mark everything gone. It is also only as
  strong as what the source promised (see base.Adapter): a FULL board listing
  proves absence, a WINDOWED one (Workday's 100-per-query cap, Adzuna's page 1)
  does not, so windowed sources age out on a per-source TTL and, where the
  source has a detail endpoint, only after it confirms the posting is gone.
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from blastjob import config as cfg_mod
from blastjob import logging as applog
from blastjob.core.closure_links import is_authoritative
from blastjob.core.company import company_key
from blastjob.core.target_store import dream_keys
from blastjob.discovery import dream_boards
from blastjob.discovery.adzuna import AdzunaAdapter
from blastjob.discovery.apple import AppleAdapter
from blastjob.discovery.ashby import AshbyAdapter
from blastjob.discovery.base import Adapter, RawPosting, is_snapshot, stale_after_days
from blastjob.discovery.dayforce import MAX_PAGES as DAYFORCE_MAX_PAGES
from blastjob.discovery.dayforce import DayforceAdapter
from blastjob.discovery.greenhouse import GreenhouseAdapter
from blastjob.discovery.lever import LeverAdapter
from blastjob.discovery.mailalerts import (
    INDEED_PLACEHOLDER_TITLE,
    LedgerEntry,
    MailAlertAdapter,
    known_company_names,
)
from blastjob.discovery.normalize import cluster_fingerprint, to_job_columns
from blastjob.discovery.searchfirms import SearchFirmAdapter, due_today
from blastjob.discovery.workday import MAX_PAGES_PER_QUERY, WorkdayAdapter
from blastjob.models.config import BlastJobConfig
from blastjob.secrets import secrets_file_hint

_ATS: dict[str, Callable[..., Adapter]] = {
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "ashby": AshbyAdapter,
}

MILES_TO_KM = 1.60934

# Closure probes are one HTTP call each; cap per source per run so a board with
# a long tail of aged-out postings can't stretch a daily run. Anything not
# probed this run simply stays active and is reconsidered next run — the
# conservative direction.
MAX_CLOSURE_PROBES_PER_SOURCE = 25
# Short-snapshot guard (see _short_snapshot_reason).
MIN_ACTIVE_EMPTY_GUARD = 3
MIN_ACTIVE_HALF_GUARD = 3  # same floor as the empty guard: a 6-posting board is not exempt


@dataclass
class DiscoveryResult:
    fetched: int = 0
    inserted_or_updated: int = 0
    skipped_excluded: int = 0
    stale_marked: int = 0
    per_source: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    # Non-failures worth a human's attention — a channel that ran cleanly and
    # produced nothing, which otherwise looks identical to one that is broken.
    notes: dict[str, str] = field(default_factory=dict)
    # Cluster merges the same-role auto rule made for postings new this run.
    reclustered: int = 0


# SQL twin of normalize.PLACEHOLDER_LOCATION (^\d+\s+locations?$). SQLite has
# no regex, so: a leading digit (GLOB is case-sensitive, LIKE is not) and a
# "... location(s)" tail. Verified against the live DB 2026-09-12 — matches
# exactly the 128 rows the regex rejects, and not "Building 5 Locations Campus".
_STORED_PLACEHOLDER_LOCATION = (
    "TRIM(geo_location_raw) GLOB '[0-9]* [Ll]ocation*' "
    "AND (TRIM(geo_location_raw) LIKE '% location' OR TRIM(geo_location_raw) LIKE '% locations')"
)


def known_jd_ids(conn: sqlite3.Connection, source: str) -> set[str]:
    """External ids under `source` that already carry a JD — the adapter can
    skip their detail fetch. Ids with an empty JD are deliberately NOT included
    so a posting whose detail fetch failed once is retried next run.

    Nor is a row whose stored location is a placeholder count ("5 Locations").
    Those rows are the wreckage of the old refresh path, and they cannot repair
    themselves: they hold a JD, so they take the presence-stub path, and the
    presence stub now (correctly) writes no location at all. Excluding them buys
    exactly ONE more detail GET each — after which the row holds a real location,
    stops matching this predicate, and drops back onto the cheap path. The repair
    also spreads itself out, since only the ids inside a run's query window are
    re-fetched at all.
    """
    rows = conn.execute(
        "SELECT external_id FROM jobs "
        f"WHERE source=? AND raw_jd <> '' AND NOT ({_STORED_PLACEHOLDER_LOCATION})",
        (source,),
    ).fetchall()
    return {str(r["external_id"]) for r in rows}


def processed_message_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT message_id FROM mail_ledger WHERE status IN ('ok','ignored')")
    return {str(r["message_id"]) for r in rows.fetchall()}


def record_ledger(conn: sqlite3.Connection, entries: list[LedgerEntry]) -> None:
    from datetime import datetime

    now = datetime.now().isoformat()
    conn.executemany(
        "INSERT OR REPLACE INTO mail_ledger "
        "(message_id, source, uid, status, postings, error, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(e.message_id, e.source, e.uid, e.status, e.postings, e.error, now) for e in entries],
    )


class MailAlertsMisconfigured(RuntimeError):
    """Enabled in config but the IMAP credentials are not in the environment."""


def _mail_adapter(cfg: BlastJobConfig, conn: sqlite3.Connection | None) -> Adapter:
    d = cfg.discovery
    user = os.environ.get(d.mail_user_env, "")
    password = os.environ.get(d.mail_password_env, "")
    if not user or not password:

        class Broken:
            source = MailAlertAdapter.source
            snapshot = False

            async def fetch(self) -> list[RawPosting]:
                raise MailAlertsMisconfigured(
                    f"set {d.mail_user_env} and {d.mail_password_env} in {secrets_file_hint()}"
                )

        return Broken()
    return MailAlertAdapter(
        host=d.mail_imap_host,
        user=user,
        password=password,
        skip_message_ids=processed_message_ids(conn) if conn is not None else set(),
        lookback_days=d.mail_lookback_days,
        mailbox=d.mail_mailbox,
        timeout_s=d.provider_timeout_s,
        debug_dir=cfg_mod.data_dir(cfg) / "mail-debug",
        known_companies=known_company_names(conn) if conn is not None else frozenset(),
    )


def build_adapters(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection | None = None,
    *,
    today: date | None = None,
) -> list[Adapter]:
    d = cfg.discovery
    adapters: list[Adapter] = []
    if d.mail_alerts_enabled:
        adapters.append(_mail_adapter(cfg, conn))
    dreams = dream_keys(conn) if conn is not None else frozenset()
    for b in d.target_boards:
        # A dream company's board is read with its own queries and caps (#90).
        dream = company_key(b.company) in dreams
        if b.ats.lower() == "workday" and b.token:
            wd = WorkdayAdapter(
                b.company,
                b.token,
                queries=dream_boards.dream_queries(d.title_keep_terms)
                if dream
                else d.adzuna_queries,
                timeout_s=d.provider_timeout_s,
                max_retries=d.provider_max_retries,
                keep_terms=d.title_keep_terms,
                drop_terms=d.title_drop_terms,
                max_pages=dream_boards.DREAM_MAX_PAGES if dream else MAX_PAGES_PER_QUERY,
                max_wanted=dream_boards.DREAM_MAX_POSTINGS if dream else 0,
            )
            if conn is not None:
                wd.known_ids = known_jd_ids(conn, wd.source)
            adapters.append(wd)
            continue
        if b.ats.lower() == "dayforce" and b.token:
            df = DayforceAdapter(
                b.company,
                b.token,
                queries=d.adzuna_queries,
                timeout_s=d.provider_timeout_s,
                max_retries=d.provider_max_retries,
                keep_terms=d.title_keep_terms,
                drop_terms=d.title_drop_terms,
                max_pages=dream_boards.DREAM_MAX_PAGES if dream else DAYFORCE_MAX_PAGES,
                max_wanted=dream_boards.DREAM_MAX_POSTINGS if dream else 0,
            )
            if conn is not None:
                df.known_ids = known_jd_ids(conn, df.source)
            adapters.append(df)
            continue
        cls = _ATS.get(b.ats.lower())
        if cls and b.token:
            board = cls(
                b.company,
                b.token,
                timeout_s=d.provider_timeout_s,
                max_retries=d.provider_max_retries,
            )
            adapters.append(
                dream_boards.CappedBoardAdapter(board, d.title_keep_terms, d.title_drop_terms)
                if dream
                else board
            )
    if d.enable_aggregators:
        adapters.append(
            AdzunaAdapter(
                queries=d.adzuna_queries,
                where=d.adzuna_where,
                distance_km=int(d.commute_radius_miles * MILES_TO_KM),
                salary_min=0,  # our own gate handles comp; don't let Adzuna hide roles
                app_id_env=d.adzuna_app_id_env,
                app_key_env=d.adzuna_app_key_env,
                timeout_s=d.provider_timeout_s,
                max_retries=d.provider_max_retries,
            )
        )
    # Retained-search firms are polled once a week (see searchfirms.py): the
    # pipeline simply doesn't build the adapter on any other weekday.
    weekly_due = due_today(today or date.today(), d.search_firm_weekday)
    if d.search_firms_enabled and d.search_firms and weekly_due:
        adapters.append(build_search_firm_adapter(cfg, conn))
    # Guarded on apple_queries too (not just apple_enabled): with no title
    # queries configured, `AppleAdapter.fetch()` would otherwise raise on a
    # missing Jina key before it ever reaches its empty query x location
    # product -- a confusing per-source error for a fresh install that just
    # hasn't set apple_queries yet.
    if d.apple_enabled and d.apple_queries:
        adapters.append(build_apple_adapter(cfg, conn))
    return adapters


def build_apple_adapter(
    cfg: BlastJobConfig, conn: sqlite3.Connection | None = None
) -> AppleAdapter:
    """Construct the Apple careers adapter (Jina reader on jobs.apple.com
    search URLs — see discovery/apple.py)."""
    d = cfg.discovery
    ap = AppleAdapter(
        queries=d.apple_queries,
        locations=d.apple_locations,
        api_key=os.environ.get("JINA_API_KEY", ""),
        max_reads=d.apple_max_reads,
        timeout_s=d.provider_timeout_s,
    )
    if conn is not None:
        ap.known_ids = known_jd_ids(conn, ap.source)
    return ap


def build_search_firm_adapter(
    cfg: BlastJobConfig, conn: sqlite3.Connection | None = None
) -> SearchFirmAdapter:
    """Construct the retained-search-firm adapter on demand (used by the
    weekday gate in `build_adapters` and by `daily.sweep_search_firms` for an
    ad-hoc run outside the weekly schedule)."""
    d = cfg.discovery
    sf = SearchFirmAdapter(
        d.search_firms,
        queries=d.search_firm_queries,
        regions=d.search_firm_regions,
        api_key=os.environ.get("JINA_API_KEY", ""),
        timeout_s=d.provider_timeout_s,
        keep_terms=d.title_keep_terms,
        drop_terms=d.title_drop_terms,
        places=d.search_firm_places,
        home_state=d.home_state,
    )
    if conn is not None:
        sf.known_ids = {
            str(r["external_id"])
            for r in conn.execute(
                "SELECT external_id FROM jobs WHERE source LIKE 'searchfirm:%' AND raw_jd <> ''"
            ).fetchall()
        }
    return sf


def _is_excluded(company: str, excludes: list[str]) -> bool:
    """Whole-word match, case-insensitive: 'Meta' drops 'Meta Platforms', not 'Metavante'.
    Like the title drop terms, but bounded by non-word characters rather than `\\b`, so a
    term that starts or ends in punctuation ('Acme, Inc.') still matches."""
    c = company.lower()
    for x in excludes:
        term = (x or "").strip().lower()
        if term and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", c):
            return True
    return False


def _assign_cluster(conn: sqlite3.Connection, fingerprint: str) -> int:
    row = conn.execute("SELECT id FROM job_clusters WHERE fingerprint=?", (fingerprint,)).fetchone()
    if row:
        return int(row["id"])
    from datetime import datetime

    cur = conn.execute(
        "INSERT INTO job_clusters (fingerprint, created_at) VALUES (?, ?)",
        (fingerprint, datetime.now().isoformat()),
    )
    return int(cur.lastrowid or 0)


async def _fetch_all(
    adapters: list[Adapter],
) -> list[tuple[str, bool, list[RawPosting] | Exception]]:
    async def one(a: Adapter):
        try:
            return a.source, is_snapshot(a), await a.fetch()
        except Exception as e:  # noqa: BLE001 — record and continue; never sink the run
            return a.source, is_snapshot(a), e

    return await asyncio.gather(*(one(a) for a in adapters))


@dataclass
class _SaveCtx:
    """What saving one source's postings needs, bundled so the per-posting and
    per-message paths share `save`."""

    conn: sqlite3.Connection
    source: str
    excludes: list[str]
    seen: set[str]
    result: DiscoveryResult

    def save(self, p: RawPosting, *, commit: bool) -> bool:
        """Upsert one posting; True when it was written."""
        from blastjob.db import client as dbc

        if not p.external_id:
            return False
        if _is_excluded(p.company, self.excludes):
            self.result.skipped_excluded += 1
            return False
        self.seen.add(p.external_id)
        # A multi-vendor adapter (alert emails) tags each posting with its
        # real origin ("linkedin"/"indeed"); store under that so the cockpit
        # shows where a job came from and cross-source clustering works.
        job_source = p.source or self.source
        cols = to_job_columns(p)
        cluster_id = _cluster_for(self.conn, p, job_source)
        if cluster_id:
            cols["cluster_id"] = cluster_id
        # A whole-card alert's "title" is the entire card text: it must not
        # overwrite a title that was parsed or repaired properly.
        dbc.upsert_job(
            self.conn,
            source=job_source,
            external_id=p.external_id,
            commit=commit,
            refresh_title=not p.extra.get("whole_card"),
            **cols,
        )
        self.result.inserted_or_updated += 1
        return True


def _persist_batches(
    ctx: _SaveCtx,
    batches: list[tuple[LedgerEntry, list[RawPosting]]],
    errs: list[str],
    saved_keys: set[tuple[str, str]],
) -> None:
    """Save each alert message's postings and its ledger row in ONE transaction.

    The ledger row is what makes a message "done" (status ok/ignored is never
    fetched again), so it must never land without its postings. Each posting
    saves inside its own SAVEPOINT: one that always fails (recorded as a source
    error) rolls back alone, so it cannot sink its siblings every day. The
    message is 'ok' if any posting saved and 'error' (retried next run) only if
    none did. A failure of the ledger write itself rolls the whole message back
    and records it as 'error'; the other messages are unaffected."""
    for entry, postings in batches:
        ctx.conn.commit()  # nothing pending leaks into or out of this message
        before = ctx.result.inserted_or_updated
        try:
            fresh = [p for p in postings if (p.source, p.external_id) not in saved_keys]
            saved, failed = _save_postings(ctx, fresh, errs, entry)
            final = entry if saved or not failed else _failed_entry(entry, failed[0])
            record_ledger(ctx.conn, [final])
            ctx.conn.commit()
            saved_keys.update((p.source, p.external_id) for p in saved)
        except Exception as e:  # noqa: BLE001 — see docstring
            ctx.conn.rollback()
            ctx.result.inserted_or_updated = before
            errs.append(f"{entry.message_id}: {type(e).__name__}: {e}")
            _record_failed_message(ctx.conn, entry, e)


def _save_postings(
    ctx: _SaveCtx, postings: list[RawPosting], errs: list[str], entry: LedgerEntry
) -> tuple[list[RawPosting], list[Exception]]:
    """Save each posting under its own SAVEPOINT; (saved, failures)."""
    saved: list[RawPosting] = []
    failed: list[Exception] = []
    for p in postings:
        ctx.conn.execute("SAVEPOINT posting")
        try:
            ctx.save(p, commit=False)
        except Exception as e:  # noqa: BLE001 — one bad posting never sinks the message
            ctx.conn.execute("ROLLBACK TO posting")
            failed.append(e)
            errs.append(f"{entry.message_id}/{p.external_id}: {type(e).__name__}: {e}")
        else:
            saved.append(p)
        finally:
            ctx.conn.execute("RELEASE posting")
    return saved, failed


def _failed_entry(entry: LedgerEntry, exc: Exception) -> LedgerEntry:
    return replace(entry, status="error", postings=0, error=repr(exc)[:200])


def _record_failed_message(conn: sqlite3.Connection, entry: LedgerEntry, exc: Exception) -> None:
    try:
        record_ledger(conn, [_failed_entry(entry, exc)])
        conn.commit()
    except Exception:  # noqa: BLE001 — no row at all is also retryable
        conn.rollback()


async def run_discovery(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    adapters: list[Adapter] | None = None,
) -> DiscoveryResult:
    from blastjob.db import client as dbc
    from blastjob.secrets import load_secrets_env

    load_secrets_env()  # make the secrets file's keys (Adzuna, IMAP) available
    result = DiscoveryResult()
    excludes = cfg.discovery.exclude_companies
    if adapters is None:
        adapters = build_adapters(cfg, conn)

    run_started = dbc._now()
    results = await _fetch_all(adapters)
    ok_sources: list[str] = []
    seen_ids: dict[str, set[str]] = {}

    # An adapter that exposes a ledger but not per-message batches has no
    # transactional pairing to offer; record its ledger up front as before.
    for a in adapters:
        ledger = getattr(a, "ledger", None)
        if ledger and not hasattr(a, "batches"):
            record_ledger(conn, ledger)
            conn.commit()

    failed_sources: set[str] = set()
    save_errors: dict[str, list[str]] = {}
    saved_keys: set[tuple[str, str]] = set()
    for adapter, (source, snapshot, payload) in zip(adapters, results, strict=True):
        if isinstance(payload, Exception):
            # Redacted, because this string does not stay here: it is persisted
            # into `daily_runs.errors`, rendered on the funnel page to any LAN
            # browser, pushed to Discord and streamed over SSE. httpx puts the
            # full request URL in its exception message, and Adzuna's
            # credentials travel as query parameters -- that combination wrote
            # the app_key into the log 284 times before 2026-09-16.
            result.errors[source] = applog.redact(f"{type(payload).__name__}: {payload}")
            failed_sources.add(source)
            continue
        if snapshot:
            ok_sources.append(source)
        seen_ids.setdefault(source, set())
        # Sum, never assign: several boards can share one adapter source name,
        # and the last one's count is not the run's count.
        result.per_source[source] = result.per_source.get(source, 0) + len(payload)
        result.fetched += len(payload)
        errs = save_errors.setdefault(source, [])
        ctx = _SaveCtx(conn, source, excludes, seen_ids[source], result)
        batches = getattr(adapter, "batches", None)
        if batches is not None:
            _persist_batches(ctx, batches, errs, saved_keys)
            continue
        for p in payload:
            try:
                ctx.save(p, commit=True)
            except Exception as e:  # noqa: BLE001 — one bad posting never sinks the rest
                conn.rollback()
                errs.append(f"{p.external_id}: {type(e).__name__}: {e}")
    for source, errs in save_errors.items():
        if errs:
            result.errors[source] = applog.redact(
                f"{len(errs)} posting(s)/message(s) not saved this run; first: {errs[0]}"
            )

    result.reclustered = _recluster_new(conn, cfg, run_started)
    _report_silent_channels(conn, adapters, result)
    # Belt and braces behind the per-board `source` namespacing: if ANY adapter
    # reporting a given source failed this run, that source's view of the world
    # is incomplete and nothing under it may be stale-marked — a success from a
    # sibling sharing the name is not evidence about the failed one's postings.
    swept = [s for s in dict.fromkeys(ok_sources) if s not in failed_sources]
    await _mark_stale(conn, adapters, swept, seen_ids, result)
    conn.commit()
    return result


def _lacks_identity(p: RawPosting) -> bool:
    """The placeholder title of an Indeed text alert, or a posting with neither a
    company nor any location: its fingerprint says nothing about the role."""
    if p.title.strip().lower() == INDEED_PLACEHOLDER_TITLE.lower():
        return True
    has_place = bool(p.city.strip() or p.region.strip() or p.location_raw.strip())
    return not p.company.strip() and not has_place


def _cluster_for(conn: sqlite3.Connection, p: RawPosting, job_source: str) -> int:
    """The cluster this posting belongs to.

    A posting we already hold keeps its stored cluster. Grouping is decided
    once on insert and afterwards only by explicit merges and splits
    (core/role_clusters.py): re-fingerprinting on refresh would undo a split
    the user made, dissolve a merge they confirmed, and shunt a presence stub (no
    location, so a different fingerprint) into a fresh cluster every day.

    A new posting joins its fingerprint's cluster, unless that cluster already
    holds a different req from the same authoritative board: two reqs on one
    board are two roles, so it gets a cluster of its own.
    """
    row = conn.execute(
        "SELECT cluster_id FROM jobs WHERE source=? AND external_id=?",
        (job_source, p.external_id),
    ).fetchone()
    if row is not None and row["cluster_id"]:
        return int(row["cluster_id"])
    fingerprint = cluster_fingerprint(p)
    if _lacks_identity(p):
        # No company/title/place to fingerprint on: it would join every other
        # such posting. Its own cluster; a human merge can still join it later.
        return _assign_cluster(conn, f"{fingerprint}#{job_source}:{p.external_id}")
    cluster_id = _assign_cluster(conn, fingerprint)
    if is_authoritative(job_source) and _board_sibling(conn, cluster_id, job_source, p.external_id):
        return _assign_cluster(conn, f"{fingerprint}#{job_source}:{p.external_id}")
    return cluster_id


def _board_sibling(
    conn: sqlite3.Connection, cluster_id: int, source: str, external_id: str
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE cluster_id=? AND source=? AND external_id<>? LIMIT 1",
        (cluster_id, source, external_id),
    ).fetchone()
    return row is not None


def _recluster_new(conn: sqlite3.Connection, cfg: BlastJobConfig, since: str) -> int:
    """Apply the same-role auto rule to groups holding a posting first seen
    this run. Merge-only; honours every 'different' decision."""
    from blastjob.core import recluster

    new_ids = {
        int(r["id"]) for r in conn.execute("SELECT id FROM jobs WHERE first_seen >= ?", (since,))
    }
    if not new_ids:
        return 0
    radius = cfg.discovery.commute_radius_miles
    plan = recluster.plan_recluster(conn, radius, cfg.discovery, new_ids)
    return recluster.apply_recluster(conn, plan)


def _report_silent_channels(
    conn: sqlite3.Connection, adapters: list[Adapter], result: DiscoveryResult
) -> None:
    """Turn "ran fine, found nothing, again" into something the user can see.

    A retained-search firm that has never produced a single posting is either
    not publishing assignments at all or being searched the wrong way — either
    way it is a dead channel wearing a working channel's clothes. Reported as a
    NOTE, not an error: nothing failed.
    """
    for a in adapters:
        for rep in getattr(a, "firm_reports", None) or []:
            if not rep.silent:
                continue
            ever = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE source=?", (rep.source,)
            ).fetchone()["n"]
            if ever:
                continue
            result.notes[rep.source] = (
                f"{rep.name}: {rep.queries} queries, {rep.results} results, "
                f"{rep.errors} errors, 0 postings kept — this channel has never "
                f"yielded a posting; its site may not publish assignments."
            )


async def _mark_stale(
    conn: sqlite3.Connection,
    adapters: list[Adapter],
    ok_sources: list[str],
    seen_ids: dict[str, set[str]],
    result: DiscoveryResult,
) -> None:
    """Stale-mark within SNAPSHOT sources that fetched successfully this run.

    A FULL snapshot listed the whole board, so one absence is proof. A WINDOWED
    source only saw a slice, so absence is evidence that has to accumulate:
    the posting ages out after `stale_after_days` of silence, and if the adapter
    can ask the source directly (`confirm_open`) nothing is marked gone until it
    says so. An incremental source's silence never means anything.
    """
    by_source = {a.source: a for a in adapters}
    now = datetime.now().isoformat()
    for source in ok_sources:
        ids = seen_ids.get(source, set())
        rows = conn.execute(
            "SELECT id, external_id, url, last_seen FROM jobs "
            "WHERE source=? AND source_state='active'",
            (source,),
        ).fetchall()
        missing = [r for r in rows if r["external_id"] not in ids]
        if not missing:
            continue
        adapter = by_source.get(source)
        partial = getattr(adapter, "partial_listing_note", "")
        if partial:
            # The adapter returned items but knows it did not read the whole board.
            result.notes[source] = f"{partial}; stale-marking skipped this run"
            continue
        ttl = stale_after_days(adapter)
        returned = result.per_source.get(source, 0)
        why = _short_snapshot_reason(len(rows), len(rows) - len(missing), returned, ttl)
        if why:
            result.notes[source] = f"{why}; stale-marking skipped this run"
            continue
        if ttl <= 0:
            for r in missing:
                _mark_one_stale(conn, r["id"], result)
            continue

        cutoff = (datetime.now() - timedelta(days=ttl)).isoformat()
        aged = sorted(
            (r for r in missing if (r["last_seen"] or "") < cutoff),
            key=lambda r: r["last_seen"] or "",
        )
        if not aged:
            continue
        confirm = getattr(adapter, "confirm_open", None)
        if confirm is None:
            for r in aged:
                _mark_one_stale(conn, r["id"], result)
            continue
        batch = aged[:MAX_CLOSURE_PROBES_PER_SOURCE]
        try:
            still_open = await confirm([(r["external_id"], r["url"] or "") for r in batch])
        except Exception:  # noqa: BLE001 — a failed probe is not evidence of closure
            continue
        for r in batch:
            if r["external_id"] in still_open:
                # Still on the board, just outside today's query window: restart
                # its clock so it isn't re-probed every run.
                conn.execute("UPDATE jobs SET last_seen=? WHERE id=?", (now, r["id"]))
            else:
                _mark_one_stale(conn, r["id"], result)


def _short_snapshot_reason(active: int, still_listed: int, returned: int, ttl: int) -> str:
    """Why a snapshot that "succeeded" is too thin to retire postings, or "".

    A 200 with an empty or truncated body (maintenance page, broken filter, a
    half-served listing) is indistinguishable from "everything closed", and for
    an authoritative source stale means closed. So: zero rows back while the
    board held several is never believed, and a FULL snapshot (ttl 0) that lost
    more than half of a big board is not either. The half rule skips WINDOWED
    sources, which see a slice of their board every run by design."""
    if returned == 0 and active >= MIN_ACTIVE_EMPTY_GUARD:
        return f"snapshot returned 0 postings while {active} are active"
    if ttl <= 0 and active >= MIN_ACTIVE_HALF_GUARD and still_listed * 2 < active:
        return f"snapshot listed only {still_listed} of {active} active postings"
    return ""


def _mark_one_stale(conn: sqlite3.Connection, job_id: int, result: DiscoveryResult) -> None:
    conn.execute("UPDATE jobs SET source_state='stale' WHERE id=?", (job_id,))
    result.stale_marked += 1

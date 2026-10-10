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
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from blastjob import config as cfg_mod
from blastjob import logging as applog
from blastjob.core.closure_links import is_authoritative
from blastjob.discovery.adzuna import AdzunaAdapter
from blastjob.discovery.apple import AppleAdapter
from blastjob.discovery.ashby import AshbyAdapter
from blastjob.discovery.base import Adapter, RawPosting, is_snapshot, stale_after_days
from blastjob.discovery.dayforce import DayforceAdapter
from blastjob.discovery.greenhouse import GreenhouseAdapter
from blastjob.discovery.lever import LeverAdapter
from blastjob.discovery.mailalerts import LedgerEntry, MailAlertAdapter, known_company_names
from blastjob.discovery.normalize import cluster_fingerprint, to_job_columns
from blastjob.discovery.searchfirms import SearchFirmAdapter, due_today
from blastjob.discovery.workday import WorkdayAdapter
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


# SQL twin of normalize._PLACEHOLDER_LOCATION (^\d+\s+locations?$). SQLite has
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
    for b in d.target_boards:
        if b.ats.lower() == "workday" and b.token:
            wd = WorkdayAdapter(
                b.company,
                b.token,
                queries=d.adzuna_queries,
                timeout_s=d.provider_timeout_s,
                max_retries=d.provider_max_retries,
                keep_terms=d.title_keep_terms,
                drop_terms=d.title_drop_terms,
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
            )
            if conn is not None:
                df.known_ids = known_jd_ids(conn, df.source)
            adapters.append(df)
            continue
        cls = _ATS.get(b.ats.lower())
        if cls and b.token:
            adapters.append(
                cls(
                    b.company,
                    b.token,
                    timeout_s=d.provider_timeout_s,
                    max_retries=d.provider_max_retries,
                )
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
    c = company.lower()
    return any(x.lower() in c for x in excludes if x)


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

    # Alert-email bookkeeping: the adapter parsed messages; we own persistence.
    for a in adapters:
        ledger = getattr(a, "ledger", None)
        if ledger:
            record_ledger(conn, ledger)

    failed_sources: set[str] = set()
    for source, snapshot, payload in results:
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
        for p in payload:
            result.fetched += 1
            if not p.external_id:
                continue
            if _is_excluded(p.company, excludes):
                result.skipped_excluded += 1
                continue
            seen_ids[source].add(p.external_id)
            # A multi-vendor adapter (alert emails) tags each posting with its
            # real origin ("linkedin"/"indeed"); store under that so the cockpit
            # shows where a job came from and cross-source clustering works.
            job_source = p.source or source
            cols = to_job_columns(p)
            cluster_id = _cluster_for(conn, p, job_source)
            if cluster_id:
                cols["cluster_id"] = cluster_id
            dbc.upsert_job(conn, source=job_source, external_id=p.external_id, **cols)
            result.inserted_or_updated += 1

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
        ttl = stale_after_days(adapter)
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


def _mark_one_stale(conn: sqlite3.Connection, job_id: int, result: DiscoveryResult) -> None:
    conn.execute("UPDATE jobs SET source_state='stale' WHERE id=?", (job_id,))
    result.stale_marked += 1

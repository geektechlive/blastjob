"""`blastjob-daily`: discover -> score -> push new roles to Discord.

The launchd job runs this once a day so the cockpit stops depending on the
user remembering to click Discover. It calls the same pipeline functions the web
app does (no HTTP hop) and then posts one digest message to the private Discord
channel: every surfaced/queued job that has not been announced yet, any source
errors, and follow-ups due today.

Invariants:
- `notified_at` is stamped only after Discord accepted the whole message, so a
  failed post is retried next run. Delivery is at-least-once by design: a crash
  in the window between Discord's 2xx and the stamp re-sends the same digest
  once. For a personal channel a rare duplicate beats a lost role; an outbox
  would be more machinery than the problem deserves.
- Links: the posting URL comes first because Discord is usually read on a
  phone, where the cockpit's 127.0.0.1 link is dead.
- Quiet when there is nothing to say (no daily "0 new" noise), except a weekly
  heartbeat so silence stays distinguishable from a dead job.
- A `PAUSE` file in the data dir skips the run entirely (kill switch, same
  convention used by a companion tool that shares the Discord channel).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from blastjob import config as cfg_mod
from blastjob import daily_employer_jd, daily_filing, daily_tools
from blastjob import logging as applog
from blastjob import secrets as secrets_mod
from blastjob.core import closure, closure_links
from blastjob.core.closure_links import LinkDeps, is_authoritative
from blastjob.daily_tools import recluster_cli
from blastjob.db import client as dbc
from blastjob.llm.cost import CostTracker
from blastjob.models.config import BlastJobConfig
from blastjob.web.apply_link import ApplyLink, apply_links_for
from blastjob.web.stage_lists import followups_due

if TYPE_CHECKING:
    from blastjob.core.closure import ClosureResult
    from blastjob.discovery.replies import ReplyMatch

log = logging.getLogger(__name__)

HEADER = "🎯 **blastjob**"
COCKPIT_BASE = "http://127.0.0.1:8765"
HEARTBEAT_WEEKDAY = 0  # Monday
MAX_DIGEST_JOBS = 25
MAX_CLOSED_NAMES = 5
PAUSE_FILE = "PAUSE"

# `res.errors` keys that mean the pipeline itself threw (as opposed to a single
# adapter failing, which is already isolated and reported per-source). --status
# leads with these because a run that recorded one is not a healthy baseline.
PIPELINE_FAILURE_KEYS = ("discovery", "matching")

Sender = Callable[[str], Awaitable[object]]


@dataclass
class DailyResult:
    paused: bool = False
    started_at: str = ""
    duration_s: float = 0.0
    fetched: int = 0
    per_source: dict[str, int] = field(default_factory=dict)
    scored: int = 0
    scoring_batches: int = 0
    scoring_s: float = 0.0
    new_jobs: int = 0
    followups: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    # Non-failures worth a human's attention (a snapshot the guard refused to
    # act on, a channel that has never yielded anything): shown in the digest,
    # not counted as errors.
    notes: dict[str, str] = field(default_factory=dict)
    sent: bool = False
    digest: str = ""
    backup_path: str = ""
    closed_postings: list[dict] = field(default_factory=list)
    replies: list[ReplyMatch] = field(default_factory=list)


def is_paused(cfg: BlastJobConfig) -> bool:
    return (cfg_mod.data_dir(cfg) / PAUSE_FILE).exists()


def pending_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Surfaced/queued, active, never announced. Best score first.

    A row whose cluster holds a kept or archived member is not a new role (the
    cockpit shows that cluster in the decided member's stage, or hides it), so it
    is never announced: that is how a repost of an applied role stays quiet."""
    return conn.execute(
        "SELECT id, company, title, url, alignment_score, review_state, "
        "geo_location_raw, geo_remote, comp_min, comp_max, comp_provenance, "
        "cluster_id, source, source_state, employer_link_source, employer_link_external_id, "
        "employer_link_url "
        "FROM jobs j WHERE source_state='active' AND notified_at='' "
        "AND review_state IN ('surfaced','queued') "
        "AND NOT (cluster_id IS NOT NULL AND EXISTS (SELECT 1 FROM jobs d "
        "WHERE d.cluster_id=j.cluster_id AND d.id<>j.id "
        "AND d.review_state IN ('kept','archived'))) "
        "ORDER BY alignment_score DESC, id ASC"
    ).fetchall()


def mark_notified(conn: sqlite3.Connection, job_ids: list[int]) -> None:
    if not job_ids:
        return
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.executemany("UPDATE jobs SET notified_at=? WHERE id=?", [(now, jid) for jid in job_ids])
    conn.commit()


_COMP_TAGS = {"posted": "posted", "inferred": "inferred (from the posting)", "estimated": "est."}


def _comp_badge(row: sqlite3.Row) -> str:
    lo, hi = int(row["comp_min"] or 0), int(row["comp_max"] or 0)
    if not (lo or hi):
        return "comp n/a"
    top = hi or lo
    # A lookup, not a ternary: the old `else` swept `inferred` and `unknown` into
    # the "posted" bucket and shipped a figure no employer published to Discord,
    # where it reads as fact on a phone.
    tag = _COMP_TAGS.get(row["comp_provenance"], "unverified")
    return f"${top // 1000}K {tag}"


def _where(row: sqlite3.Row) -> str:
    loc = (row["geo_location_raw"] or "").strip()
    if row["geo_remote"]:
        return f"Remote{' · ' + loc if loc else ''}"
    return loc or "location n/a"


def format_job_line(
    row: sqlite3.Row, base_url: str = COCKPIT_BASE, *, warm: bool = False, url: str = ""
) -> str:
    """`url` is the role's best apply link (web/apply_link); the row's own URL
    when there is none."""
    flag = " (review)" if row["review_state"] == "queued" else ""
    marker = " 🤝 you know someone here" if warm else ""
    score = row["alignment_score"] if row["alignment_score"] is not None else "?"
    return (
        f"• **{score}** {row['company']} — {row['title']}{flag}{marker}\n"
        f"   {_where(row)} · {_comp_badge(row)} · <{url or row['url']}>"
        f" · cockpit <{base_url}/jobs/{row['id']}>"
    )


def warm_job_ids(conn: sqlite3.Connection, jobs: list[sqlite3.Row]) -> frozenset[int]:
    """Which of `jobs` have a network warm path — used to jump the digest
    queue ahead of higher-scoring cold roles. Deterministic SQL only."""
    from blastjob.core import network as net
    from blastjob.core.company import company_key

    keys = {company_key(r["company"] or "") for r in jobs}
    counts = net.warm_counts(conn, keys)
    return frozenset(
        int(r["id"]) for r in jobs if counts.get(company_key(r["company"] or ""), 0) > 0
    )


def order_for_digest(
    jobs: list[sqlite3.Row], warm_ids: frozenset[int] = frozenset()
) -> list[sqlite3.Row]:
    """Digest order: warm-path jobs jump ahead of higher-scoring cold roles. A
    stable sort keeps each group's own score-descending order, so a warm role
    never loses its spot to a colder one. Shared by `build_digest` (what gets
    shown) and `run_daily` (what gets stamped) so the two never disagree about
    which jobs were actually rendered."""
    return sorted(jobs, key=lambda r: 0 if int(r["id"]) in warm_ids else 1)


def rendered_jobs(
    jobs: list[sqlite3.Row], warm_ids: frozenset[int] = frozenset()
) -> list[sqlite3.Row]:
    """The subset of `jobs` that `build_digest` actually renders (the digest
    window, `MAX_DIGEST_JOBS` wide). Anything outside this slice must not be
    stamped `notified_at` — see the overflow-stamping bug this guards against
    in tests/test_daily_notify.py."""
    return order_for_digest(jobs, warm_ids)[:MAX_DIGEST_JOBS]


def build_digest(
    jobs: list[sqlite3.Row],
    errors: dict[str, str],
    followups: list[dict],
    *,
    base_url: str = COCKPIT_BASE,
    heartbeat: str = "",
    footer: str = "",
    warm_ids: frozenset[int] = frozenset(),
    closed_postings: list[dict] | None = None,
    replies: list[ReplyMatch] | None = None,
    apply_links: Mapping[int, ApplyLink] | None = None,
    notes: Mapping[str, str] | None = None,
) -> str:
    """The Discord message. Empty string means "nothing to say". `apply_links`
    (`web.apply_link.apply_links_for`) gives each job its cluster's best link;
    a job missing from it links its own URL."""
    links = apply_links or {}
    closed = closed_postings or []
    replied = replies or []
    noted = notes or {}
    if not (jobs or errors or followups or heartbeat or closed or replied or noted):
        return ""
    n = len(jobs)
    title = f"{HEADER} — {n} new role{'s' if n != 1 else ''}" if n else HEADER
    lines = [title]
    for row in rendered_jobs(jobs, warm_ids):
        link = links.get(int(row["id"]))
        lines.append(
            format_job_line(
                row, base_url, warm=int(row["id"]) in warm_ids, url=link.url if link else ""
            )
        )
    if n > MAX_DIGEST_JOBS:
        # /all lists every active job (surfaced + queued + kept + archived),
        # so it contains the overflow regardless of the surfaced/queued mix —
        # unlike "/" (surfaced only) or "/queue" (queued only), either of which
        # can miss part of what got left out of the digest.
        lines.append(f"…and {n - MAX_DIGEST_JOBS} more in the cockpit: <{base_url}/all>")
    if closed:
        lines.append("")
        lines.append(format_closed_line(closed))
    if replied:
        lines.append("")
        lines.append("**Replies**")
        for r in replied:
            lines.append(format_reply_line(r, base_url))
    if followups:
        lines.append("")
        lines.append("**Follow-ups due**")
        for f in followups:
            lines.append(
                f"• {f['company']} — {f['title']}: {f['followup_reason']} "
                f"<{base_url}/jobs/{f['id']}>"
            )
    if errors:
        lines.append("")
        lines.append("**Source errors**")
        for src, err in sorted(errors.items()):
            lines.append(f"• {src}: {err[:160]}")
    if noted:
        lines.append("")
        lines.append("**Notes**")
        for src, note in sorted(noted.items()):
            lines.append(f"• {src}: {note[:200]}")
    if heartbeat:
        lines.append("")
        lines.append(heartbeat)
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)


def format_reply_line(r: ReplyMatch, base_url: str = COCKPIT_BASE) -> str:
    """One entry: who replied, about what, and a one-line snippet -- surface
    only, never a draft or a suggested response (the user answers this themselves)."""
    return (
        f"• {r.company} — {r.title}: reply from {r.sender} ({r.when})\n"
        f'   "{r.snippet}" · cockpit <{base_url}/jobs/{r.job_id}>'
    )


def format_closed_line(closed: list[dict]) -> str:
    """One line naming kept jobs whose posting closed this run."""
    n = len(closed)
    names = ", ".join(f"{c['company']} {c['title']}" for c in closed[:MAX_CLOSED_NAMES])
    more = ", …" if n > MAX_CLOSED_NAMES else ""
    return f"{n} kept posting{'s' if n != 1 else ''} closed: {names}{more}"


def _top_sources(per_source: dict[str, int], limit: int = 5) -> str:
    """Top N sources by fetch count, comma-joined, with a trailing '…' if more."""
    parts = sorted(per_source.items(), key=lambda kv: kv[1], reverse=True)
    src = ", ".join(f"{k} {v}" for k, v in parts[:limit])
    if len(parts) > limit:
        src += ", …"
    return src


def format_duration(s: float) -> str:
    """'12m 03s' under an hour, '1h 34m' at/above an hour."""
    total = int(round(max(s, 0.0)))
    if total < 3600:
        m, sec = divmod(total, 60)
        return f"{m}m {sec:02d}s"
    h, rem = divmod(total, 3600)
    m = rem // 60
    return f"{h}h {m:02d}m"


def format_run_footer(res: DailyResult) -> str:
    """One italic line summarizing a run: timing, fetch mix, scoring throughput."""
    src = _top_sources(res.per_source)
    fetched_str = f"fetched {res.fetched}" + (f" ({src})" if src else "")
    scoring_str = ""
    if res.scoring_batches:
        scoring_str = (
            f" · scored {res.scored} in {res.scoring_batches} batches"
            f" / {format_duration(res.scoring_s)}"
        )
    return f"_Run {format_duration(res.duration_s)} · {fetched_str}{scoring_str}_"


def heartbeat_line(conn: sqlite3.Connection, today: dt.date) -> str:
    if today.weekday() != HEARTBEAT_WEEKDAY:
        return ""
    row = conn.execute(
        "SELECT COUNT(*) AS active, "
        "SUM(CASE WHEN review_state='kept' THEN 1 ELSE 0 END) AS kept "
        "FROM jobs WHERE source_state='active'"
    ).fetchone()
    line = (
        f"_Weekly heartbeat: daily run alive. {row['active']} active postings, "
        f"{row['kept'] or 0} kept._"
    )
    totals = week_totals(conn, today)
    if totals["runs"]:
        avg_m = int(round(totals["avg_duration_s"] / 60))
        line += (
            f" Last 7 days: {totals['runs']} runs, fetched {totals['fetched']}, "
            f"scored {totals['scored']}, {totals['new_jobs']} new, avg {avg_m}m."
        )
    return line


def record_run(conn: sqlite3.Connection, res: DailyResult) -> int:
    """Insert a per-run summary row (sent=0) before the Discord send is attempted."""
    finished_at = dt.datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO daily_runs (started_at, finished_at, duration_s, fetched, per_source, "
        "scored, scoring_batches, scoring_s, new_jobs, followups, sent, errors) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (
            res.started_at,
            finished_at,
            res.duration_s,
            res.fetched,
            json.dumps(res.per_source),
            res.scored,
            res.scoring_batches,
            res.scoring_s,
            res.new_jobs,
            res.followups,
            json.dumps(res.errors),
        ),
    )
    conn.commit()
    if cur.lastrowid is None:
        raise RuntimeError("daily_runs insert returned no row id")
    return cur.lastrowid


def mark_run_sent(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute("UPDATE daily_runs SET sent=1 WHERE id=?", (run_id,))
    conn.commit()


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM daily_runs ORDER BY id DESC LIMIT 1").fetchone()


def week_totals(conn: sqlite3.Connection, today: dt.date) -> dict:
    cutoff = (today - dt.timedelta(days=7)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS runs, COALESCE(SUM(fetched),0) AS fetched, "
        "COALESCE(SUM(scored),0) AS scored, COALESCE(SUM(new_jobs),0) AS new_jobs, "
        "COALESCE(AVG(duration_s),0) AS avg_duration_s "
        "FROM daily_runs WHERE started_at >= ?",
        (cutoff,),
    ).fetchone()
    return dict(row)


async def run_daily(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    *,
    send: Sender,
    score: bool = True,
    dry_run: bool = False,
    today: dt.date | None = None,
) -> DailyResult:
    from blastjob.discovery.pipeline import run_discovery
    from blastjob.matching.pipeline import run_matching

    cfg_mod.apply_runtime_settings(cfg)
    res = DailyResult()
    if is_paused(cfg):
        log.info("PAUSE file present; skipping daily run")
        res.paused = True
        return res

    run_start = time.monotonic()
    res.started_at = dt.datetime.now().isoformat(timespec="seconds")

    try:
        backup_path = dbc.backup_db(conn, cfg_mod.data_dir(cfg) / "backups")
        res.backup_path = str(backup_path)
        log.info("db backup: %s", backup_path)
    except Exception as e:
        log.exception("db backup failed")
        res.errors["backup"] = f"{type(e).__name__}: {e}"

    try:
        dres = await run_discovery(cfg, conn)
        res.fetched = dres.fetched
        res.per_source = dict(dres.per_source)
        res.errors.update(dres.errors)
        res.notes.update(dres.notes)
        log.info(
            "discovery: fetched %d saved %d stale %d errors %d",
            dres.fetched,
            dres.inserted_or_updated,
            dres.stale_marked,
            len(dres.errors),
        )
    except Exception as e:
        # A pipeline-level throw, not a single adapter's already-isolated
        # failure. Roll back anything mid-flight that hadn't self-committed
        # (per-job upserts already have, same as any other crash mid-loop),
        # then keep going: the run must still be recorded and reported —
        # see the module docstring and the CLAUDE.md defect writeup this
        # guard was written against.
        conn.rollback()
        log.exception("discovery pipeline failed")
        res.errors["discovery"] = applog.redact(f"{type(e).__name__}: {e}")

    # Free sibling copies land BEFORE matching, so a copied row is scored once, on
    # the full text. Local only: no request, no LLM call.
    free_copy = await daily_employer_jd.free_copy_step(cfg, conn, res, dry_run=dry_run)

    if score:
        # Only never-scored jobs: re-scoring hundreds of resolved postings every
        # morning burned 95 minutes of CLI time on 2026-09-05 for no new signal.
        scoring_start = time.monotonic()

        def _on_progress(_i: int, n: int) -> None:
            res.scoring_batches = n

        try:
            mres = await run_matching(
                cfg, conn, CostTracker(), on_progress=_on_progress, only_unscored=True
            )
            res.scoring_s = time.monotonic() - scoring_start
            res.scored = mres.scored
            if mres.scoring_failures:
                res.errors["scoring"] = (
                    f"{mres.scoring_failures} batch(es) failed; retried next run"
                )
            for note in mres.notes:
                res.errors["candidate_profile"] = note
            log.info(
                "matching: scored %d surfaced %d queued %d",
                mres.scored,
                mres.surfaced,
                mres.queued,
            )
        except Exception as e:
            conn.rollback()
            res.scoring_s = time.monotonic() - scoring_start
            log.exception("matching pipeline failed")
            res.errors["matching"] = applog.redact(f"{type(e).__name__}: {e}")

    # One deps object for closure and the employer-description step, so a board
    # fetched by one is cached for the other.
    shared_deps = closure_links.default_deps()
    closure_res = await _closure_pass(cfg, conn, res, shadow=dry_run, deps=shared_deps)
    # Closure moves kept jobs to posting_closed; file their packets (#20).
    daily_filing.filing_pass(cfg, conn, dry_run=dry_run)
    await daily_employer_jd.lookup_step(
        cfg, conn, res, closure_res, shared_deps, free=free_copy, dry_run=dry_run, score=score
    )
    await _replies_pass(cfg, conn, res, dry_run=dry_run)

    jobs = pending_jobs(conn)
    fups = followups_due(conn)
    warm_ids = warm_job_ids(conn, jobs)
    links = apply_links_for(conn, jobs)
    res.new_jobs = len(jobs)
    res.followups = len(fups)
    hb = heartbeat_line(conn, today or dt.date.today())
    res.digest = build_digest(
        jobs,
        res.errors,
        fups,
        heartbeat=hb,
        warm_ids=warm_ids,
        closed_postings=res.closed_postings,
        replies=res.replies,
        notes=res.notes,
        apply_links=links,
    )
    res.duration_s = time.monotonic() - run_start

    if dry_run:
        return res

    run_id = record_run(conn, res)

    if not res.digest:
        log.info("nothing to announce")
        return res

    footer = format_run_footer(res)
    res.digest = build_digest(
        jobs,
        res.errors,
        fups,
        heartbeat=hb,
        footer=footer,
        warm_ids=warm_ids,
        closed_postings=res.closed_postings,
        replies=res.replies,
        notes=res.notes,
        apply_links=links,
    )
    await send(res.digest)  # raises on failure -> nothing stamped
    # Stamp only what was actually rendered in the sent digest — anything
    # past the MAX_DIGEST_JOBS cutoff stays notified_at='' so it leads the
    # next run's digest instead of being silently buried forever.
    mark_notified(conn, [int(r["id"]) for r in rendered_jobs(jobs, warm_ids)])
    res.sent = True
    mark_run_sent(conn, run_id)
    return res


async def _closure_pass(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    res: DailyResult,
    *,
    shadow: bool = False,
    deps: LinkDeps | None = None,
) -> ClosureResult | None:
    """Posting closure, after discovery + matching so it sees today's
    source_state. Never sinks the run: a failure is reported like a source error.

    Secrets are loaded here too, so a dry run probes Workday/Dayforce exactly
    like the real run. On failure the pending step is rolled back: the pass
    commits after every self-consistent step, and whatever was mid-flight must
    not ride along on record_run's or mark_notified's later commit."""
    secrets_mod.load_secrets_env()
    try:
        # A dry run must not move jobs even with closure_auto on (it still
        # records verdicts and links, like --closure-report).
        cres = await closure.run_closure(cfg, conn, shadow=shadow, deps=deps)
    except Exception as e:
        conn.rollback()
        log.exception("closure pass failed")
        res.errors["closure"] = f"{type(e).__name__}: {e}"
        return None
    res.closed_postings = list(cres.closed_kept)
    log.info(
        "closure: checked %d linked %d moved %d filled %d newly closed kept %d (auto=%s)",
        cres.checked,
        cres.linked,
        cres.moved,
        cres.filled,
        len(cres.closed_kept),
        cfg.discovery.closure_auto,
    )
    return cres


async def _replies_pass(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    res: DailyResult,
    *,
    dry_run: bool = False,
) -> None:
    """Scan for human replies to jobs already applied to, after discovery so a
    reply about a job that closed today still gets checked against the
    pre-closure application state. Runs whenever the personal mailbox has
    credentials -- deliberately NOT gated on `mail_alerts_enabled`, which is a
    flag about the alert account's job feed; turning that feed off must not
    silently stop watching for a recruiter's answer. With no credentials this
    is a silent no-op, not a reported error. A genuine failure (e.g. IMAP
    login) is recorded like any other source error and never sinks the run --
    see `_closure_pass`, same pattern."""
    from blastjob.discovery import replies

    secrets_mod.load_secrets_env()
    if not replies.replies_configured(cfg):
        return
    try:
        # A dry run prints the digest and stamps nothing -- including the
        # reply ledger. Stamping here would mark a reply seen and the real
        # 07:30 run would never surface it.
        res.replies = await replies.scan_replies(cfg, conn, record=not dry_run)
    except Exception as e:
        conn.rollback()
        log.exception("reply scan failed")
        res.errors["replies"] = applog.redact(f"{type(e).__name__}: {e}")
        return
    if res.replies:
        log.info("replies: %d matched", len(res.replies))


async def closure_report(cfg: BlastJobConfig, conn: sqlite3.Connection) -> list[dict]:
    """Shadow closure pass for review: records verdicts and links but forces no
    state transition whatever the config says, sends nothing, and writes no
    daily_runs row. Returns the kept jobs with their verdicts, plus
    `would_move_to`: what each row becomes the first run after closure_auto is
    switched on (shadow-stamped closures fire in bulk on that day)."""
    secrets_mod.load_secrets_env()
    cres = await closure.run_closure(cfg, conn, shadow=True)
    would_revert = set(cres.would_revert_jd)
    rows = conn.execute(
        "SELECT id, company, title, source, review_state, employer_link_source, "
        "employer_link_external_id, posting_check, posting_check_manual, posting_closed_at, "
        "application_state FROM jobs WHERE review_state='kept' ORDER BY id"
    ).fetchall()
    now = dt.datetime.now()
    return [
        {
            **dict(r),
            "would_move_to": closure.would_move_to(r, now=now),
            "would_revert_jd": r["id"] in would_revert,
        }
        for r in rows
    ]


def format_closure_report(rows: list[dict]) -> str:
    lines = [
        "id | company | title | source | link | posting_check | application_state | would_move_to"
    ]
    for r in rows:
        if r["employer_link_source"]:
            link = f"{r['employer_link_source']}/{r['employer_link_external_id']}"
        else:
            link = "own source" if is_authoritative(r["source"]) else "none"
        check = (r["posting_check"] or "-") + (" (manual)" if r["posting_check_manual"] else "")
        lines.append(
            f"{r['id']} | {r['company']} | {r['title']} | {r['source']} | {link} | "
            f"{check} | {r['application_state'] or '-'} | {r['would_move_to']}"
            + (" (employer description would revert)" if r.get("would_revert_jd") else "")
        )
    return "\n".join(lines)


async def sweep_search_firms(
    cfg: BlastJobConfig, conn: sqlite3.Connection, *, score: bool = True
) -> dict:
    """Ad-hoc run of just the retained-search-firm adapter, outside the weekly
    schedule — for on-demand tuning/verification. Sends nothing to Discord and
    records no `daily_runs` row; it is a diagnostic tool, not part of the
    scheduled pipeline."""
    from blastjob.discovery.pipeline import build_search_firm_adapter, run_discovery
    from blastjob.matching.pipeline import run_matching

    sf = build_search_firm_adapter(cfg, conn)
    dres = await run_discovery(cfg, conn, adapters=[sf])
    if score:
        await run_matching(cfg, conn, CostTracker(), only_unscored=True)
    rows = conn.execute(
        "SELECT id, source, company, title, url, alignment_score, review_state "
        "FROM jobs WHERE source LIKE 'searchfirm:%' ORDER BY id"
    ).fetchall()
    return {
        "per_source": dict(dres.per_source),
        "errors": dict(dres.errors),
        "fetched": dres.fetched,
        "jobs": [dict(r) for r in rows],
    }


def _discord_sender(cfg: BlastJobConfig) -> Sender:
    from blastjob.notify.discord import credentials, send_message
    from blastjob.secrets import load_secrets_env

    load_secrets_env()
    token, channel = credentials(
        token_env=cfg.notify.discord_token_env, channel_env=cfg.notify.discord_channel_env
    )

    async def send(text: str) -> object:
        return await send_message(text, token=token, channel_id=channel)

    return send


async def _print_digest(text: str) -> None:
    print(text)


async def _discard(_text: str) -> None:
    return None


def sender_for(cfg: BlastJobConfig, *, dry_run: bool) -> Sender:
    """Where the digest goes (`notify.mode`). Only `discord` needs credentials,
    so a stdout/off install never fails a real run over missing Discord keys."""
    if dry_run or cfg.notify.mode == "off":
        return _discard
    if cfg.notify.mode == "discord":
        return _discord_sender(cfg)
    return _print_digest


def _run_started_date(row: sqlite3.Row) -> dt.date | None:
    """`started_at` is an ISO timestamp on every real run, but `DailyResult`
    defaults it to '' — parse defensively so a malformed/missing value makes
    `--status` say so instead of raising (a crashing `--status` is itself an
    invisible failure)."""
    try:
        return dt.datetime.fromisoformat(row["started_at"]).date()
    except (TypeError, ValueError):
        return None


def _format_status(row: sqlite3.Row, *, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    per_source = json.loads(row["per_source"] or "{}")
    errors = json.loads(row["errors"] or "{}")
    lines: list[str] = []

    failed = {k: errors[k] for k in PIPELINE_FAILURE_KEYS if errors.get(k)}
    if failed:
        detail = "; ".join(f"{k}: {v}" for k, v in failed.items())
        lines.append(f"LAST RUN FAILED — {detail}")

    run_date = _run_started_date(row)
    if run_date is None:
        lines.append("last run: unknown date (malformed started_at)")
    elif run_date != today:
        age = (today - run_date).days
        lines.append(f"last run: {run_date.isoformat()}, {age} day{'s' if age != 1 else ''} ago")

    lines += [
        f"Last run: {row['started_at']} -> {row['finished_at']} "
        f"({format_duration(row['duration_s'])})",
        f"fetched {row['fetched']} ({_top_sources(per_source)})",
        f"scored {row['scored']} in {row['scoring_batches']} batches "
        f"({format_duration(row['scoring_s'])})",
        f"new {row['new_jobs']}, follow-ups {row['followups']}, sent {bool(row['sent'])}",
        # backup_path isn't a daily_runs column (no schema migration for it) so
        # the historical --status view can't show a past run's backup path —
        # only the in-process summary printed at the end of a live run can.
        "backup: not tracked in run history (see the summary line of a live run)",
    ]
    if errors:
        lines.append(f"errors: {errors}")
    return "\n".join(lines)


def _format_paused_status(cfg: BlastJobConfig) -> str:
    """`--status` while a PAUSE file sits in the data dir. No daily_runs row
    is written for a paused run (see `run_daily`) — the file's own mtime is
    the timestamp, so a stale successful run from days ago is never shown as
    if it were current."""
    pause_path = cfg_mod.data_dir(cfg) / PAUSE_FILE
    try:
        since = dt.datetime.fromtimestamp(pause_path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return "paused (PAUSE file present)"
    return f"paused since {since} (PAUSE file present)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="blastjob-daily", description=__doc__.split("\n")[0])
    ap.add_argument(
        "--dry-run", action="store_true", help="print the digest; send nothing, stamp nothing"
    )
    ap.add_argument("--no-score", action="store_true", help="discover only (skip LLM scoring)")
    ap.add_argument("--status", action="store_true", help="print the last recorded run and exit")
    ap.add_argument(
        "--sweep-search-firms",
        action="store_true",
        help="run only the retained-search-firm adapter on demand; sends nothing, records nothing",
    )
    ap.add_argument(
        "--closure-report",
        action="store_true",
        help="shadow posting-closure pass over kept jobs: records verdicts, moves nothing, "
        "sends nothing",
    )
    ap.add_argument(
        "--recluster-report",
        action="store_true",
        help="print the same-role cluster merges the backfill would make; changes nothing",
    )
    ap.add_argument(
        "--recluster",
        action="store_true",
        help="apply the same-role cluster merges (merge-only; honours 'not the same role')",
    )
    daily_tools.add_arguments(ap)
    args = ap.parse_args(argv)

    applog.setup()
    cfg = cfg_mod.load_config()
    conn = dbc.init_db(cfg)
    try:
        if args.status:
            if is_paused(cfg):
                print(_format_paused_status(cfg))
                return 0
            row = last_run(conn)
            print(_format_status(row) if row is not None else "no runs recorded")
            return 0
        if args.recluster_report or args.recluster:
            print(recluster_cli(cfg, conn, apply=args.recluster))
            return 0
        if (tool_report := daily_tools.dispatch(args, cfg, conn)) is not None:
            print(tool_report)
            return 0
        if args.closure_report:
            print(format_closure_report(asyncio.run(closure_report(cfg, conn))))
            return 0
        if args.sweep_search_firms:
            from blastjob.secrets import load_secrets_env

            load_secrets_env()
            result = asyncio.run(sweep_search_firms(cfg, conn, score=not args.no_score))
            for src, n in sorted(result["per_source"].items()):
                print(f"{src}: {n}")
            for src, err in sorted(result["errors"].items()):
                print(f"error {src}: {err}")
            for j in result["jobs"]:
                score_val = j["alignment_score"] if j["alignment_score"] is not None else "?"
                print(
                    f"{score_val} | {j['review_state']} | {j['source']} | {j['title']} | {j['url']}"
                )
            return 0
        send = sender_for(cfg, dry_run=args.dry_run)
        res = asyncio.run(
            run_daily(cfg, conn, send=send, score=not args.no_score, dry_run=args.dry_run)
        )
    finally:
        conn.close()
    if res.paused:
        print("paused (PAUSE file present)")
        return 0
    summary = (
        f"fetched {res.fetched} ({_top_sources(res.per_source)}), "
        f"scored {res.scored} in {res.scoring_batches} batches "
        f"({format_duration(res.scoring_s)}), new {res.new_jobs}, "
        f"follow-ups {res.followups}, errors {len(res.errors)}, sent {res.sent}, "
        f"duration {format_duration(res.duration_s)}, "
        f"backup {res.backup_path or 'failed'}"
    )
    log.info(summary)
    print(summary)
    if args.dry_run and res.digest:
        print("---\n" + res.digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

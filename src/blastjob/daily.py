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
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from blastjob import config as cfg_mod
from blastjob import daily_dream, daily_targets, daily_tools
from blastjob import logging as applog
from blastjob import secrets as secrets_mod
from blastjob.core import closure, run_lock
from blastjob.core import target_store as target_store
from blastjob.core.closure_links import is_authoritative
from blastjob.core.pause import PAUSE_FILE
from blastjob.daily_budget import STOPPED_BUDGET, budget_note, yesterday_calls_line
from blastjob.daily_digest import (  # noqa: F401 -- re-exported: daily.build_digest etc.
    COCKPIT_BASE,
    HEADER,
    MAX_CLOSED_NAMES,
    MAX_DIGEST_JOBS,
    MAX_TARGET_NEWS,
    _comp_badge,
    _top_sources,
    _where,
    build_digest,
    format_closed_line,
    format_duration,
    format_job_line,
    format_reply_line,
    format_run_footer,
    order_for_digest,
    rendered_jobs,
    rendered_target_news,
    warm_job_ids,
)
from blastjob.daily_steps import _closure_pass, _replies_pass, run_steps  # noqa: F401
from blastjob.daily_tools import recluster_cli
from blastjob.db import client as dbc
from blastjob.llm import gateway
from blastjob.llm.cost import CallCost, CostTracker, aggregate_calls, describe_cost
from blastjob.models.config import BlastJobConfig
from blastjob.web.apply_link import apply_links_for
from blastjob.web.stage_lists import followups_due

if TYPE_CHECKING:
    from blastjob.discovery.replies import ReplyMatch

log = logging.getLogger(__name__)

HEARTBEAT_WEEKDAY = 0  # Monday

# `res.errors` keys that mean the pipeline itself threw (as opposed to a single
# adapter failing, which is already isolated and reported per-source). --status
# leads with these because a run that recorded one is not a healthy baseline.
PIPELINE_FAILURE_KEYS = ("discovery", "matching")

Sender = Callable[[str], Awaitable[object]]

# `daily_runs.stopped_reason` -> the line `--status` leads with. A run that ended early is never
# shown as a complete one.
STOPPED_STATUS = {
    "deadline": "STOPPED EARLY: hit the daily time limit (deadline); the rest waits for tomorrow",
    "lock_held": "SKIPPED: another run held the run lock, so this run did no work",
    "budget": "STOPPED EARLY: the model call budget was reached; the rest waits for the next run",
}


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
    # What this run's scoring calls cost (unknown if any usage was unknown). Shown in
    # the footer and the run log; not stored in daily_runs.
    scoring_cost: CallCost = field(default_factory=CallCost)
    # The employer-description re-score pass (daily_employer_jd.lookup_step), same rules.
    rescore_cost: CallCost = field(default_factory=CallCost)
    # The target-company news step (daily_targets): scans that failed this run, and its model cost.
    target_failed: int = 0
    target_cost: CallCost = field(default_factory=CallCost)
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
    # Why the run ended early ('' = it ran to the end), and the digest line saying so.
    stopped_reason: str = ""
    stopped_note: str = ""
    # Steps (or parts of one) the deadline kept from running. Named in the digest and status.
    skipped_steps: list[str] = field(default_factory=list)

    @property
    def total_cost(self) -> CallCost:
        """Everything the run's model passes cost (unknown if any part was)."""
        return aggregate_calls([self.scoring_cost, self.rescore_cost, self.target_cost])

    @property
    def has_cost(self) -> bool:
        """Whether a model call ran, so the footer may show a cost. The rescore
        pass keeps no call count: a known zero means it made none."""
        return (
            bool(self.scoring_batches)
            or self.rescore_cost.cost_usd > 0
            or (not self.rescore_cost.known)
            or self.target_cost.cost_usd > 0
            or (not self.target_cost.known)
        )


def cockpit_base(cfg: BlastJobConfig) -> str:
    """Base of every cockpit link in the digest: `web.public_url` when set, else
    loopback on the configured port (docs/using/digest.md)."""
    return cfg.web.public_url or f"http://127.0.0.1:{cfg.web.port}"


def is_paused(cfg: BlastJobConfig) -> bool:
    return (cfg_mod.data_dir(cfg) / PAUSE_FILE).exists()


def pending_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Surfaced/queued, active, never announced. Best score first.

    A row whose cluster holds a kept or archived member is not a new role (the
    cockpit shows that cluster in the decided member's stage, or hides it), so it
    is never announced: that is how a repost of an applied role stays quiet."""
    # A far dream role (`dream_far`, #90) waits for the weekly Dream list section instead.
    return conn.execute(
        f"SELECT {daily_dream.DIGEST_COLUMNS} "
        "FROM jobs j WHERE source_state='active' AND notified_at='' "
        "AND COALESCE(dream_far,0)=0 "
        "AND review_state IN ('surfaced','queued') "
        f"AND {daily_dream.CLUSTER_NOT_DECIDED} "
        "ORDER BY alignment_score DESC, id ASC"
    ).fetchall()


def mark_notified(conn: sqlite3.Connection, job_ids: list[int]) -> None:
    if not job_ids:
        return
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.executemany("UPDATE jobs SET notified_at=? WHERE id=?", [(now, jid) for jid in job_ids])
    conn.commit()


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
        "scored, scoring_batches, scoring_s, new_jobs, followups, sent, errors, stopped_reason, "
        "stopped_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
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
            res.stopped_reason,
            res.stopped_note,
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
        "FROM daily_runs WHERE started_at >= ? AND COALESCE(stopped_reason,'') != 'lock_held'",
        (cutoff,),
    ).fetchone()
    return dict(row)


STOPPED_DEADLINE = "deadline"  # the budget's is daily_budget.STOPPED_BUDGET
STOPPED_LOCK_HELD = "lock_held"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _deadline_note(
    conn: sqlite3.Connection, minutes: int, skipped: list[str], *, score: bool
) -> str:
    """The digest line for a run the deadline cut short: what is left and which steps did not
    run (never shown as done)."""
    from blastjob.matching.pipeline import count_unscored

    left = count_unscored(conn) if score else 0
    base = f"Stopped at the {minutes}-minute limit"
    if left:
        # An upper bound: rows the geo check or title prefilter will turn away are unscored too.
        note = f"{base}; up to {_plural(left, 'job')} left for tomorrow"
    else:
        note = f"{base}; the remaining steps run tomorrow"
    if skipped:
        note += f". Not run (time limit): {', '.join(dict.fromkeys(skipped))}"
    return note


def _record_skipped(
    conn: sqlite3.Connection,
    res: DailyResult,
    held: run_lock.RunLockHeld,
    run_start: float,
    *,
    dry_run: bool,
) -> DailyResult:
    """Another run holds the run lock: do no work and send nothing (a skip must not spam; the
    next run's digest carries everything it would have). The skip is recorded so `--status`
    and the funnel show it."""
    log.warning("daily run skipped: %s", held)
    res.stopped_reason = STOPPED_LOCK_HELD
    res.errors["lock"] = str(held)
    res.duration_s = time.monotonic() - run_start
    if not dry_run:
        record_run(conn, res)
    return res


async def run_daily(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    *,
    send: Sender,
    score: bool = True,
    dry_run: bool = False,
    today: dt.date | None = None,
) -> DailyResult:
    cfg_mod.apply_runtime_settings(cfg)
    res = DailyResult()
    if is_paused(cfg):
        log.info("PAUSE file present; skipping daily run")
        res.paused = True
        return res

    run_start = time.monotonic()
    res.started_at = dt.datetime.now().isoformat(timespec="seconds")

    # One run at a time across processes (core/run_lock.py), held for the whole run so the
    # digest and its stamps are never interleaved with a cockpit pass.
    with ExitStack() as stack:
        try:
            stack.enter_context(run_lock.hold(cfg_mod.data_dir(cfg), "daily run"))
        except run_lock.RunLockHeld as held:
            return _record_skipped(conn, res, held, run_start, dry_run=dry_run)
        minutes = cfg.limits.daily_deadline_minutes
        with gateway.origin_scope("scheduled"), gateway.deadline_scope(minutes * 60) as state:
            cut = await run_steps(cfg, conn, res, state, score=score, dry_run=dry_run, today=today)
        if cut or state.hit:
            if state.reason == STOPPED_BUDGET:
                res.stopped_reason = STOPPED_BUDGET
                res.stopped_note = budget_note(
                    conn, cfg.limits.scheduled_calls_per_day, res.skipped_steps, score=score
                )
            else:
                res.stopped_reason = STOPPED_DEADLINE
                res.stopped_note = _deadline_note(conn, minutes, res.skipped_steps, score=score)
            log.warning("daily run %s (first refused call at %s)", res.stopped_note, state.hit_at)
        return await _announce(cfg, conn, res, send, run_start, dry_run=dry_run, today=today)


def _skipped_runs_line(conn: sqlite3.Connection, run_id: int) -> str:
    """ "N earlier runs were skipped" for runs skipped on the run lock since the last digest that
    went out. Part of the footer only, so it rides on a digest that is sent anyway and never
    makes a quiet day send."""
    row = conn.execute(
        "SELECT COUNT(*) FROM daily_runs WHERE stopped_reason='lock_held' AND id < ? "
        "AND id > COALESCE((SELECT MAX(id) FROM daily_runs WHERE sent=1), 0)",
        (run_id,),
    ).fetchone()
    n = int(row[0])
    if not n:
        return ""
    return f"\n_{_plural(n, 'earlier run')} skipped: another run held the run lock_"


async def _announce(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    res: DailyResult,
    send: Sender,
    run_start: float,
    *,
    dry_run: bool,
    today: dt.date | None,
) -> DailyResult:
    """Build the digest, record the run, and send it (quiet when there is nothing to say)."""
    jobs = pending_jobs(conn)
    dream_jobs = daily_dream.weekly_jobs(cfg, conn, today or dt.date.today())
    target_news = daily_targets.pending_news(conn)
    fups = followups_due(conn)
    warm_ids = warm_job_ids(conn, jobs)
    links = apply_links_for(conn, [*jobs, *dream_jobs])
    res.new_jobs = len(jobs)
    res.followups = len(fups)
    hb = heartbeat_line(conn, today or dt.date.today())

    def digest(footer: str = "") -> str:
        return build_digest(
            jobs,
            res.errors,
            fups,
            base_url=cockpit_base(cfg),
            heartbeat=hb,
            footer=footer,
            warm_ids=warm_ids,
            closed_postings=res.closed_postings,
            replies=res.replies,
            notes=res.notes,
            apply_links=links,
            target_news=target_news,
            target_failed=res.target_failed,
            dream_jobs=dream_jobs,
            stopped=res.stopped_note,
        )

    res.digest = digest()
    res.duration_s = time.monotonic() - run_start

    if dry_run:
        return res

    run_id = record_run(conn, res)

    if not res.digest:
        log.info("nothing to announce")
        return res

    # The calls line rides on a digest that is sent anyway (the quiet-day return is above).
    footer = format_run_footer(res) + _skipped_runs_line(conn, run_id)
    res.digest = digest(footer + yesterday_calls_line(conn, today))
    await send(res.digest)  # raises on failure -> nothing stamped
    # Stamp only what was actually rendered in the sent digest — anything
    # past the MAX_DIGEST_JOBS cutoff stays notified_at='' so it leads the
    # next run's digest instead of being silently buried forever.
    mark_notified(conn, [int(r["id"]) for r in rendered_jobs(jobs, warm_ids)])
    # The Dream list section follows the same rule: only the roles it rendered.
    daily_dream.mark_dream_notified(
        conn, [int(r["id"]) for r in daily_dream.rendered_dream_jobs(dream_jobs)]
    )
    # Same rule for target news: only the events the sent digest rendered.
    target_store.mark_notified(conn, [n.id for n in rendered_target_news(target_news)])
    res.sent = True
    mark_run_sent(conn, run_id)
    return res


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
    # One lock across both passes (each also takes it, reentrantly).
    with run_lock.hold(cfg_mod.data_dir(cfg), "search-firm sweep"):
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

    reason = row["stopped_reason"] if "stopped_reason" in row.keys() else ""  # noqa: SIM118
    stopped = STOPPED_STATUS.get(reason or "")
    if stopped:
        lines.append(stopped)
        note = row["stopped_note"] if "stopped_note" in row.keys() else ""  # noqa: SIM118
        if note:
            lines.append(note)

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
    # Every hand-run daily command counts against the scheduled call budget (ADR 0060).
    with gateway.origin_scope("scheduled"), gateway.refusal_scope():
        return _dispatch(args)


def _dispatch(args: argparse.Namespace) -> int:
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
    except run_lock.RunLockHeld as held:
        # A hand-run command (sweep, workday fix, rescore) met a running pass: say so, no queue.
        print(f"{held}. Try again when it finishes.", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if res.paused:
        print("paused (PAUSE file present)")
        return 0
    if res.stopped_reason == STOPPED_LOCK_HELD:
        skipped = f"daily run skipped: {res.errors.get('lock', 'another run holds the run lock')}"
        log.info(skipped)
        print(skipped)
        return 0
    summary = (
        f"fetched {res.fetched} ({_top_sources(res.per_source)}), "
        f"scored {res.scored} in {res.scoring_batches} batches "
        f"({format_duration(res.scoring_s)}, cost {describe_cost(res.total_cost)}), "
        f"new {res.new_jobs}, "
        f"follow-ups {res.followups}, errors {len(res.errors)}, sent {res.sent}, "
        f"duration {format_duration(res.duration_s)}, "
        f"backup {res.backup_path or 'failed'}"
    )
    if res.stopped_reason:
        summary += f", {res.stopped_note or STOPPED_STATUS.get(res.stopped_reason, '')}"
    log.info(summary)
    print(summary)
    if args.dry_run and res.digest:
        print("---\n" + res.digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

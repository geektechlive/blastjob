"""The steps of one daily run, from the backup to the target scan (see docs/how-it-works/daily.md).

Split out of `daily.py`, which owns the run's frame: the lock, the deadline scope, the digest.

The deadline gates every step that discovers, scores or looks things up over the network. Two
steps cost no model call and are cheap and local, so they still run after the cutoff: the reply
scan (recruiter replies are the owner's top channel) and packet filing. Whatever does not run
is named in `res.skipped_steps`, so the digest and `--status` say "not run", never "done".
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from blastjob import config as cfg_mod
from blastjob import daily_employer_jd, daily_filing, daily_label, daily_targets
from blastjob import logging as applog
from blastjob import secrets as secrets_mod
from blastjob.core import closure, closure_links
from blastjob.core.closure_links import LinkDeps
from blastjob.db import client as dbc
from blastjob.llm import gateway
from blastjob.llm.cost import CostTracker, describe_cost
from blastjob.models.config import BlastJobConfig

if TYPE_CHECKING:
    from blastjob.core.closure import ClosureResult
    from blastjob.daily import DailyResult

log = logging.getLogger(__name__)


def out_of_time(state: gateway.DeadlineState) -> bool:
    """Between steps: stop starting work once a call was refused (deadline or call budget) or too
    little time is left."""
    return state.hit or gateway.deadline_reached()


def _may_run(res: DailyResult, state: gateway.DeadlineState, name: str) -> bool:
    """Whether a gated step may start. A step that may not is named as not run."""
    if out_of_time(state):
        res.skipped_steps.append(name)
        return False
    return True


async def run_steps(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    res: DailyResult,
    state: gateway.DeadlineState,
    *,
    score: bool,
    dry_run: bool,
    today: dt.date | None,
) -> bool:
    """Every step from the backup to the target scan. True when the deadline cut the run
    short: a step was not started, or a model call inside one was refused. A call already in
    flight finishes (or hits its own bound); nothing new starts after the cutoff."""
    from blastjob.discovery.pipeline import run_discovery
    from blastjob.matching.pipeline import run_matching

    try:
        backup_path = dbc.backup_db(conn, cfg_mod.data_dir(cfg) / "backups")
        res.backup_path = str(backup_path)
        log.info("db backup: %s", backup_path)
    except Exception as e:
        log.exception("db backup failed")
        res.errors["backup"] = f"{type(e).__name__}: {e}"

    if _may_run(res, state, "discovery"):
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
            # see the module docstring and docs/how-it-works/daily.md for the defect
            # this guard was written against.
            conn.rollback()
            log.exception("discovery pipeline failed")
            res.errors["discovery"] = applog.redact(f"{type(e).__name__}: {e}")

    free_copy = None
    if _may_run(res, state, "employer-description copies"):
        # Free sibling copies land BEFORE matching, so a copied row is scored once, on
        # the full text. Local only: no request, no LLM call.
        free_copy = await daily_employer_jd.free_copy_step(cfg, conn, res, dry_run=dry_run)

    if score and _may_run(res, state, "scoring"):
        with gateway.caller_scope("daily scoring"):
            await _matching_step(cfg, conn, res, state, run_matching)

    shared_deps = None
    closure_res = None
    if _may_run(res, state, "closure checks"):
        # One deps object for closure and the employer-description step, so a board
        # fetched by one is cached for the other.
        shared_deps = closure_links.default_deps()
        closure_res = await _closure_pass(cfg, conn, res, shadow=dry_run, deps=shared_deps)
    # Closure moves kept jobs to posting_closed; file their packets (#20). Local, no model call:
    # it runs after the cutoff too.
    daily_filing.filing_pass(cfg, conn, dry_run=dry_run)
    if shared_deps is not None and _may_run(res, state, "employer-description lookup"):
        with gateway.caller_scope("daily employer description"):
            await daily_employer_jd.lookup_step(
                cfg,
                conn,
                res,
                closure_res,
                shared_deps,
                free=free_copy,
                dry_run=dry_run,
                score=score,
            )
    elif shared_deps is None:
        res.skipped_steps.append("employer-description lookup")
    # The reply scan makes no model call and recruiters are the owner's top channel: it runs
    # after the cutoff too.
    await _replies_pass(cfg, conn, res, dry_run=dry_run)
    # Same for the recruiter label (no model call): mail the owner filed by hand to triage.
    await daily_label.label_pass(cfg, conn, res, dry_run=dry_run)
    if _may_run(res, state, "target scans"):
        with gateway.caller_scope("daily target scans"):
            await daily_targets.targets_step(cfg, conn, res, dry_run=dry_run, today=today)
    return state.hit or bool(res.skipped_steps)


async def _matching_step(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    res: DailyResult,
    state: gateway.DeadlineState,
    run_matching: Callable[..., Awaitable[Any]],
) -> None:
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
        res.scoring_cost = mres.cost
        # A batch the deadline or the call budget refused is counted as failed by the scorer; the
        # run says it stopped at the limit instead of blaming the batch.
        if mres.scoring_failures and not state.hit:
            res.errors["scoring"] = f"{mres.scoring_failures} batch(es) failed; retried next run"
        for note in mres.notes:
            res.errors["candidate_profile"] = note
        log.info(
            "matching: scored %d surfaced %d queued %d cost %s",
            mres.scored,
            mres.surfaced,
            mres.queued,
            describe_cost(mres.cost),
        )
    except gateway.CallRefused:
        conn.rollback()
        res.scoring_s = time.monotonic() - scoring_start
        state.hit = True
        state.reason = state.reason or "deadline"
        log.warning("matching cut off by the daily %s", gateway.refusal_label())
    except Exception as e:
        conn.rollback()
        res.scoring_s = time.monotonic() - scoring_start
        log.exception("matching pipeline failed")
        res.errors["matching"] = applog.redact(f"{type(e).__name__}: {e}")


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

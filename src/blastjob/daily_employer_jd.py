"""The employer-description steps of the daily run (issue #17).

Kept out of `daily.py` (already past the file-size budget). Two steps, both
fail-soft like `_closure_pass`: an exception rolls the connection back, lands in
`res.errors["employer_jd"]` and the run carries on.

* `free_copy_step`: before matching, local only. A snippet row with a held
  authoritative sibling takes its full text now, so matching scores it once.
* `lookup_step`: after closure. The request-metered network lookup (only when
  `discovery.employer_jd_lookup`), then re-scores every row whose text changed.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

from blastjob import logging as applog
from blastjob import secrets as secrets_mod
from blastjob.core import employer_jd
from blastjob.core.closure import ClosureResult
from blastjob.core.closure_links import LinkDeps
from blastjob.llm.cost import CostTracker
from blastjob.matching import rescore
from blastjob.models.config import BlastJobConfig

if TYPE_CHECKING:
    from blastjob.daily import DailyResult

log = logging.getLogger(__name__)

ERROR_KEY = "employer_jd"
_FAILURE_STATUSES = frozenset({"error", "transient"})


async def free_copy_step(
    cfg: BlastJobConfig, conn: sqlite3.Connection, res: DailyResult, *, dry_run: bool
) -> employer_jd.UpgradeResult | None:
    try:
        return await employer_jd.run_upgrade(
            cfg,
            conn,
            min_score=cfg.discovery.employer_jd_min_score,
            network=False,
            write=not dry_run,
            request_limit=0,
        )
    except Exception as e:
        conn.rollback()
        log.exception("employer description copy step failed")
        res.errors[ERROR_KEY] = applog.redact(f"{type(e).__name__}: {e}")
        return None


def _rescore_ids(
    upgrade: employer_jd.UpgradeResult, closure_res: ClosureResult | None
) -> list[int]:
    reverted_by_closure = closure_res.reverted_jd if closure_res else []
    ids = [*upgrade.upgraded, *upgrade.reverted, *reverted_by_closure, *upgrade.stale_kept]
    return list(dict.fromkeys(ids))


def _outcomes_by_job(outcomes: list[employer_jd.RowOutcome]) -> list[employer_jd.RowOutcome]:
    """One outcome per row across the two steps. A dry run writes nothing, so a row
    the free step would upgrade is offered to the network step again; counting both
    reports it twice. The later (network) outcome wins, unless it only says the row
    was skipped and the earlier one acted."""
    acted = employer_jd.STORED | employer_jd.WOULD_STORE | {"reverted", "would_revert"}
    by_job: dict[int, employer_jd.RowOutcome] = {}
    for o in outcomes:
        earlier = by_job.get(o.job_id)
        if earlier is None or o.status in acted or earlier.status not in acted:
            by_job[o.job_id] = o
    return list(by_job.values())


def _note(
    free: employer_jd.UpgradeResult | None,
    net: employer_jd.UpgradeResult,
    *,
    rescored: int,
    rescore_failed: int,
) -> str:
    """One plain line, or '' when there is nothing a human should read."""
    outcomes = _outcomes_by_job([*(free.outcomes if free else []), *net.outcomes])
    acted = employer_jd.STORED | employer_jd.WOULD_STORE | {"reverted", "would_revert"}
    failures = sum(o.status in _FAILURE_STATUSES for o in outcomes) + rescore_failed
    if not (any(o.status in acted for o in outcomes) or failures or rescored):
        return ""
    merged = employer_jd.UpgradeResult(
        outcomes=outcomes, reverted=[*(free.reverted if free else []), *net.reverted]
    )
    merged.requests_used = net.requests_used
    not_found = sum(o.status == "no_match" for o in outcomes)
    tail = f" Re-scored {rescored}; not found {not_found}; failures {failures}."
    return merged.summary() + tail


async def lookup_step(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    res: DailyResult,
    closure_res: ClosureResult | None,
    deps: LinkDeps,
    *,
    free: employer_jd.UpgradeResult | None,
    dry_run: bool,
    score: bool,
) -> None:
    d = cfg.discovery
    secrets_mod.load_secrets_env()
    rescored = failed = 0
    try:
        net = await employer_jd.run_upgrade(
            cfg,
            conn,
            min_score=d.employer_jd_min_score,
            network=d.employer_jd_lookup,
            request_limit=d.employer_jd_daily_requests,
            skip_search_ids=closure_res.searched if closure_res else frozenset(),
            write=not dry_run,
            deps=deps,
        )
        if score and not dry_run:
            counts = await rescore.rescore_jobs(
                cfg, conn, CostTracker(), _rescore_ids(net, closure_res)
            )
            rescored, failed = counts.undecided + counts.kept, len(counts.failed)
    except Exception as e:
        conn.rollback()
        log.exception("employer description lookup failed")
        res.errors[ERROR_KEY] = applog.redact(f"{type(e).__name__}: {e}")
        return
    note = _note(free, net, rescored=rescored, rescore_failed=failed)
    if note:
        res.notes[ERROR_KEY] = note
        log.info(note)

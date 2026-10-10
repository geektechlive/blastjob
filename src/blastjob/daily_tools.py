"""Hand-run data maintenance flags for `blastjob-daily`.

Kept out of `daily.py` so the daily run's module stays about the daily run.
Each flag prints a report and exits; none sends anything or records a run.

- `--repair-linkedin-titles-report` / `--repair-linkedin-titles [--accept ids]`:
  split LinkedIn rows stored from the whole-card alert layout
  (`discovery.linkedin_repair`). The report writes nothing.
- `--duplicate-applications`: kept rows with an application that share a
  company key and normalized title with another one (read-only).
"""

from __future__ import annotations

import argparse
import sqlite3

from blastjob.models.config import BlastJobConfig


def recluster_cli(cfg: BlastJobConfig, conn: sqlite3.Connection, *, apply: bool) -> str:
    """The same-role backfill (`--recluster-report` / `--recluster`): the report
    always; the merges only if `apply`. The report is computed before anything
    is written, so it describes the stages as they are now and as they would
    be. (Moved from daily.py, which still imports it under the same name.)"""
    from blastjob.core import recluster
    from blastjob.web import recluster_view

    plan = recluster.plan_recluster(conn, cfg.discovery.commute_radius_miles, cfg.discovery)
    items = recluster_view.describe(conn, plan)
    applied = recluster.apply_recluster(conn, plan) if apply else None
    return recluster_view.format_report(items, applied=applied)


def _id_list(value: str) -> frozenset[int]:
    try:
        return frozenset(int(v) for v in value.split(",") if v.strip())
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"expected comma-separated job ids, got {value!r}") from e


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument(
        "--repair-linkedin-titles-report",
        action="store_true",
        help="print the whole-card LinkedIn title repairs and the merges they cause; "
        "changes nothing",
    )
    ap.add_argument(
        "--repair-linkedin-titles",
        action="store_true",
        help="apply the structural whole-card splits (plus --accept ids), then recluster them",
    )
    ap.add_argument(
        "--accept",
        type=_id_list,
        default=frozenset(),
        metavar="IDS",
        help="comma-separated job ids whose vocabulary-only split you confirm (e.g. 1917,914)",
    )
    ap.add_argument(
        "--duplicate-applications",
        action="store_true",
        help="list kept applications that look like the same role (read-only)",
    )
    bf_help = "index packet files + past application events (core/backfill.py)"
    ap.add_argument("--backfill-artifacts-report", action="store_true", help=f"report: {bf_help}")
    ap.add_argument("--backfill-artifacts", action="store_true", help=bf_help)


def dispatch(args: argparse.Namespace, cfg: BlastJobConfig, conn: sqlite3.Connection) -> str | None:
    """The report for a maintenance flag, or None when none was given."""
    if args.repair_linkedin_titles_report or args.repair_linkedin_titles:
        from blastjob.discovery import linkedin_repair as lr

        outcome = lr.run_repair(
            conn,
            cfg.discovery.commute_radius_miles,
            cfg.discovery,
            apply=args.repair_linkedin_titles,
            accept=args.accept,
        )
        return lr.format_repair(outcome)
    if args.duplicate_applications:
        from blastjob.core import duplicate_applications as dup

        return dup.format_pairs(dup.duplicate_applications(conn))
    if args.backfill_artifacts_report or args.backfill_artifacts:
        from blastjob.core import backfill

        return backfill.run(conn, apply=args.backfill_artifacts)
    return None

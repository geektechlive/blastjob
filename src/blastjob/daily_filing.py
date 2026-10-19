"""Packet filing in `blastjob-daily` (issue #20).

- `filing_pass`: the daily run files every entry after closure (closure is what
  moves a kept job to posting_closed). A dry run only logs the report.
- `--file-packets-report` / `--file-packets`: the hand-run backfill. First the
  rename step (`core/packet_renames.py`: kept jobs' newest packet documents get
  the current capped token), then the filing pass. The report writes nothing:
  no rename, no move, no lock file, no row.

Kept out of `daily.py`, which is already past the file-size budget.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from blastjob import config as cfg_mod
from blastjob.core import filing_lock, packet_filing, packet_renames
from blastjob.models.config import BlastJobConfig

log = logging.getLogger(__name__)


def filing_pass(cfg: BlastJobConfig, conn: sqlite3.Connection, *, dry_run: bool) -> None:
    """Never sinks the run: a failure is a logged warning."""
    try:
        res = packet_filing.file_packets(conn, cfg_mod.output_dir(cfg), apply=not dry_run)
    except Exception:  # noqa: BLE001 -- filing never fails the daily run
        log.warning("packet filing pass failed", exc_info=True)
        return
    for line in packet_filing.format_report(res).splitlines():
        log.info("filing: %s", line)


def _renames(conn: sqlite3.Connection, cfg: BlastJobConfig) -> str:
    """Apply the rename step under the exclusive filing lock."""
    root = cfg_mod.output_dir(cfg)
    with filing_lock.exclusive(root) as ok:
        if not ok:
            return "Renames skipped: a writer holds the filing lock (or no output folder)."
        if packet_filing.running_build(conn, datetime.now()):
            return "Renames skipped: a build or refine is running."
        plan = packet_renames.plan_renames(conn, root)
        result = packet_renames.apply_renames(conn, root, plan)
    return packet_renames.format_renames(plan, applied=True, result=result)


def file_packets_cli(cfg: BlastJobConfig, conn: sqlite3.Connection, *, apply: bool) -> str:
    root = cfg_mod.output_dir(cfg)
    if apply:
        renames = _renames(conn, cfg)
    else:
        renames = packet_renames.format_renames(
            packet_renames.plan_renames(conn, root), applied=False
        )
    filing = packet_filing.format_report(packet_filing.file_packets(conn, root, apply=apply))
    return f"Output folder: {root}\n\n{renames}\n\n{filing}"

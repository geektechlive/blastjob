"""The daily recruiter-label pass (#83): record new messages from the owner's one label.

Runs next to the reply scan and, like it, makes no model call, so it also runs after the
cutoff. A label that is off, or credentials that are absent, is a silent no-op. A label the
server does not have is reported as a digest note (a check that did not run is never an
all-clear), and any failure is a source-error line; neither sinks the run. A dry run reads
but records nothing. See docs/how-it-works/discovery.md.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

from blastjob import logging as applog
from blastjob import secrets as secrets_mod
from blastjob.models.config import BlastJobConfig

if TYPE_CHECKING:
    from blastjob.daily import DailyResult

log = logging.getLogger(__name__)

NOTE_KEY = "recruiter_mail"
NOTE_SUFFIX = "Check the recruiter label in Settings."


async def label_pass(
    cfg: BlastJobConfig, conn: sqlite3.Connection, res: DailyResult, *, dry_run: bool = False
) -> None:
    from blastjob.discovery import label_mail

    if not cfg.discovery.recruiter_label.strip():
        return
    secrets_mod.load_secrets_env()
    try:
        scan = await label_mail.scan_label(cfg, conn, record=not dry_run)
    except Exception as e:
        conn.rollback()
        log.exception("recruiter label scan failed")
        res.errors[NOTE_KEY] = applog.redact(f"{type(e).__name__}: {e}")
        return
    if scan.problem:
        res.notes[NOTE_KEY] = f"{scan.problem}. {NOTE_SUFFIX}"
    elif scan.recorded:
        log.info("recruiter mail: %d new message(s) to triage", scan.recorded)

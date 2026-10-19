"""The "N scores predate your last profile or work-history change" note (#29).

`base.html` lazy-loads it beside the Re-score (Match) button, the only action
that redoes an old-fingerprint score; the daily run never does. Counted against
the fingerprint THIS process would score with, so it answers "what would Match
re-score", including a provider difference between the cockpit and the daily
run. Empty when nothing is stale.
"""

from __future__ import annotations

import logging
import sqlite3

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from blastjob.matching import pipeline
from blastjob.matching.fingerprint import scoring_fingerprint
from blastjob.web.deps import Deps

log = logging.getLogger(__name__)


def register(app: FastAPI, deps: Deps) -> None:
    @app.get("/scores/stale", response_class=HTMLResponse)
    def stale_scores(request: Request) -> HTMLResponse:
        cfg = deps.load_config()
        try:
            fingerprint = scoring_fingerprint(cfg, pipeline.work_history_text(cfg))
            c = deps.conn()
            try:
                n = pipeline.stale_score_count(c, fingerprint)
            finally:
                c.close()
        except (OSError, ValueError, sqlite3.Error):
            # ValueError covers an undecodable work_history.md (UnicodeDecodeError).
            # Rendered on every page: a note that cannot be computed is omitted,
            # never a broken header.
            log.warning("stale-score count failed", exc_info=True)
            n = 0
        return deps.render(request, "_stale_scores.html", n=n)

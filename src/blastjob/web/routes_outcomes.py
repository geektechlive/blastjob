"""The outcome report and per-job history: which documents went out, and what
happened to them.

Read `core/packet_sends.py`'s module docstring before changing anything below
-- it states a HARD CONSTRAINT this route module exists to respect, not just
implement: descriptive bookkeeping only. `outcome_report(conn)` and
`job_event_log`/`stage_durations` had no caller outside tests until the
2026-09-17 dead-code review (WIRE, not delete -- this is the feature the user
asked for by name). This module is that wiring, and it inherits the
constraint whole: no ranking, no win/success rate, no aggregate across sends,
and no sort control keyed on score or stage. `/outcomes` renders
`outcome_report`'s rows in exactly the order the query returns them
(`sent_at`) -- there is no `sort` query parameter here on purpose, unlike
`web/routes_stages.py`'s stage pages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from blastjob.core import packet_sends as ps
from blastjob.web.deps import Deps


def _basename(path: str) -> str:
    return Path(path).name if path else ""


def _outcome_rows(report: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`outcome_report`'s rows, plus the file BASENAME `/jobs/{id}/files/{name}`
    expects -- computed here, in Python, rather than in the template, so the
    template stays a dumb renderer of what this function already decided.
    Order is preserved exactly; this is formatting, not a sort.
    """
    rows = []
    for send in report:
        rows.append(
            {
                **send,
                "resume_name": _basename(send["resume_path"]),
                "cover_letter_name": _basename(send["cover_letter_path"]),
            }
        )
    return rows


def register(app: FastAPI, deps: Deps) -> None:
    @app.get("/outcomes", response_class=HTMLResponse)
    async def outcomes_page(request: Request) -> HTMLResponse:
        c = deps.conn()
        try:
            report = ps.outcome_report(c)
        finally:
            c.close()
        return deps.render(request, "outcomes.html", report=_outcome_rows(report))

    @app.get("/jobs/{job_id}/history", response_class=HTMLResponse)
    async def job_history(request: Request, job_id: int) -> HTMLResponse:
        """One job's own timeline: every send it ever produced, interleaved
        with every outcome recorded against any of them, oldest first, plus
        how long it sat at each stage. Renders `_job_history.html` directly
        (no `base.html`): a self-contained fragment, so it can be embedded
        into the job detail page later via an `{% include %}` without pulling
        in nav chrome it does not need -- see the INTEGRATION PATCH for that
        follow-up, which is out of this workstream's ownership of
        `detail.html`.
        """
        c = deps.conn()
        try:
            job = c.execute("SELECT id, company, title FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise HTTPException(status_code=404, detail="job not found")
            events = ps.job_event_log(c, job_id)
        finally:
            c.close()
        return deps.render(
            request,
            "_job_history.html",
            job=job,
            events=events,
            durations=ps.stage_durations(events),
        )

"""`POST /jobs/{id}/employer-link`: the user pastes the employer's own posting URL.

Stored as `employer_link_source='manual'` (external id = url = the URL). It is
the preferred apply link (web/apply_link) and DISPLAY-ONLY: closure never
probes it or treats it as evidence (core/closure.MANUAL_LINK).

Validated syntactically, never fetched: an absolute http(s) URL
(`safe_external_url`) whose host is not an aggregator
(`page_fetch.fetchable`, which also rejects linkedin.com). An empty submit
clears a manual link, and only a manual one; a link closure found stays.
Success answers HX-Refresh so the header's apply link updates; a refusal
re-renders the same-role block with the reason.
"""

from __future__ import annotations

import sqlite3

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse

from blastjob.core.page_fetch import fetchable
from blastjob.web import same_role as srv
from blastjob.web.apply_link import MANUAL_LINK
from blastjob.web.deps import Deps
from blastjob.web.security import require_csrf
from blastjob.web.urls import safe_external_url

MAX_URL_CHARS = 2000
REJECTED = (
    "Not saved: paste the employer's own posting URL (http or https). LinkedIn and job "
    "boards like Indeed or Glassdoor are already covered by the posting itself."
)


def validate(url: str) -> str:
    """The URL to store, or "" when it must be refused."""
    url = url.strip()
    if len(url) > MAX_URL_CHARS or not safe_external_url(url) or not fetchable(url):
        return ""
    return url


def save_manual_link(conn: sqlite3.Connection, job_id: int, url: str) -> None:
    conn.execute(
        "UPDATE jobs SET employer_link_source=?, employer_link_external_id=?, "
        "employer_link_url=? WHERE id=?",
        (MANUAL_LINK, url, url, job_id),
    )
    conn.commit()


def clear_manual_link(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET employer_link_source='', employer_link_external_id='', "
        "employer_link_url='' WHERE id=? AND employer_link_source=?",
        (job_id, MANUAL_LINK),
    )
    conn.commit()


def register(app: FastAPI, deps: Deps) -> None:
    @app.post("/jobs/{job_id}/employer-link", response_class=HTMLResponse)
    async def employer_link(
        request: Request,
        job_id: int,
        url: str = Form(""),
        _: None = Depends(require_csrf),
    ) -> HTMLResponse:
        c = deps.conn()
        try:
            job = c.execute("SELECT id, company FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                return HTMLResponse("Not found", status_code=404)
            if not url.strip():
                clear_manual_link(c, job_id)
                return HTMLResponse("", headers={"HX-Refresh": "true"})
            clean = validate(url)
            if clean:
                save_manual_link(c, job_id, clean)
                return HTMLResponse("", headers={"HX-Refresh": "true"})
            ctx = srv.detail_context(c, job, deps.radius(), deps.load_config().discovery)
            return deps.render(request, srv.PARTIAL, **{**ctx, "employer_link_error": REJECTED})
        finally:
            c.close()

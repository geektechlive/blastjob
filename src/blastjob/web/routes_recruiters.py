"""Recruiter desk writes (#98): set a status, log a job-less note.

Both are state-changing, so both sit behind `require_csrf` (token plus Origin/Host checks), and
only an effective recruiter (the desk's own predicate) is accepted: anyone else is a 404.
Draft-only: logging a note records that the owner spoke or wrote; nothing is sent or contacted.
See docs/how-it-works/network.md.
"""

from __future__ import annotations

import datetime as dt
import re

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse

from blastjob.core import outreach_asks as oa
from blastjob.core import recruiter_status as rs
from blastjob.web import recruiter_view
from blastjob.web.deps import Deps
from blastjob.web.security import require_csrf

_TOKEN = re.compile(r"^[0-9a-f]{32}$")


def _bad(message: str) -> HTMLResponse:
    return HTMLResponse(message, status_code=400)


def register(app: FastAPI, deps: Deps) -> None:
    def render_row(request: Request, conn, contact_id: int):
        ctx = recruiter_view.row_context(conn, contact_id)
        if ctx is None:
            return HTMLResponse("Not found", status_code=404)
        return deps.render(request, "_recruiter_row.html", **ctx)

    @app.post("/network/contacts/{contact_id}/recruiter-status", response_class=HTMLResponse)
    async def recruiter_status(
        request: Request,
        contact_id: int,
        status: str = Form(...),
        reason: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        c = deps.conn()
        try:
            if not rs.is_effective_recruiter(c, contact_id):
                return HTMLResponse("Not found", status_code=404)
            try:
                rs.set_status(c, contact_id, status, reason)
            except rs.StatusError as e:
                return _bad(str(e))
            return render_row(request, c, contact_id)
        finally:
            c.close()

    @app.post("/network/contacts/{contact_id}/asks", response_class=HTMLResponse)
    async def recruiter_note(
        request: Request,
        contact_id: int,
        channel: str = Form(...),
        asked_on: str = Form(""),
        follow_up_on: str = Form(""),
        note: str = Form(""),
        request_token: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        if request_token and not _TOKEN.match(request_token):
            return _bad("Bad request token.")
        c = deps.conn()
        try:
            if not rs.is_effective_recruiter(c, contact_id):
                return HTMLResponse("Not found", status_code=404)
            try:
                oa.log_ask(
                    c,
                    job_id=None,
                    network_contact_id=contact_id,
                    contact_id=None,
                    kind="recruiter",
                    channel=channel,
                    asked_on=asked_on.strip() or dt.date.today().isoformat(),
                    follow_up_on=follow_up_on.strip() or None,
                    note=note,
                    request_token=request_token,
                )
            except oa.AskError as e:
                return _bad(str(e))
            return render_row(request, c, contact_id)
        finally:
            c.close()

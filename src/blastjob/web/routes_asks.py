"""Outreach-ask routes (#97): log an ask for a person on a job, and update it later.

Both are state-changing, so both sit behind `require_csrf` (token plus Origin/Host checks).
Draft-only: logging an ask records that the owner asked; nothing is sent or contacted.
See docs/how-it-works/network.md.
"""

from __future__ import annotations

import datetime as dt
import re

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse

from blastjob.core import outreach_asks as oa
from blastjob.web import service
from blastjob.web.asks_view import ask_context
from blastjob.web.deps import Deps
from blastjob.web.security import require_csrf

_TOKEN = re.compile(r"^[0-9a-f]{32}$")


def _bad(message: str) -> HTMLResponse:
    return HTMLResponse(message, status_code=400)


def register(app: FastAPI, deps: Deps) -> None:
    def render_asks(request: Request, conn, job_id: int):
        job = service.job_detail(conn, job_id)
        return deps.render(request, "_asks.html", job=job, **ask_context(conn, job_id))

    @app.post("/jobs/{job_id}/asks", response_class=HTMLResponse)
    async def log_ask(
        request: Request,
        job_id: int,
        network_contact_id: int | None = Form(None),
        contact_id: int | None = Form(None),
        kind: str = Form(...),
        channel: str = Form(...),
        asked_on: str = Form(""),
        follow_up_on: str = Form(""),
        note: str = Form(""),
        request_token: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        # At most one recipient id: two ids (even the same number) are a malformed request.
        if network_contact_id is not None and contact_id is not None:
            return _bad("Name one person, not two.")
        if request_token and not _TOKEN.match(request_token):
            return _bad("Bad request token.")
        c = deps.conn()
        try:
            if service.job_detail(c, job_id) is None:
                return HTMLResponse("Not found", status_code=404)
            try:
                oa.log_ask(
                    c,
                    job_id=job_id,
                    network_contact_id=network_contact_id,
                    contact_id=contact_id,
                    kind=kind,
                    channel=channel,
                    asked_on=asked_on.strip() or dt.date.today().isoformat(),
                    follow_up_on=follow_up_on.strip() or None,
                    note=note,
                    request_token=request_token,
                )
            except oa.AskError as e:
                return _bad(str(e))
            return render_asks(request, c, job_id)
        finally:
            c.close()

    @app.post("/asks/{ask_id}", response_class=HTMLResponse)
    async def update_ask(
        request: Request,
        ask_id: int,
        status: str = Form(...),
        follow_up_on: str = Form(""),
        note: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        c = deps.conn()
        try:
            ask = oa.get_ask(c, ask_id)
            if ask is None:
                return HTMLResponse("Not found", status_code=404)
            try:
                oa.update_ask(c, ask_id, status=status, follow_up_on=follow_up_on, note=note)
            except oa.AskError as e:
                return _bad(str(e))
            if ask["job_id"] is None:
                return HTMLResponse("Saved.")
            return render_asks(request, c, int(ask["job_id"]))
        finally:
            c.close()

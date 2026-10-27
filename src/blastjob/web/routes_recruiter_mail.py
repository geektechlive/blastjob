"""The Recruiter mail page and its decisions (#83).

Every write sits behind `require_csrf` (token plus Origin and Host checks). A decision carries
the row's `version` and is refused with 409 when the row has changed since the page was
rendered. Draft-only: linking or dismissing records the owner's choice; nothing is sent,
moved or labelled in the mailbox. Text from the mail is rendered only through Jinja
autoescaping, and no link from a message is ever rendered. See
docs/how-it-works/discovery.md.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, Path, Request
from fastapi.responses import HTMLResponse

from blastjob.core import mail_triage as mt
from blastjob.web import recruiter_mail
from blastjob.web.deps import Deps
from blastjob.web.security import require_csrf

STALE_MESSAGE = (
    "That message changed since you opened this page, so nothing was saved. "
    "The current state is shown below."
)
CONFLICT = 409
MAX_ID_DIGITS = 18  # a SQLite id is 63-bit; longer is not a row, and int() on huge text is slow


def register(app: FastAPI, deps: Deps) -> None:
    def _page(
        request: Request, conn: sqlite3.Connection, status: int = 200, **extra: Any
    ) -> HTMLResponse:
        label = deps.load_config().discovery.recruiter_label.strip()
        response = deps.render(
            request, "recruiter_mail.html", **recruiter_mail.page_context(conn, label), **extra
        )
        response.status_code = status
        return response

    @app.get("/recruiter-mail", response_class=HTMLResponse)
    async def recruiter_mail_page(request: Request):
        c = deps.conn()
        try:
            return _page(request, c)
        finally:
            c.close()

    @app.post("/recruiter-mail/{triage_id}/decide", response_class=HTMLResponse)
    async def recruiter_mail_decide(
        request: Request,
        triage_id: Annotated[int, Path(ge=0, le=mt.MAX_ROW)],
        version: Annotated[int, Form(ge=0, le=mt.MAX_ROW)],
        action: str = Form(""),
        target_id: str = Form(""),
        change: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        c = deps.conn()
        try:
            text = target_id.strip()
            ok = text.isascii() and text.isdigit() and len(text) <= MAX_ID_DIGITS
            target = int(text) if ok else None
            if text and target is None:
                return _page(request, c, 400, error="Choose what to link this message to.")
            try:
                mt.decide(
                    c,
                    triage_id,
                    version=version,
                    action=action,
                    target_id=target,
                    change=change.strip() == "1",
                )
            except mt.TriageNotFound:
                return HTMLResponse("Not found", status_code=404)
            except mt.StaleDecision:
                return _page(request, c, CONFLICT, error=STALE_MESSAGE)
            except mt.TriageError as e:
                return _page(request, c, 400, error=str(e))
            return _page(request, c)
        finally:
            c.close()

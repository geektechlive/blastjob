"""Recruiter-submission routes and the apply-time enforcement hook (#100, ADR 0063).

Every write sits behind `require_csrf` (token plus Origin and Host checks). Draft-only: a
record that a recruiter put the owner forward; nothing is sent. The server, not the page, is
the guard: both routes that can mark a job applied pass `apply_guard` into the state write, so
a stale page that never showed the warning is refused (409) too. See
docs/how-it-works/same-role.md.
"""

from __future__ import annotations

import datetime as dt
import html
import sqlite3
from collections.abc import Callable

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse

from blastjob.core import network_views as nv
from blastjob.core import submissions as subs
from blastjob.models.config import BlastJobConfig
from blastjob.web import service, stage_moves, view_helpers
from blastjob.web.deps import Deps
from blastjob.web.security import require_csrf

CONFLICT = 409
# Marks a 409 as this feature's warning, so cockpit.js swaps only these (not e.g. a run 409).
WARNING_HEADER = {"X-Submission-Warning": "1"}
PARTIAL = "_submissions.html"


def section_context(
    conn: sqlite3.Connection,
    job_id: int,
    cfg: BlastJobConfig,
    *,
    error: str = "",
    warning: str = "",
    form: dict[str, str] | None = None,
) -> dict[str, object]:
    """Template inputs for the "Recruiter submissions" block of one job's page."""
    radius_miles, geo = cfg.discovery.commute_radius_miles, cfg.discovery
    warning_pair = subs.first_apply_warning(conn, job_id, radius_miles, geo)
    return {
        "sub_job_id": job_id,
        # For the job page's Applied buttons (_app_status.html); "" once applied_at is set.
        "first_apply_confirm": warning_pair[0],
        "submission_token": warning_pair[1],
        "submissions": subs.submissions_for_role(conn, job_id, radius_miles, geo),
        "sub_recruiters": [{"id": d["id"], "name": d["name"]} for d in nv.recruiter_desk(conn)],
        "sub_today": dt.date.today().isoformat(),
        "sub_error": error,
        "sub_warning": warning,
        "sub_form": form or {},
        "sub_name_max": subs.MAX_NAME_CHARS,
        "sub_firm_max": subs.MAX_FIRM_CHARS,
        "sub_note_max": subs.MAX_NOTE_CHARS,
    }


def apply_guard(deps: Deps, job_id: int, ack: str) -> subs.Guard:
    """The in-transaction check for a move that can mark the job applied."""
    return subs.ack_guard(job_id, deps.radius(), deps.load_config().discovery, ack)


def stage_move(
    deps: Deps,
    conn: sqlite3.Connection,
    job_id: int,
    action: str,
    ack: str,
    load_config: Callable[[], BlastJobConfig],
) -> tuple[bool, HTMLResponse | None]:
    """`stage_moves.run_action` with the submission check. A missing acknowledgement is raised
    inside the state write's transaction (nothing is written) and answered 409 with a reload
    request, because a stale page never showed the warning."""
    try:
        guard = apply_guard(deps, job_id, ack)
        return stage_moves.run_action(conn, job_id, action, load_config, guard=guard), None
    except subs.AckRequired as e:
        headers = {"HX-Refresh": "true", **WARNING_HEADER}
        return False, HTMLResponse(html.escape(e.message), CONFLICT, headers=headers)


def status_move(
    deps: Deps,
    request: Request,
    conn: sqlite3.Connection,
    job_id: int,
    status: str,
    expected: list[str],
    ack: str,
) -> tuple[bool, HTMLResponse | None]:
    """`service.set_application_state` with the submission check; a refusal re-renders the
    status panel with the warning (409)."""
    try:
        guard = apply_guard(deps, job_id, ack)
        return service.set_application_state(conn, job_id, status, expected, guard=guard), None
    except subs.AckRequired as e:
        resp = view_helpers.render_app_status(
            deps, request, conn, job_id, submission_block=e.message, status_code=CONFLICT
        )
        resp.headers.update(WARNING_HEADER)
        return False, resp


def _bad(message: str) -> HTMLResponse:
    return HTMLResponse(message, status_code=400)


def _parse_contact(raw: str) -> int | None:
    text = raw.strip()
    if not text:
        return None
    if not (text.isascii() and text.isdigit()):
        raise subs.SubmissionError("Bad recruiter.")
    return int(text)


def register(app: FastAPI, deps: Deps) -> None:
    def render_section(request: Request, conn: sqlite3.Connection, job_id: int, **extra):
        job = service.job_detail(conn, job_id)
        ctx = section_context(conn, job_id, deps.load_config(), **extra)
        return deps.render(request, PARTIAL, job=job, **ctx)

    @app.post("/jobs/{job_id}/submissions", response_class=HTMLResponse)
    async def log_submission(
        request: Request,
        job_id: int,
        network_contact_id: str = Form(""),
        recruiter_name: str = Form(""),
        firm: str = Form(""),
        submitted_on: str = Form(""),
        note: str = Form(""),
        log_anyway: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        c = deps.conn()
        try:
            if service.job_detail(c, job_id) is None:
                return HTMLResponse("Not found", status_code=404)
            day = submitted_on.strip() or dt.date.today().isoformat()
            try:
                contact = _parse_contact(network_contact_id)
                # One write lock from the check to the insert: a direct application cannot
                # land in between. add_submission commits.
                c.execute("BEGIN IMMEDIATE")
                # A bad form is a 400 before it is ever a warning.
                subs.check_input(c, contact, recruiter_name, firm, day, note)
                prior = subs.prior_applications(
                    c, job_id, deps.radius(), deps.load_config().discovery
                )
                if prior and log_anyway.strip() != "1":
                    form = {
                        "network_contact_id": network_contact_id,
                        "recruiter_name": recruiter_name,
                        "firm": firm,
                        "submitted_on": day,
                        "note": note,
                    }
                    c.rollback()
                    resp = render_section(
                        request, c, job_id, warning=subs.prior_application_warning(prior), form=form
                    )
                    resp.status_code = CONFLICT
                    resp.headers.update(WARNING_HEADER)
                    return resp
                subs.add_submission(
                    c,
                    job_id=job_id,
                    network_contact_id=contact,
                    recruiter_name=recruiter_name,
                    firm=firm,
                    submitted_on=day,
                    note=note,
                )
            except subs.SubmissionError as e:
                c.rollback()
                return _bad(str(e))
            return render_section(request, c, job_id)
        finally:
            c.close()

    @app.post("/submissions/{submission_id}/delete", response_class=HTMLResponse)
    async def delete_submission(
        request: Request, submission_id: int, _: None = Depends(require_csrf)
    ):
        c = deps.conn()
        try:
            row = subs.get_submission(c, submission_id)
            if row is None:
                return HTMLResponse("Not found", status_code=404)
            subs.delete_submission(c, submission_id)
            return render_section(request, c, int(row["job_id"]))
        finally:
            c.close()

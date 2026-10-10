"""Rendering helpers shared by more than one route module.

These lived inside `create_app`'s closure, which is precisely why 66 routes had
to live there too. They take `Deps` explicitly now, so a route module can import
them and `create_app` can shrink to wiring.

Behavior is unchanged from the closure versions -- this is a move, and the
existing route tests are the proof. Anything that looked worth improving on the
way past was deliberately left alone: a refactor that also changes behavior
cannot be verified by the tests that passed before it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from blastjob.core import same_role as sr
from blastjob.web import same_role as srv
from blastjob.web import service, status_filter
from blastjob.web import stage_lists as sl
from blastjob.web import stages as st
from blastjob.web.deps import Deps

# One-line page titles per list view.
VIEW_TITLES: dict[str, str] = {
    st.SURFACED: "Surfaced · cleared the bar",
    st.QUEUED: "Queue · borderline fit, or the location needs a look",
    "matches": "Matches · every undecided scored role, ranked",
    st.SHORTLIST: "Shortlist · roles you're weighing or tailoring",
    st.APPLIED: "Applied · in a hiring process",
    st.CLOSED: "Closed · finished",
}


def packet_role_slug(packet, title: str) -> str | None:
    """Which role's sidecars in a shared company-day folder belong to THIS job.
    Without it the groundedness panel can show another role's verdict."""
    if not packet or not packet.get("dir"):
        return None
    from blastjob.core.paths import resolve_role_slug

    return resolve_role_slug(Path(packet["dir"]), title or "")


def list_page(
    deps: Deps, request: Request, view: str, rows, c: sqlite3.Connection, sort: str = "fit"
) -> HTMLResponse:
    status = request.query_params.get("status", "all")
    all_jobs = service.annotate_network(c, rows)
    jobs, status_counts, chips = status_filter.filter_stage(all_jobs, view, status)
    if view in srv.CHIP_VIEWS:
        jobs = srv.annotate_rows(c, jobs, deps.radius(), deps.load_config().discovery)
    # Carry the pay toggle through filter links (surfaced/matches only).
    sort_q = "sort=comp&" if sort == "comp" else ""
    return deps.render(
        request,
        "dashboard.html",
        counts=sl.counts(c),
        jobs=jobs,
        view=view,
        view_title=VIEW_TITLES[view],
        sort=sort,
        status=status if status in status_counts else "all",
        status_counts=status_counts,
        status_filters=chips,
        sort_q=sort_q,
        due_count=sum(1 for j in all_jobs if j.get("followup_reason")),
        empty_reason=(
            sl.empty_reason(c, view)
            if not all_jobs
            else ("Nothing with this status. Show all above." if not jobs else "")
        ),
        threshold=deps.load_config().discovery.alignment_threshold,
    )


def render_app_status(
    deps: Deps, request: Request, c: sqlite3.Connection, job_id: int, send_note: str = ""
) -> HTMLResponse:
    """`send_note` is non-empty when a state change could NOT snapshot which
    documents went out. It is rendered, not swallowed: an as-sent record that
    silently did not happen is the failure that feature exists to prevent."""
    job = service.job_detail(c, job_id)
    reason = sl.followup_reason(job) if job is not None else ""
    confirm = (
        srv.apply_confirm(
            sr.possible_same_role(c, job_id, deps.radius(), deps.load_config().discovery)
        )
        if job
        else ""
    )
    return deps.render(
        request,
        "_app_status.html",
        job=job,
        followup_reason=reason,
        apply_confirm=confirm,
        send_note=send_note,
    )


def stage_page(deps: Deps, request: Request, view: str, sort: str = "fit") -> HTMLResponse:
    c = deps.conn()
    try:
        if view == st.SURFACED:
            rows = sl.surfaced_jobs(c, sort=sort)
        elif view == "matches":
            rows = sl.matches(c, sort=sort)
        else:
            rows = sl.STAGE_LISTS[view](c)
        return list_page(deps, request, view, rows, c, sort)
    finally:
        c.close()


def redirect(request: Request, path: str) -> RedirectResponse:
    """307 so a POST-shaped client keeps its method, and the query string is
    carried across -- /kept?status=drafted must not lose its filter on the way
    to /shortlist."""
    query = request.url.query
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=307)


def row_response(
    deps: Deps,
    request: Request,
    c: sqlite3.Connection,
    job_id: int,
    *,
    moved: bool,
    from_list: str,
) -> HTMLResponse:
    """What a row action answers. A missing job is a 404 (rendering the row
    against None raised a 500). From a stage list the job has left the list, so
    the answer is empty and the row's outerHTML swap removes it; if the move was
    refused the page reloads to show where the job really is. Elsewhere (the job
    page ignores the body) the updated row renders."""
    job = service.annotate_network_one(c, service.job_detail(c, job_id))
    if job is None:
        return HTMLResponse("Not found", status_code=404)
    if from_list:
        return HTMLResponse("", headers={} if moved else {"HX-Refresh": "true"})
    return deps.render(
        request,
        "_job_row.html",
        job=job,
        threshold=deps.load_config().discovery.alignment_threshold,
    )

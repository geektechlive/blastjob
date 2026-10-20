"""The list pages: the five pipeline stages, Matches, All, Compare, and the two
redirects that keep old links working.

Split out of `web/app.py` (2026-09-17), which had reached 1,309 lines with 66
routes inside one closure. Every handler here is the one that was there, moved
verbatim; the stage membership and sort rules still live in `web/stages.py` and
`web/stage_lists.py`, which remain the single source for both.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from blastjob.web import service, status_filter
from blastjob.web import stage_lists as sl
from blastjob.web import stages as st
from blastjob.web.deps import Deps
from blastjob.web.view_helpers import redirect, stage_page


def register(app: FastAPI, deps: Deps) -> None:
    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, sort: str = "fit"):
        return stage_page(deps, request, st.SURFACED, sort)

    @app.get("/queue", response_class=HTMLResponse)
    async def queue(request: Request):
        return stage_page(deps, request, st.QUEUED)

    @app.get("/matches", response_class=HTMLResponse)
    async def matches(request: Request, sort: str = "fit"):
        return stage_page(deps, request, "matches", sort)

    @app.get("/shortlist", response_class=HTMLResponse)
    async def shortlist(request: Request):
        return stage_page(deps, request, st.SHORTLIST)

    @app.get("/applied", response_class=HTMLResponse)
    async def applied(request: Request):
        return stage_page(deps, request, st.APPLIED)

    @app.get("/closed", response_class=HTMLResponse)
    async def closed_page(request: Request):
        return stage_page(deps, request, st.CLOSED)

    @app.get("/kept")
    async def kept(request: Request):
        """Kept split into Shortlist / Applied / Closed; old links land here."""
        return redirect(request, "/shortlist")

    @app.get("/followups")
    async def followups(request: Request):
        """Follow-ups live on the Applied page now."""
        return redirect(request, "/applied")

    @app.get("/all", response_class=HTMLResponse)
    async def all_jobs(request: Request, status: str = "all"):
        c = deps.conn()
        try:
            cfg = deps.load_config()
            every = service.annotate_network(c, service.all_active(c, cfg), cfg.candidate)
            jobs, status_counts = status_filter.filter_jobs(every, status)
            return deps.render(
                request,
                "all.html",
                jobs=jobs,
                counts=sl.counts(c),
                status=status if status in status_counts else "all",
                status_counts=status_counts,
                status_filters=status_filter.FILTERS,
            )
        finally:
            c.close()

    @app.get("/compare", response_class=HTMLResponse)
    async def compare_view(request: Request, source: str = "", state: str = "surfaced"):
        from blastjob.web import compare as cmp

        state = cmp.normalize_state(state)
        c = deps.conn()
        try:
            return deps.render(
                request,
                "compare.html",
                rows=cmp.compare_rows(
                    c,
                    source=source,
                    state=state,
                    floor=deps.load_config().discovery.comp_floor_usd,
                ),
                source=source,
                state=state,
                state_label=st.LABELS[state],
                stage_choices=[(s, st.LABELS[s]) for s in st.STAGES],
            )
        finally:
            c.close()

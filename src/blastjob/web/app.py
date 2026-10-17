"""FastAPI + HTMX cockpit. Bound to 127.0.0.1 by the launcher; state-changing
routes require the per-launch CSRF token; all untrusted job text is rendered
through Jinja autoescaping.

`create_app` takes injectable load/save config callables so tests can run against
a temp data dir without touching ~/.blastjob or the real config file.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from blastjob import config as cfg_mod
from blastjob.core import closure
from blastjob.core import company as co
from blastjob.core import network as net
from blastjob.core import network_views as nv
from blastjob.db import client as dbc
from blastjob.llm.cost import CostTracker
from blastjob.models.config import BlastJobConfig, DiscoveryConfig
from blastjob.models.job import ARCHIVE_REASON_LABELS, ARCHIVE_REASONS, ReviewState
from blastjob.models.tracking import STATUSES
from blastjob.web import employer_jd as web_employer_jd
from blastjob.web import (
    fitscore,
    packet_view,
    packets,
    people,
    progress,
    routes_activity,
    routes_network,
    routes_outcomes,
    routes_packets,
    routes_settings,
    routes_stages,
    service,
)
from blastjob.web import manual_job as mj
from blastjob.web import routes_employer_link as employer_link
from blastjob.web import same_role as srv
from blastjob.web import stage_lists as sl
from blastjob.web import stage_moves as sm
from blastjob.web import stages as st
from blastjob.web import view_helpers as vh
from blastjob.web.deps import Deps
from blastjob.web.runtime import RUNTIME, Run
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN, LocalRequestGuard, require_csrf
from blastjob.web.upload_guard import MaxBodySizeMiddleware
from blastjob.web.urls import safe_external_url

# Runs the global status banner tracks; per-job runs (tailor/prep/research)
# stream on their own page instead.
BANNER_RUNS = ("discovery", "matching", mj.MANUAL_SCORE_RUN)

# LinkedIn's FULL data archive (the one that carries recommendations and message
# history, not just Connections.csv) routinely runs to tens of megabytes. The old
# 10MB cap silently rejected exactly the file this feature is for. Streamed to a
# temp file in chunks rather than buffered whole.
from blastjob.web.upload_guard import MAX_NETWORK_UPLOAD_BYTES  # noqa: E402  (re-exported)

_UPLOAD_CHUNK = 1024 * 1024
# ZIP local-file-header magic, for deciding the format by content.
_ZIP_MAGIC = b"PK\x03\x04"

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
# Every href fed by a discovery feed or by model output goes through this filter.
# Jinja escapes quotes but not URL schemes, and base.html carries the CSRF token
# on <body>, so a javascript: href would be a full takeover on a LAN-bound page.
templates.env.filters["safe_url"] = safe_external_url
# One rule for "we only hold a snippet", shared by the page and the tailor build.
templates.env.globals["is_jd_snippet"] = dbc.is_jd_snippet
# Employer-JD lookup (web/employer_jd.py): provenance label + whether the button may show.
templates.env.globals["jd_provenance"] = web_employer_jd.jd_provenance
templates.env.globals["jd_provenance_url"] = web_employer_jd.jd_provenance_url
templates.env.globals["ejd_refusal"] = web_employer_jd.refusal
# Row actions depend on the stage a job is in (one rule, web/stages.py).
templates.env.globals["stage_of"] = st.stage_of
templates.env.globals["next_stage"] = st.next_stage

LoadCfg = Callable[[], BlastJobConfig]
SaveCfg = Callable[[BlastJobConfig], None]


log = logging.getLogger(__name__)

# The nav's main row, in workflow order: (stage key, label, href).
STAGE_NAV: tuple[tuple[str, str, str], ...] = tuple(
    (stage, st.LABELS[stage], st.ROUTES[stage]) for stage in st.STAGES
)
# One-line page titles per list view.
VIEW_TITLES: dict[str, str] = {
    st.SURFACED: "Surfaced · cleared the bar",
    st.QUEUED: "Queue · borderline fit, or the location needs a look",
    "matches": "Matches · every undecided scored role, ranked",
    st.SHORTLIST: "Shortlist · roles you're weighing or tailoring",
    st.APPLIED: "Applied · in a hiring process",
    st.CLOSED: "Closed · finished",
}


def render(request: Request, name: str, **extra):
    """Starlette's current signature is TemplateResponse(request, name, context).

    Full pages (not `_partials`) get the nav's stage counts, from the same list
    functions the stage pages render, unless the caller already has them."""
    ctx = {
        "token": LAUNCH_TOKEN,
        "csrf_header": CSRF_HEADER,
        "archive_reasons": ARCHIVE_REASON_LABELS,
        "stage_nav": STAGE_NAV,
        **extra,
    }
    nav_counts = getattr(request.app.state, "nav_counts", None)
    if "counts" not in ctx and not name.startswith("_") and nav_counts is not None:
        ctx["counts"] = nav_counts()
    configured = getattr(request.app.state, "profile_configured", None)
    if not name.startswith("_") and configured is not None:
        ctx.setdefault("first_run", not configured())
    return templates.TemplateResponse(request, name, ctx)


def create_app(
    load_config: LoadCfg = cfg_mod.load_config,
    save_config: SaveCfg = cfg_mod.save_config,
) -> FastAPI:
    injected = load_config

    def _installing_load() -> BlastJobConfig:
        """Every load installs the runtime settings (aliases, agencies, ...),
        whatever loader was injected."""
        return cfg_mod.apply_runtime_settings(injected())

    load_config = _installing_load

    app = FastAPI(title="blastjob cockpit")
    # Outermost: require_csrf's `await request.form()` spools the whole body
    # before any route runs, so an in-route size check can never fire first.
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=MAX_NETWORK_UPLOAD_BYTES)
    # Added last, so it runs first: refuse a non-local Host (DNS rebinding) and a
    # foreign-Origin write before anything else, including the size check.
    app.add_middleware(LocalRequestGuard)
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    def conn() -> sqlite3.Connection:
        return dbc.init_db(load_config())

    def _nav_counts() -> dict[str, int]:
        c = conn()
        try:
            return sl.counts(c)
        finally:
            c.close()

    app.state.nav_counts = _nav_counts
    app.state.profile_configured = lambda: load_config().candidate.is_configured()

    def _radius() -> float:
        return load_config().discovery.commute_radius_miles

    def _geo() -> DiscoveryConfig:
        return load_config().discovery

    async def _discovery_work(run: Run):
        # Discover THEN score in one action — fetching without scoring leaves new
        # jobs invisible, which is pointless friction. "Match" stays separate for
        # re-scoring existing jobs after tuning the threshold/rubric.
        from blastjob.discovery.pipeline import run_discovery
        from blastjob.matching.pipeline import run_matching

        cfg = load_config()
        c = dbc.init_db(cfg)
        try:
            run.emit("log", "Discovering jobs from configured sources...")
            dres = await run_discovery(cfg, c)
            run.emit(
                "log",
                f"Fetched {dres.fetched}, saved {dres.inserted_or_updated}, "
                f"excluded {dres.skipped_excluded}, stale {dres.stale_marked}.",
            )
            if dres.errors:
                run.emit("log", f"Source errors: {dres.errors}")

            run.emit("log", "Scoring new jobs...")

            def prog(i: int, n: int) -> None:
                run.emit("progress", progress.format_payload(i, n, label="Scoring batch"))

            mres = await run_matching(cfg, c, CostTracker(), on_progress=prog, only_unscored=True)
            run.emit(
                "log",
                f"Done: {mres.surfaced} surfaced, {mres.queued} queued "
                f"(scored {mres.scored} of {dres.fetched} fetched).",
            )
            for note in mres.notes:
                run.emit("log", note)
            if mres.scoring_failures:
                run.emit(
                    "log",
                    f"⚠ {mres.scoring_failures} scoring batch(es) failed — those jobs "
                    "stay unscored and are retried next run (see blastjob.log).",
                )
            return {"fetched": dres.fetched, "surfaced": mres.surfaced, "queued": mres.queued}
        finally:
            c.close()

    async def _matching_work(run: Run):
        from blastjob.matching.pipeline import run_matching

        cfg = load_config()
        c = dbc.init_db(cfg)
        try:
            run.emit("log", "Scoring candidates (geo + prefilter + LLM)...")

            def prog(i: int, n: int) -> None:
                run.emit("progress", progress.format_payload(i, n, label="Scoring batch"))

            res = await run_matching(cfg, c, CostTracker(), on_progress=prog)
            run.emit(
                "log",
                f"Considered {res.considered}, scored {res.scored} -> surfaced "
                f"{res.surfaced}, queued {res.queued}, rejected {res.rejected} "
                f"(prefiltered {res.prefiltered_out}, geo-rejected {res.geo_rejected}, "
                f"location unclear {res.geo_unknown}).",
            )
            for note in res.notes:
                run.emit("log", note)
            if res.scoring_failures:
                run.emit(
                    "log",
                    f"⚠ {res.scoring_failures} scoring batch(es) failed — those jobs "
                    "stay unscored and are retried next run (see blastjob.log).",
                )
            return {"surfaced": res.surfaced, "queued": res.queued}
        finally:
            c.close()

    deps = Deps(
        conn=conn,
        load_config=load_config,
        render=render,
        nav_counts=_nav_counts,
        radius=_radius,
    )

    srv.register(app, conn, _radius, render, _geo, load_config)
    mj.register(app, conn, load_config, render)
    routes_stages.register(app, deps)
    routes_network.register(app, deps)
    routes_packets.register(app, deps)
    routes_settings.register(app, deps, save_config)
    routes_outcomes.register(app, deps)
    employer_link.register(app, deps)
    web_employer_jd.register(app, deps)
    routes_activity.register(app, deps)

    def _packet_role_slug(packet, title: str) -> str | None:
        return vh.packet_role_slug(packet, title)

    def _list_page(request: Request, view: str, rows, c, sort: str = "fit") -> HTMLResponse:
        return vh.list_page(deps, request, view, rows, c, sort)

    def _render_app_status(
        request: Request, c: sqlite3.Connection, job_id: int, send_note: str = ""
    ) -> HTMLResponse:
        return vh.render_app_status(deps, request, c, job_id, send_note)

    def _stage_page(request: Request, view: str, sort: str = "fit") -> HTMLResponse:
        return vh.stage_page(deps, request, view, sort)

    def _redirect(request: Request, path: str) -> RedirectResponse:
        return vh.redirect(request, path)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail(
        request: Request, job_id: int, source_view: str = "", url_dropped: str = ""
    ):
        c = conn()
        try:
            row = service.job_detail(c, job_id)
            if row is None:
                return HTMLResponse("Not found", status_code=404)
            followup_reason = sl.followup_reason(row)
            warm_paths = net.warm_paths(c, job_id)
            # annotate_network_one returns a dict (the row plus network badges),
            # so it needs its own name rather than rebinding the sqlite3.Row.
            job = service.annotate_network_one(c, row)
            assert job is not None
            cfg = load_config()
            packet = packets.packet_for_job(c, job_id, load_config=load_config)
            return render(
                request,
                "detail.html",
                job=job,
                followup_reason=followup_reason,
                contacts=people.contacts_for_job(c, job_id),
                profile=co.get_profile(c, job["company"]),
                signals=co.comp_signals(c, job_id=job_id),
                floor=co.negotiated_floor(c, job_id),
                comp_kinds=co.COMP_KINDS,
                warm_paths=warm_paths,
                warm_people=nv.warm_paths_for_job(c, job_id),
                packet=packet,
                groundedness=fitscore.load_fit_score(
                    packet["dir"] if packet else None,
                    role_slug=_packet_role_slug(packet, job["title"]),
                ),
                packet_report=packet_view.load_packet_report(
                    packet["dir"] if packet else None,
                    role_slug=_packet_role_slug(packet, job["title"]),
                ),
                is_local=packets.is_local_request(request),
                back=service.back_href(source_view),
                url_dropped=bool(url_dropped),
                stage=st.stage_of(row),
                next_state=st.next_stage(row["application_state"] or ""),
                threshold=cfg.discovery.alignment_threshold,
                **srv.detail_context(c, row, cfg.discovery.commute_radius_miles, cfg.discovery),
            )
        finally:
            c.close()

    @app.post("/jobs/{job_id}/keep", response_class=HTMLResponse)
    async def keep(
        request: Request,
        job_id: int,
        from_list: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        c = conn()
        try:
            moved = service.set_review_state(
                c,
                job_id,
                ReviewState.KEPT,
                [ReviewState.NEW, ReviewState.SURFACED, ReviewState.QUEUED],
            )
            return vh.row_response(deps, request, c, job_id, moved=moved, from_list=from_list)
        finally:
            c.close()

    @app.post("/jobs/{job_id}/stage/{action}", response_class=HTMLResponse)
    async def stage_action(
        request: Request,
        job_id: int,
        action: str,
        _: None = Depends(require_csrf),
    ):
        """List-row stage moves: apply, advance, withdraw, reopen. Each is a
        CAS on the job's current stage; the row leaves its list."""
        if action not in sm.ACTIONS:
            return HTMLResponse("Invalid action", status_code=400)
        c = conn()
        try:
            moved = sm.run_action(c, job_id, action)
            if moved and action == "advance":
                # Advance keeps the row on Applied (only its stage changes), so
                # the empty-200 outerHTML swap would delete a row that is still
                # listed. Reload to show the new stage.
                return HTMLResponse("", headers={"HX-Refresh": "true"})
            return vh.row_response(deps, request, c, job_id, moved=moved, from_list="1")
        finally:
            c.close()

    @app.post("/jobs/{job_id}/archive", response_class=HTMLResponse)
    async def archive(
        request: Request,
        job_id: int,
        reason: str = Form(""),
        note: str = Form(""),
        from_list: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        if reason and reason not in ARCHIVE_REASONS:
            return HTMLResponse("Invalid reason", status_code=400)
        c = conn()
        try:
            moved = service.archive_job(
                c,
                job_id,
                [ReviewState.NEW, ReviewState.SURFACED, ReviewState.QUEUED],
                reason=reason,
                note=note,
            )
            return vh.row_response(deps, request, c, job_id, moved=moved, from_list=from_list)
        finally:
            c.close()

    def _applied_at(c: sqlite3.Connection, job_id: int) -> str:
        row = c.execute("SELECT applied_at FROM jobs WHERE id=?", (job_id,)).fetchone()
        return ((row["applied_at"] if row else "") or "").strip()

    @app.post("/jobs/{job_id}/status", response_class=HTMLResponse)
    async def set_status(
        request: Request,
        job_id: int,
        status: str = Form(...),
        _: None = Depends(require_csrf),
    ):
        # posting_closed/filled belong to the closure lifecycle, which records
        # the prior state a reopen restores; a bare status write would not.
        if status not in STATUSES or status in closure.AUTOMATION_STATES:
            return HTMLResponse("Invalid status", status_code=400)
        c = conn()
        try:
            # Permissive expected set: any current application_state is allowed to
            # move (users correct mistakes); the CAS still guards review_state='kept'.
            first_send = status == "applied" and not _applied_at(c, job_id)
            moved = service.set_application_state(c, job_id, status, ["", *STATUSES])
            send_note = ""
            if moved:
                # This dropdown is BOTH ways a job reaches "applied" (the other
                # is the Shortlist row) and the path that records a rejection or
                # a ghosting. Until 2026-09-16 it recorded the outcome but never
                # the send, so as-sent attribution never fired from the job page
                # and outcomes attached to a send that did not exist.
                from blastjob.core import packet_sends as ps

                # Only the move that first stamps applied_at is a send; a move
                # back into applied (screen -> applied) is the same attempt.
                if first_send:
                    send_note = sm.record_send_for_job(c, job_id)
                try:
                    ps.record_outcome_for_job(c, job_id, status)
                except Exception:  # noqa: BLE001 — a missing send is not an error
                    pass
            return _render_app_status(request, c, job_id, send_note=send_note)
        finally:
            c.close()

    @app.post("/jobs/{job_id}/packet/release", response_class=HTMLResponse)
    async def release_packet(
        request: Request,
        job_id: int,
        reason: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        """Send a quarantined packet anyway, on the record.

        The gate can be wrong — it fires on mechanical checks, and the user knows
        things it does not. So the override exists, it needs a stated reason,
        and `RELEASED.txt` keeps every one of them. Without this route the only
        way out of quarantine would be moving files by hand, which is the
        unaudited bypass quarantine exists to prevent.
        """
        from blastjob.core import quarantine as qz
        from blastjob.core.refine import locate as locate_resume

        stated = reason.strip()
        if not stated:
            return HTMLResponse("Say why you're sending it anyway.", status_code=400)
        c = conn()
        try:
            # Read the recorded artifact_dir; never recompute a packet path from
            # today's slugs — two output layouts exist on disk.
            packet = packets.packet_for_job(c, job_id, load_config=load_config)
            if not packet or not packet.get("dir"):
                return HTMLResponse("No packet for this job", status_code=404)
            target = packets.safe_output_path(load_config(), packet["dir"])
            if target is None or not target.exists():
                return HTMLResponse("No packet folder", status_code=404)
            row = service.job_detail(c, job_id)
            title = row["title"] if row is not None else ""
            # The folder is shared by every role tailored for this company that
            # day: release only this job's documents, or another role's held
            # files come back public under a report that still says held.
            try:
                found = locate_resume(target, row["company"] if row else "", title)
            except FileNotFoundError:
                return HTMLResponse("No resume for this role in the packet", status_code=404)
            # A legacy per-role folder (no slug) belongs to this role whole.
            stems = [found.stem, found.cover_stem] if found.slug else None
            moved = qz.release(target, reason=stated, stems=stems)
            if not moved:
                return HTMLResponse("Nothing is quarantined", status_code=400)
            # The folder and the sidecar must agree. Without this the documents
            # come back but the report still says "quarantined", so the page
            # keeps showing the held badge and offers Release again.
            if not packet_view.record_override(
                packet["dir"], reason=stated, role_slug=_packet_role_slug(packet, title) or ""
            ):
                # Public files under a report that still says "quarantined" would be
                # refused by every download with no explanation. Put them back and say so.
                log.warning("released job %s but could not record the override", job_id)
                stuck = qz.undo_release(target, moved, why="override could not be recorded")
                suffix = (
                    f" {len(stuck)} file(s) could not be re-held and are still in the folder."
                    if stuck
                    else " Nothing was released; try again."
                )
                return HTMLResponse(
                    "The release could not be recorded in the packet report." + suffix,
                    status_code=500,
                )
            return HTMLResponse("", headers={"HX-Refresh": "true"})
        finally:
            c.close()

    @app.post("/jobs/{job_id}/posting/{verdict}", response_class=HTMLResponse)
    async def set_posting_verdict(
        request: Request,
        job_id: int,
        verdict: str,
        _: None = Depends(require_csrf),
    ):
        """The user's manual "Posting closed" / "Still open". Always applies,
        whatever closure_auto says, and automation never overturns it."""
        if verdict not in ("closed", "open"):
            return HTMLResponse("Invalid verdict", status_code=400)
        c = conn()
        try:
            ok = closure.set_manual_verdict(
                c, job_id, is_open=verdict == "open", now=datetime.now()
            )
            job = service.job_detail(c, job_id) if ok else None
            if job is None:
                return HTMLResponse("Not found", status_code=404)
            reason = sl.followup_reason(job)
            # The verdict can move application_state, so the status panel is
            # refreshed out-of-band alongside the badge.
            return render(
                request, "_posting_status.html", job=job, followup_reason=reason, oob=True
            )
        finally:
            c.close()

    @app.post("/jobs/{job_id}/notes", response_class=HTMLResponse)
    async def set_notes(
        request: Request,
        job_id: int,
        notes: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        c = conn()
        try:
            service.set_app_notes(c, job_id, notes)
            return _render_app_status(request, c, job_id)
        finally:
            c.close()

    @app.post("/jobs/{job_id}/followup", response_class=HTMLResponse)
    async def set_followup(
        request: Request,
        job_id: int,
        next_action_due: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        c = conn()
        try:
            service.set_next_action_due(c, job_id, next_action_due)
            return _render_app_status(request, c, job_id)
        finally:
            c.close()

    @app.get("/pipeline", response_class=HTMLResponse)
    async def pipeline(request: Request):
        c = conn()
        try:
            # One computation of the five stage lists, shared by the board and
            # the header counts -- so the counts are literally the rows the page
            # renders, not a second opinion about them.
            lists = sl.stage_list_cache(c)
            return render(
                request,
                "pipeline.html",
                board=sl.pipeline_board(c, lists=lists),
                counts=sl.counts(c, lists=lists),
            )
        finally:
            c.close()

    @app.post("/run/discovery", response_class=HTMLResponse)
    async def run_discovery_route(request: Request, _: None = Depends(require_csrf)):
        run = RUNTIME.start("discovery", _discovery_work)
        return render(request, "_run_status.html", run=run)

    @app.post("/run/matching", response_class=HTMLResponse)
    async def run_matching_route(request: Request, _: None = Depends(require_csrf)):
        run = RUNTIME.start("matching", _matching_work)
        return render(request, "_run_status.html", run=run)

    @app.get("/runs/active", response_class=HTMLResponse)
    async def run_active(request: Request):
        """Global status banner. Every page asks for this on load so a
        discovery/matching run started on one page stays visible after
        navigating to another (the work never depended on the tab; only the
        banner did)."""
        run = RUNTIME.active(BANNER_RUNS)
        if run is None:
            return HTMLResponse("")
        return render(request, "_run_status.html", run=run)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_status(request: Request, run_id: str):
        run = RUNTIME.get(run_id)
        if run is None:
            return HTMLResponse("", status_code=404)
        return render(request, "_run_status.html", run=run)

    @app.post("/companies/unexclude", response_class=HTMLResponse)
    async def company_unexclude(
        request: Request, company: str = Form(""), _: None = Depends(require_csrf)
    ):
        """Lift a company-wide exclusion. Deliberately does NOT un-archive the
        postings already archived under it — those are separate decisions, each
        reversible on its own row."""
        c = conn()
        try:
            service.unexclude_company(c, company)
            return render(request, "_exclusions.html", exclusions=service.excluded_companies(c))
        finally:
            c.close()

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app

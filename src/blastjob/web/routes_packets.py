"""Everything about a tailored packet: viewing it, rebuilding it, refining it,
serving its files, and the long-running jobs that produce it. Also the funnel
and history views, which read the same packet folders, and the SSE endpoint
every build streams through.

Split out of `web/app.py` (2026-09-17).

`build_runs.artifact_dir` is the only source for where a packet lives -- two
output layouts exist on disk and a path must never be recomputed from today's
slugs. That shared rule is why these routes belong together.
"""

from __future__ import annotations

import subprocess

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from blastjob import config as cfg_mod
from blastjob.core.artifacts import stored_path
from blastjob.core.history import scan_history
from blastjob.db import client as dbc
from blastjob.llm.child_env import child_env
from blastjob.web import fitscore, packet_view, packets, service
from blastjob.web import funnel as fn
from blastjob.web import stage_moves as sm
from blastjob.web import view_helpers as vh
from blastjob.web.deps import Deps
from blastjob.web.downloads import resolve_download
from blastjob.web.runtime import RUNTIME
from blastjob.web.security import require_csrf


def register(app: FastAPI, deps: Deps) -> None:
    @app.get("/jobs/{job_id}/packet", response_class=HTMLResponse)
    async def job_packet(request: Request, job_id: int):
        """Where the tailored bundle actually landed. The path has always been in
        build_runs.artifact_dir; nothing ever read it back, and the SSE `done`
        handler overwrote the one log line that mentioned it."""
        c = deps.conn()
        try:
            packet = packets.packet_for_job(c, job_id, load_config=deps.load_config)
            row = service.job_detail(c, job_id)
            title = row["title"] if row is not None else ""
            return deps.render(
                request,
                "_packet.html",
                job_id=job_id,
                packet=packet,
                groundedness=fitscore.load_fit_score(
                    packet["dir"] if packet else None,
                    role_slug=vh.packet_role_slug(packet, title),
                ),
                packet_report=packet_view.load_packet_report(
                    packet["dir"] if packet else None,
                    role_slug=vh.packet_role_slug(packet, title),
                ),
                is_local=packets.is_local_request(request),
            )
        finally:
            c.close()

    @app.post("/jobs/{job_id}/reveal", response_class=HTMLResponse)
    async def job_reveal(request: Request, job_id: int, _: None = Depends(require_csrf)):
        """Open the packet folder in Finder. Only from the computer running
        blastjob: the cockpit may be bound to the LAN and the Origin check accepts
        RFC1918, so the peer address is the only thing that can tell this machine
        from another device."""
        if not packets.is_local_request(request):
            return HTMLResponse(
                "Finder is only available from the computer running blastjob", status_code=403
            )
        c = deps.conn()
        try:
            cfg = deps.load_config()
            packet = packets.packet_for_job(c, job_id, load_config=deps.load_config)
            if packet is None:
                return HTMLResponse("No packet folder to open", status_code=404)
            reveal_row = service.job_detail(c, job_id)
            reveal_title = reveal_row["title"] if reveal_row is not None else ""
            target = packets.safe_output_path(cfg, packet["dir"])
            if target is None or not target.is_dir():
                return HTMLResponse("No packet folder to open", status_code=404)
            subprocess.run(  # noqa: S603
                ["open", str(target)],  # noqa: S607
                check=False,
                env=child_env(),
            )
            return deps.render(
                request,
                "_packet.html",
                job_id=job_id,
                packet=packet,
                groundedness=fitscore.load_fit_score(
                    packet["dir"], role_slug=vh.packet_role_slug(packet, reveal_title)
                ),
                packet_report=packet_view.load_packet_report(
                    packet["dir"], role_slug=vh.packet_role_slug(packet, reveal_title)
                ),
                is_local=True,
            )
        finally:
            c.close()

    @app.post("/jobs/{job_id}/retailor", response_class=HTMLResponse)
    async def job_retailor(request: Request, job_id: int, _: None = Depends(require_csrf)):
        """Re-tailor after the work history changed. build_state is never reset
        anywhere else, so without this claim_for_build can never re-claim a job
        it already finished."""
        from blastjob.web.build import make_tailor_work

        c = deps.conn()
        try:
            job = service.job_detail(c, job_id)
            # On a snippet the build refuses, so resetting first would leave a
            # packet on disk that the job no longer admits to having.
            if job is not None and not dbc.is_jd_snippet(job["raw_jd"], job["jd_pasted_at"]):
                dbc.reset_build_state(c, job_id)
        finally:
            c.close()
        run = RUNTIME.start("tailor", make_tailor_work(job_id, deps.load_config))
        return deps.render(
            request, "_build_panel.html", run=run, heading="Re-tailoring", job_id=job_id
        )

    @app.post("/jobs/{job_id}/jd", response_class=HTMLResponse)
    async def job_paste_jd(
        request: Request,
        job_id: int,
        jd: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        """Store the full description the user pasted over a truncated snippet,
        then re-score it against the new text (issue #4)."""
        from blastjob.web.paste_rescore import start_rescore

        c = deps.conn()
        try:
            changed = dbc.pasted_jd_changes(c, job_id, jd)
            saved = dbc.set_pasted_jd(c, job_id, jd)
            job = service.job_detail(c, job_id)
            if job is None:
                return HTMLResponse("Not found", status_code=404)
            if saved and changed:
                start_rescore(deps.load_config, job_id)
            error = "" if saved else "Nothing to save: the pasted description was empty."
            resp = deps.render(request, "_jd_block.html", job=job, jd_error=error)
            if saved and changed:
                # base.html's #run-status listens for this and re-reads
                # /runs/active, so the score-job run shows in the banner.
                resp.headers["HX-Trigger"] = "refresh-run-status"
            return resp
        finally:
            c.close()

    @app.post("/jobs/{job_id}/unkeep", response_class=HTMLResponse)
    async def job_unkeep(
        request: Request,
        job_id: int,
        from_list: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        """Remove from the Shortlist (refused once applied or closed)."""
        c = deps.conn()
        try:
            moved = sm.unkeep(c, job_id, deps.load_config().discovery.alignment_threshold)
            if moved:
                dbc.reset_build_state(c, job_id)
                sm.file_after_move(c, job_id, deps.load_config)
            return vh.row_response(deps, request, c, job_id, moved=moved, from_list=from_list)
        finally:
            c.close()

    @app.post("/jobs/{job_id}/unarchive", response_class=HTMLResponse)
    async def job_unarchive(
        request: Request,
        job_id: int,
        from_list: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        c = deps.conn()
        try:
            moved = service.unarchive_job(c, job_id)
            if moved:
                sm.file_after_move(c, job_id, deps.load_config)
            return vh.row_response(deps, request, c, job_id, moved=moved, from_list=from_list)
        finally:
            c.close()

    @app.post("/jobs/{job_id}/followup/draft", response_class=HTMLResponse)
    async def job_followup_draft(request: Request, job_id: int, _: None = Depends(require_csrf)):
        """NOTE the path: POST /jobs/{id}/followup is already taken (it sets the
        reminder date), so the draft lives one level deeper."""
        from blastjob.web.followup import make_followup_work

        c = deps.conn()
        try:
            packet = packets.packet_for_job(c, job_id, load_config=deps.load_config)
        finally:
            c.close()
        run = RUNTIME.start(
            "followup",
            make_followup_work(job_id, deps.load_config, artifact_dir=(packet or {}).get("dir")),
            key=str(job_id),
        )
        return deps.render(request, "_followup_panel.html", run=run, heading="Follow-up draft")

    @app.get("/builds/{run_id}/followup-link", response_class=HTMLResponse)
    async def followup_link(request: Request, run_id: str):
        run = RUNTIME.get(run_id)
        return deps.render(
            request, "_followup_link.html", result=(getattr(run, "result", None) or {})
        )

    @app.get("/funnel", response_class=HTMLResponse)
    async def funnel_page(request: Request):
        """Where the pipeline leaks. See web/funnel.py for why this exists."""
        c = deps.conn()
        try:
            cfg = deps.load_config()
            return deps.render(
                request,
                "funnel.html",
                funnel=fn.build_funnel(c, cfg),
                dead=fn.dead_boards(c, cfg),
                silent=fn.silent_channels(c, cfg),
                stages=fn.STAGES,
                drops=("stale", "geo_rejected", "prefiltered", "below_band", "comp_rejected"),
            )
        finally:
            c.close()

    @app.get("/history", response_class=HTMLResponse)
    async def history_page(request: Request):
        """Every packet ever built. Reachable at last: scan_history matched a
        filename no build had written since the output layout changed."""
        cfg = deps.load_config()
        out_dir = cfg_mod.output_dir(cfg)
        # rglob: this also finds packets filed into the bucket folders.
        entries = scan_history(out_dir)
        entries.sort(key=lambda e: e.timestamp or "", reverse=True)
        c = deps.conn()
        try:
            job_ids = packets.history_job_ids(c, entries, output_root=out_dir)
            # Reveal opens a job's LATEST packet, so offer it only on that row.
            latest = {
                jid: dirs[0]
                for jid in set(job_ids.values())
                if (dirs := packets.job_packet_dirs(c, jid))
            }
        finally:
            c.close()
        rows = []
        for e in entries:
            jid = job_ids.get((e.path, e.role))
            latest_dir = latest.get(jid) if jid is not None else None
            rows.append(
                {
                    "entry": e,
                    "job_id": jid,
                    "docs": packets.role_documents(packets.packet_files(e.path), e.role),
                    # Root form on both sides: the folder may have been filed.
                    "is_latest": bool(latest_dir)
                    and stored_path(latest_dir or "", out_dir) == stored_path(e.path, out_dir),
                }
            )
        return deps.render(
            request, "history.html", rows=rows, is_local=packets.is_local_request(request)
        )

    @app.get("/jobs/{job_id}/files/{name}")
    async def job_packet_file(job_id: int, name: str):
        """Open one document from a job's packet in the browser -- the only way to
        reach it from another device, where Reveal in Finder cannot work."""
        c = deps.conn()
        try:
            # One resolver for this route and the job page's Files list, so a
            # link is shown only for the file this route would really serve.
            target = resolve_download(c, deps.load_config(), job_id, name)
        finally:
            c.close()
        if target is None:
            return HTMLResponse("Not found", status_code=404)
        if target.suffix in (".md", ".txt", ".json"):
            return FileResponse(target, media_type="text/plain; charset=utf-8")
        return FileResponse(target, filename=target.name)

    @app.post("/jobs/{job_id}/refine", response_class=HTMLResponse)
    async def job_refine(
        request: Request,
        job_id: int,
        feedback: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        """A corrective revision rather than a full re-tailor — the action that
        answers the groundedness panel's list of unsupported claims."""
        from blastjob.web.refine import make_refine_work

        if not feedback.strip():
            return HTMLResponse("Say what to change first.", status_code=400)
        c = deps.conn()
        try:
            packet = packets.packet_for_job(c, job_id, load_config=deps.load_config)
        finally:
            c.close()
        run = RUNTIME.start(
            "refine",
            make_refine_work(
                (packet or {}).get("dir"), feedback.strip(), deps.load_config, job_id=job_id
            ),
        )
        return deps.render(request, "_build_panel.html", run=run, heading="Revising", job_id=job_id)

    @app.post("/jobs/{job_id}/tailor", response_class=HTMLResponse)
    async def tailor(request: Request, job_id: int, _: None = Depends(require_csrf)):
        from blastjob.web.build import make_tailor_work

        run = RUNTIME.start("tailor", make_tailor_work(job_id, deps.load_config))
        return deps.render(
            request, "_build_panel.html", run=run, heading="Tailoring", job_id=job_id
        )

    @app.post("/jobs/{job_id}/outreach", response_class=HTMLResponse)
    async def outreach(
        request: Request,
        job_id: int,
        contact_id: int | None = Form(None),
        _: None = Depends(require_csrf),
    ):
        from blastjob.web.build import make_outreach_work

        run = RUNTIME.start(
            "outreach",
            make_outreach_work(job_id, deps.load_config, contact_id=contact_id),
            key=f"{job_id}:{contact_id or ''}",
        )
        heading = "Outreach draft" + (" (addressed)" if contact_id else "")
        return deps.render(request, "_build_panel.html", run=run, heading=heading)

    @app.post("/jobs/{job_id}/prep", response_class=HTMLResponse)
    async def prep_route(request: Request, job_id: int, _: None = Depends(require_csrf)):
        from blastjob.web.prep import make_prep_work

        run = RUNTIME.start("prep", make_prep_work(job_id, deps.load_config), key=str(job_id))
        return deps.render(request, "_build_panel.html", run=run, heading="Interview prep")

    # ----- people / addressed outreach -----

    @app.get("/builds/{run_id}/events")
    async def build_events(request: Request, run_id: str):
        from blastjob.web.build import sse_format

        try:
            last_id = max(int(request.headers.get("last-event-id") or 0), 0)
        except ValueError:
            last_id = 0  # a garbage header replays from the start, never a 500

        async def gen():
            async for ev in RUNTIME.stream(run_id, last_id):
                yield sse_format(ev)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

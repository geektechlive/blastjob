"""Network desks, contacts, company profiles and their research actions.

Split out of `web/app.py` (2026-09-17). These belonged together already: the
companies routes were physically separated from their own write routes by the
network-import block, which is the kind of drift a 1,300-line module produces.

Every handler is the one that was in `create_app`, moved verbatim apart from
taking its dependencies from `Deps` instead of a closure.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from blastjob import config as cfg_mod
from blastjob.core import company as co
from blastjob.core import network as net
from blastjob.core import network_views as nv
from blastjob.core import target_store as ts
from blastjob.core.paths import locate
from blastjob.discovery import follow_seeds as fs
from blastjob.web import people, recruiter_view, service
from blastjob.web.asks_view import ask_context
from blastjob.web.deps import Deps
from blastjob.web.runtime import RUNTIME
from blastjob.web.security import require_csrf
from blastjob.web.upload_guard import MAX_NETWORK_UPLOAD_BYTES, parse_content_length

log = logging.getLogger(__name__)

# "$180,000", "180000", "180k", "$1.5m": what a person types for a salary.
_MONEY_RE = re.compile(r"^\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([km]?)$", re.I)
_MONEY_SCALE = {"": 1, "k": 1_000, "m": 1_000_000}


def parse_usd(text: str) -> int | None:
    """A typed dollar amount as whole dollars; None for blank; ValueError for
    anything that is not a plain amount (a wrong salary is worse than none)."""
    raw = text.strip()
    if not raw:
        return None
    m = _MONEY_RE.match(raw)
    if m is None:
        raise ValueError(raw)
    return round(float(m.group(1).replace(",", "")) * _MONEY_SCALE[m.group(2).lower()])


# LinkedIn's FULL data archive routinely runs to tens of megabytes; streamed to
# a temp file in chunks rather than buffered whole.
_UPLOAD_CHUNK = 1024 * 1024
_ZIP_MAGIC = b"PK\x03\x04"


def _render_contacts(deps: Deps, request, c: sqlite3.Connection, job_id: int, error: str = ""):
    return deps.render(
        request,
        "_contacts.html",
        job=service.job_detail(c, job_id),
        contacts=people.contacts_for_job(c, job_id),
        people_error=error,
        **ask_context(c, job_id),
    )


def register(app: FastAPI, deps: Deps) -> None:
    @app.get("/network/follows", response_class=HTMLResponse)
    async def network_follows(request: Request):
        """Followed companies the pipeline has never seen. Read-only: it presents
        candidates, it never writes a board token to config."""
        c = deps.conn()
        try:
            seeds = fs.unresolved_follows(c, deps.load_config())
            return deps.render(request, "_follows.html", seeds=seeds, summary=fs.summary(seeds))
        finally:
            c.close()

    @app.post("/network/enrich", response_class=HTMLResponse)
    async def network_enrich(request: Request, _: None = Depends(require_csrf)):
        """The user's "full agentic pass" over the import. Sends DISTINCT title and
        company strings only -- never a name -- and is resumable, so a re-import
        classifies just the delta."""
        from blastjob.web.enrich import make_enrich_work

        run = RUNTIME.start("enrich", make_enrich_work(deps.load_config), key="network")
        return deps.render(request, "_build_panel.html", run=run, heading="Reading titles")

    @app.get("/network/recruiters", response_class=HTMLResponse)
    async def network_recruiters(request: Request):
        c = deps.conn()
        try:
            return deps.render(request, "_recruiters.html", **recruiter_view.desk_context(c))
        finally:
            c.close()

    @app.get("/network/contacts", response_class=HTMLResponse)
    async def network_contact_search(request: Request, q: str = ""):
        """Find a connection that is not on the recruiter desk, to label it."""
        c = deps.conn()
        try:
            query = q.strip()[:80]
            return deps.render(
                request,
                "_contact_search.html",
                q=query,
                results=nv.search_contacts(c, query),
            )
        finally:
            c.close()

    @app.post("/network/contacts/{contact_id}/recruiter", response_class=HTMLResponse)
    async def network_contact_recruiter(
        request: Request,
        contact_id: int,
        value: str = Form(...),
        ctx: str = Form("desk"),
        q: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        """Set (or clear, with "auto") the user's own recruiter verdict. It wins over
        the title rule and the enrichment label (issue #72)."""
        if value != "auto" and value not in nv.RECRUITER_OVERRIDES:
            return HTMLResponse("Invalid value", status_code=400)
        if ctx not in ("desk", "search"):
            return HTMLResponse("Invalid context", status_code=400)
        c = deps.conn()
        try:
            if not nv.set_recruiter_override(c, contact_id, value):
                return HTMLResponse("Not found", status_code=404)
            if ctx == "search":
                query = q.strip()[:80]
                # The desk is refreshed out of band so it never shows a stale label.
                return deps.render(
                    request,
                    "_contact_search.html",
                    q=query,
                    results=nv.search_contacts(c, query),
                    oob=True,
                    **recruiter_view.desk_context(c),
                )
            return deps.render(request, "_recruiters.html", **recruiter_view.desk_context(c))
        finally:
            c.close()

    @app.get("/network/dormant", response_class=HTMLResponse)
    async def network_dormant(request: Request):
        c = deps.conn()
        try:
            return deps.render(request, "_dormant.html", ties=nv.dormant_ties(c))
        finally:
            c.close()

    @app.get("/jobs/{job_id}/contacts", response_class=HTMLResponse)
    async def contacts_partial(request: Request, job_id: int):
        c = deps.conn()
        try:
            return _render_contacts(deps, request, c, job_id)
        finally:
            c.close()

    @app.post("/jobs/{job_id}/people", response_class=HTMLResponse)
    async def find_people_route(request: Request, job_id: int, _: None = Depends(require_csrf)):
        c = deps.conn()
        try:
            job = service.job_detail(c, job_id)
            if job is None:
                return HTMLResponse("Not found", status_code=404)
            error = ""
            try:
                # Inline model call: it takes a cockpit model slot like any run does.
                async with RUNTIME.slot():
                    found = await people.find_people(
                        deps.load_config(), job["company"], job["title"]
                    )
                if not found:
                    error = "Search returned no one clearly tied to this company."
                else:
                    people.add_contacts(c, job_id, found)
            except people.PeopleSearchUnavailable as e:
                error = str(e)
            except Exception as e:  # noqa: BLE001 — show it in the panel, never a 500
                log.warning("people search failed for job %s: %s", job_id, e)
                error = f"People search failed: {type(e).__name__}"
            return _render_contacts(deps, request, c, job_id, error)
        finally:
            c.close()

    # ----- companies: profiles, comp signals, automated research -----

    @app.get("/companies", response_class=HTMLResponse)
    async def companies(request: Request):
        c = deps.conn()
        try:
            return deps.render(
                request,
                "companies.html",
                profiles=co.list_profiles(c),
                exclusions=service.excluded_companies(c),
            )
        finally:
            c.close()

    @app.get("/companies/{key}", response_class=HTMLResponse)
    async def company_page(request: Request, key: str):
        c = deps.conn()
        try:
            profile = co.get_profile_by_key(c, key)
            if profile is None:
                return HTMLResponse("Not found", status_code=404)
            if profile.get("workup_path"):
                # Shown where it is NOW: its entry may have been filed (issue #20).
                out_dir = cfg_mod.output_dir(deps.load_config())
                profile = {**profile, "workup_path": str(locate(profile["workup_path"], out_dir))}
            return deps.render(
                request,
                "company.html",
                profile=profile,
                jobs=co.jobs_for_company(c, key),
                signals=co.comp_signals(c, company=profile["company"]),
                floor=None,
                job=None,
                comp_kinds=co.COMP_KINDS,
                is_target=ts.get_target(c, key) is not None,
            )
        finally:
            c.close()

    # ----- network: LinkedIn connections import, warm-path summary -----

    @app.get("/network", response_class=HTMLResponse)
    async def network_page(request: Request, error: str = ""):
        c = deps.conn()
        try:
            return deps.render(
                request,
                "network.html",
                summary=net.network_summary(c),
                recruiters=net.recruiters(c),
                advocates=net.advocates(c),
                error=error,
            )
        finally:
            c.close()

    @app.post("/network/import", response_class=HTMLResponse)
    async def network_import(
        request: Request,
        file: UploadFile = File(...),
        _: None = Depends(require_csrf),
    ):
        def _page(c, **kw):
            return deps.render(
                request,
                "network.html",
                summary=net.network_summary(c),
                recruiters=net.recruiters(c),
                advocates=net.advocates(c),
                **kw,
            )

        too_big = f"File too large ({MAX_NETWORK_UPLOAD_BYTES // (1024 * 1024)}MB max)."
        declared = parse_content_length(request.headers.get("content-length"))
        if declared is not None and declared > MAX_NETWORK_UPLOAD_BYTES:
            c = deps.conn()
            try:
                return _page(c, error=too_big)
            finally:
                c.close()

        # A full archive is a ZIP; the connections-only export is a bare CSV.
        # Decided by the bytes, not the filename: branching on `.zip` sent a ZIP
        # named `.csv` down the CSV path, where it failed with a decode error
        # that told the user nothing useful.
        # mkstemp hands back (fd, path) and OPENS the file; keeping only the path
        # leaked one descriptor per import for the life of a KeepAlive process.
        fd, tmp_name = tempfile.mkstemp()
        os.close(fd)
        tmp_path = Path(tmp_name)
        written = 0
        is_zip = False
        first_chunk = True
        c = deps.conn()
        try:
            try:
                with tmp_path.open("wb") as out:
                    while chunk := await file.read(_UPLOAD_CHUNK):
                        if first_chunk:
                            is_zip = chunk.startswith(_ZIP_MAGIC)
                            first_chunk = False
                        written += len(chunk)
                        if written > MAX_NETWORK_UPLOAD_BYTES:
                            return _page(c, error=too_big)
                        out.write(chunk)
                result = (
                    net.import_archive(c, tmp_path)
                    if is_zip
                    else net.import_connections_csv(c, tmp_path)
                )
            except (ValueError, zipfile.BadZipFile) as exc:
                return _page(c, error=str(exc) or "Could not read that file.")
            finally:
                tmp_path.unlink(missing_ok=True)
            return _page(c, import_result=result)
        finally:
            c.close()

    def _render_signals(request: Request, c: sqlite3.Connection, job_id: int, error: str = ""):
        return deps.render(
            request,
            "_comp_signals.html",
            job=service.job_detail(c, job_id),
            signals=co.comp_signals(c, job_id=job_id),
            floor=co.negotiated_floor(c, job_id),
            comp_kinds=co.COMP_KINDS,
            signal_error=error,
        )

    @app.post("/jobs/{job_id}/comp-signal", response_class=HTMLResponse)
    async def add_comp_signal(
        request: Request,
        job_id: int,
        kind: str = Form(...),
        low_usd: str = Form(""),
        high_usd: str = Form(""),
        source: str = Form(""),
        note: str = Form(""),
        observed_at: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        if kind not in co.COMP_KINDS:
            return HTMLResponse("Invalid kind", status_code=400)
        c = deps.conn()
        try:
            job = service.job_detail(c, job_id)
            if job is None:
                return HTMLResponse("Not found", status_code=404)
            try:
                low, high = parse_usd(low_usd), parse_usd(high_usd)
            except ValueError:
                return _render_signals(
                    request,
                    c,
                    job_id,
                    "Could not read that amount. Use digits, like 180000 or $180,000.",
                )
            co.add_comp_signal(
                c,
                company=job["company"],
                job_id=job_id,
                kind=kind,
                low_usd=low,
                high_usd=high,
                source=source[:120],
                note=note,
                observed_at=observed_at or None,
            )
            return _render_signals(request, c, job_id)
        finally:
            c.close()

    @app.post("/jobs/{job_id}/research", response_class=HTMLResponse)
    async def research_job_company(request: Request, job_id: int, _: None = Depends(require_csrf)):
        from blastjob.web.research import make_research_work

        c = deps.conn()
        try:
            job = service.job_detail(c, job_id)
        finally:
            c.close()
        if job is None:
            return HTMLResponse("Not found", status_code=404)
        run = RUNTIME.start(
            "research",
            make_research_work(job["company"], deps.load_config, job_id=job_id),
            key=co.company_key(job["company"]),
        )
        return deps.render(
            request, "_build_panel.html", run=run, heading=f"Researching {job['company']}"
        )

    @app.post("/companies/{key}/research", response_class=HTMLResponse)
    async def research_company_route(request: Request, key: str, _: None = Depends(require_csrf)):
        from blastjob.web.research import make_research_work

        c = deps.conn()
        try:
            profile = co.get_profile_by_key(c, key)
        finally:
            c.close()
        if profile is None:
            return HTMLResponse("Not found", status_code=404)
        run = RUNTIME.start(
            "research",
            make_research_work(profile["company"], deps.load_config),
            key=co.company_key(profile["company"]),
        )
        return deps.render(
            request, "_build_panel.html", run=run, heading=f"Researching {profile['company']}"
        )

    @app.post("/contacts/{contact_id}/contacted", response_class=HTMLResponse)
    async def contact_contacted(
        request: Request,
        contact_id: int,
        undo: int = Form(0),
        _: None = Depends(require_csrf),
    ):
        c = deps.conn()
        try:
            row = people.contact(c, contact_id)
            if row is None:
                return HTMLResponse("Not found", status_code=404)
            people.mark_contacted(c, contact_id, contacted=not undo)
            return _render_contacts(deps, request, c, int(row["job_id"]))
        finally:
            c.close()

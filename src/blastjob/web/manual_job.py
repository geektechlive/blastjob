"""Add a job posting by hand: GET/POST /jobs/new.

No discovery adapter covers every channel a posting can arrive through --
the user forwarding a LinkedIn screenshot while away from a computer is the
motivating case. This route is the manual front door onto the exact same
pipeline a discovered posting goes through: it inserts one `jobs` row
(`source='manual'`) and starts an ordinary `run_matching` pass scoped to that
row, so the gate/geo/prefilter/CAS rules from `matching/pipeline.py` all
apply unchanged -- nothing here special-cases a manual row.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from blastjob.db import client as dbc
from blastjob.llm.cost import CostTracker, describe_cost
from blastjob.models.config import BlastJobConfig
from blastjob.web.runtime import RUNTIME, Run
from blastjob.web.security import require_csrf
from blastjob.web.urls import safe_external_url

Render = Callable[..., HTMLResponse]
LoadCfg = Callable[[], BlastJobConfig]

# Kept off matching.pipeline.SINGLE_FLIGHT deliberately: a manual add must
# score even while a full "matching" pass is mid-run, or the new job would
# sit unscored (invisible on every stage page) until the next daily run.
# Added to app.BANNER_RUNS so the existing global run panel shows it -- no
# second progress system.
MANUAL_SCORE_RUN = "score-job"

SOURCE = "manual"


def _now() -> str:
    return datetime.now().isoformat()


def _external_id(url: str, company: str, title: str, raw_jd: str) -> str:
    """Deterministic id so resubmitting the same posting lands on the same
    row instead of duplicating it. URL is the strongest identity signal when
    given; otherwise a stable hash of the normalized text. Never the builtin
    `hash()` -- it's salted per process, so it would not be deterministic
    across cockpit restarts."""
    basis = url if url else f"{company.strip().lower()}|{title.strip().lower()}|{raw_jd.strip()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ManualJobForm:
    """What the form posted, echoed back on error so the user doesn't retype it."""

    company: str = ""
    title: str = ""
    url: str = ""
    location: str = ""
    remote: bool = False
    raw_jd: str = ""


def _validate(company: str, title: str, raw_jd: str) -> list[str]:
    errors = []
    if not company:
        errors.append("Company is required.")
    if not title:
        errors.append("Title is required.")
    if not raw_jd:
        errors.append("Job description is required.")
    return errors


def _find_existing(conn: sqlite3.Connection, external_id: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM jobs WHERE source=? AND external_id=?", (SOURCE, external_id)
    ).fetchone()
    return int(row["id"]) if row is not None else None


async def _score_new_job(run: Run, load_config: LoadCfg, job_id: int) -> dict:
    from blastjob.matching.pipeline import run_matching

    cfg = load_config()
    c = dbc.init_db(cfg)
    try:
        run.emit("log", f"Scoring manually added job {job_id}...")
        res = await run_matching(cfg, c, CostTracker(), job_ids=[job_id], exclusive=False)
        run.emit(
            "log",
            f"Considered {res.considered}, scored {res.scored}. Cost {describe_cost(res.cost)}.",
        )
        for note in res.notes:
            run.emit("log", note)
        return {"scored": res.scored}
    finally:
        c.close()


def register(
    app: FastAPI,
    conn: Callable[[], sqlite3.Connection],
    load_config: LoadCfg,
    render: Render,
) -> None:
    @app.get("/jobs/new", response_class=HTMLResponse)
    async def new_job_form(request: Request) -> HTMLResponse:
        return render(request, "job_new.html", form=ManualJobForm(), errors=[])

    @app.post("/jobs/new", response_class=HTMLResponse, response_model=None)
    async def create_job(
        request: Request,
        company: str = Form(""),
        title: str = Form(""),
        url: str = Form(""),
        location: str = Form(""),
        remote: str = Form(""),
        raw_jd: str = Form(""),
        _: None = Depends(require_csrf),
    ) -> HTMLResponse | RedirectResponse:
        company = company.strip()
        title = title.strip()
        location = location.strip()
        raw_jd = raw_jd.strip()
        is_remote = remote in ("on", "true", "1")

        errors = _validate(company, title, raw_jd)

        raw_url = url.strip()
        safe_url = safe_external_url(raw_url) if raw_url else ""
        url_dropped = bool(raw_url) and not safe_url

        if errors:
            return render(
                request,
                "job_new.html",
                form=ManualJobForm(
                    company=company,
                    title=title,
                    url=url,
                    location=location,
                    remote=is_remote,
                    raw_jd=raw_jd,
                ),
                errors=errors,
                url_dropped=url_dropped,
            )

        ext_id = _external_id(safe_url, company, title, raw_jd)
        c = conn()
        try:
            existing_id = _find_existing(c, ext_id)
            if existing_id is not None:
                return RedirectResponse(f"/jobs/{existing_id}", status_code=303)

            job_id = dbc.upsert_job(
                c,
                source=SOURCE,
                external_id=ext_id,
                company=company,
                title=title,
                url=safe_url,
                raw_jd=raw_jd,
                geo_remote=1 if is_remote else 0,
                geo_location_raw=location,
                review_state="new",
                jd_pasted_at=_now(),
            )
        finally:
            c.close()

        RUNTIME.start(MANUAL_SCORE_RUN, lambda run: _score_new_job(run, load_config, job_id))
        query = "?url_dropped=1" if url_dropped else ""
        return RedirectResponse(f"/jobs/{job_id}{query}", status_code=303)

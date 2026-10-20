"""The cockpit's "Find the employer's posting" button (employer-JD lookup, U8).

`POST /jobs/{id}/employer-jd` runs `core.employer_jd.run_upgrade` for ONE
aggregator snippet row (network on, backoff ignored, a 15-request budget), then
re-scores what changed. It runs under the `score-job` name so the global run
panel (app.BANNER_RUNS) shows it, and it is single-flight PER JOB: a second click
while that job's run is going gets the same run back, so a double-click can't
spend the request budget twice. The button is the only network caller here; it
adopts text only through the same trust checks as the daily step.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse

from blastjob import secrets as secrets_mod
from blastjob.core import employer_jd as ejd
from blastjob.core.company import company_key
from blastjob.db import client as dbc
from blastjob.llm.cost import CostTracker, describe_cost
from blastjob.matching import rescore
from blastjob.models.config import BlastJobConfig
from blastjob.web import service
from blastjob.web.apply_link import board_name
from blastjob.web.compare import is_agency
from blastjob.web.deps import Deps
from blastjob.web.manual_job import MANUAL_SCORE_RUN
from blastjob.web.runtime import RUNTIME, Run
from blastjob.web.security import require_csrf

log = logging.getLogger(__name__)

# One-job cockpit cap (plan revision 10): a click may spend this many requests.
BUTTON_REQUEST_LIMIT = 40
PROVENANCE_PREFIX = "employer:"
GENERIC_LABEL = "employer site"

# Abuse guards: the route is CSRF-protected but a scripted loop over job ids could still
# spend Jina/LLM budget, so a job can't be re-run within the cooldown and only a couple
# of lookups run at once.
COOLDOWN_SECONDS = 600
MAX_CONCURRENT_RUNS = 2

# job_id -> time.time() of the last run this process started (covers runs that recorded
# no lookup, e.g. one that crashed or found nothing to write).
LAST_START: dict[int, float] = {}

# job_id -> its run. A finished entry is ignored (and replaced) on the next click.
IN_FLIGHT: dict[int, Run] = {}

LoadCfg = Callable[[], BlastJobConfig]


def _field(job: Any, key: str) -> str:
    try:
        return str(job[key] or "")
    except (KeyError, IndexError):
        return ""


def refusal(job: Mapping[str, Any] | Any) -> str:
    """Why the button must not run for this row, or "" when it may."""
    if not dbc.is_jd_snippet(_field(job, "raw_jd"), _field(job, "jd_pasted_at")):
        return "Not a snippet: this job already has a full description."
    if _field(job, "source") not in ejd.AGGREGATOR_SOURCES:
        return "Only aggregator postings (Adzuna, LinkedIn, Indeed) are looked up this way."
    if is_agency(_field(job, "company")):
        return "This posting is from an agency; its employer is not named, so there is no lookup."
    if not company_key(_field(job, "company")):
        return "No company on this posting, so there is nothing to look up."
    return ""


def _cooldown_left(job: Mapping[str, Any] | Any, now: float) -> float:
    """Seconds until this job may be looked up again (0 when it may)."""
    last = LAST_START.get(int(_field(job, "id") or 0), 0.0)
    try:
        recorded = datetime.fromisoformat(_field(job, "jd_lookup_at")).timestamp()
    except ValueError:
        recorded = 0.0
    return max(0.0, max(last, recorded) + COOLDOWN_SECONDS - now)


def throttle_refusal(job: Mapping[str, Any] | Any, now: float | None = None) -> str:
    """Why a NEW run must not start for this job right now, or "". A job whose run is
    already going is not throttled: single-flight hands that run back."""
    job_id = int(_field(job, "id") or 0)
    existing = IN_FLIGHT.get(job_id)
    if existing is not None and existing.status == "running":
        return ""
    left = _cooldown_left(job, time.time() if now is None else now)
    if left > 0:
        minutes = int(left // 60) + 1
        return f"This job was looked up recently; try again in about {minutes} minutes."
    running = sum(1 for r in IN_FLIGHT.values() if r.status == "running")
    if running >= MAX_CONCURRENT_RUNS:
        return "Two lookups are already running; try again when one finishes."
    return ""


def jd_provenance(job: Mapping[str, Any] | Any) -> str:
    """Where an adopted description came from: "Greenhouse", "Workday", or the
    page host. "" when the description is not an employer one."""
    source = _field(job, "jd_source")
    if not source.startswith(PROVENANCE_PREFIX):
        return ""
    link = source[len(PROVENANCE_PREFIX) :]
    if name := board_name(link):
        return name
    if link.startswith("page:") and (host := link[len("page:") :]):
        return host
    host = urlparse(_field(job, "employer_link_url")).hostname or ""
    return host or GENERIC_LABEL


def jd_provenance_url(job: Mapping[str, Any] | Any) -> str:
    """The posting the adopted text came from ("" = show the badge unlinked): the held
    source row's own url, else a page link's canonical URL, else the employer link only
    while it is still the link this text was adopted over. Never a later manual link.
    The caller renders it through safe_url."""
    source = _field(job, "jd_source")
    if not source.startswith(PROVENANCE_PREFIX):
        return ""
    if held := _field(job, "jd_link_url"):
        return held
    link_source = source[len(PROVENANCE_PREFIX) :]
    if link_source.startswith("page:"):
        return _field(job, "jd_source_ref")
    if _field(job, "employer_link_source") == link_source:
        return _field(job, "employer_link_url")
    return ""


def _outcome_line(o: ejd.RowOutcome) -> str:
    route = f" via {o.via}" if o.via else ""
    note = f" ({o.note})" if o.note else ""
    return f"Job {o.job_id}: {o.status}{route}{note}"


async def _work(run: Run, load_config: LoadCfg, job_id: int) -> dict:
    # The web process doesn't load the secrets file by itself (the Jina key lives there).
    secrets_mod.load_secrets_env()
    cfg = load_config()
    conn = dbc.init_db(cfg)
    try:
        run.emit("log", f"Looking for the employer's posting for job {job_id}...")
        res = await ejd.run_upgrade(
            cfg,
            conn,
            job_ids=[job_id],
            min_score=None,
            network=True,
            write=True,
            respect_backoff=False,
            request_limit=BUTTON_REQUEST_LIMIT,
        )
        for o in res.outcomes:
            run.emit("log", _outcome_line(o))
        changed = [*res.upgraded, *res.reverted]
        if not changed:
            run.emit("log", res.summary())
            return {"upgraded": 0, "scored": 0}
        tracker = CostTracker()
        counts = await rescore.rescore_jobs(cfg, conn, tracker, changed)
        run.emit(
            "log",
            f"{res.summary()} Re-scored {counts.undecided + counts.kept}. "
            f"Cost {describe_cost(tracker.total_call_cost)}.",
        )
        return {"upgraded": len(res.upgraded), "scored": counts.undecided + counts.kept}
    finally:
        conn.close()


def start_upgrade(load_config: LoadCfg, job_id: int) -> Run:
    """The job's run, started now unless one for it is already going."""
    existing = IN_FLIGHT.get(job_id)
    if existing is not None and existing.status == "running":
        return existing
    run = RUNTIME.start(MANUAL_SCORE_RUN, lambda r: _work(r, load_config, job_id))
    IN_FLIGHT[job_id] = run
    LAST_START[job_id] = time.time()
    return run


async def _rescore_work(run: Run, load_config: LoadCfg, job_ids: list[int]) -> dict:
    cfg = load_config()
    conn = dbc.init_db(cfg)
    try:
        run.emit("log", f"Re-scoring {len(job_ids)} job(s) after a split...")
        tracker = CostTracker()
        counts = await rescore.rescore_jobs(cfg, conn, tracker, job_ids)
        run.emit("log", f"Cost {describe_cost(tracker.total_call_cost)}.")
        return {"scored": counts.undecided + counts.kept}
    finally:
        conn.close()


def start_rescore_jobs(load_config: LoadCfg, job_ids: list[int]) -> Run:
    """One `score-job` run re-scoring `job_ids` (rows whose adopted text was reverted)."""
    return RUNTIME.start(MANUAL_SCORE_RUN, lambda r: _rescore_work(r, load_config, job_ids))


def register(app: FastAPI, deps: Deps) -> None:
    @app.post("/jobs/{job_id}/employer-jd", response_class=HTMLResponse)
    async def job_employer_jd(
        request: Request, job_id: int, _: None = Depends(require_csrf)
    ) -> HTMLResponse:
        c = deps.conn()
        try:
            job = service.job_detail(c, job_id)
        finally:
            c.close()
        if job is None:
            return HTMLResponse("Not found", status_code=404)
        why = refusal(job)
        if why:
            return deps.render(request, "_jd_block.html", job=job, jd_error=why)
        if slow := throttle_refusal(job):
            return deps.render(request, "_jd_block.html", job=job, jd_error=slow)
        start_upgrade(deps.load_config, job_id)
        resp = deps.render(
            request,
            "_jd_block.html",
            job=job,
            jd_error="Looking for the employer's posting; the page updates when it's done.",
        )
        # base.html's #run-status listens for this and re-reads /runs/active.
        resp.headers["HX-Trigger"] = "refresh-run-status"
        return resp

"""The Running now / Pause view at `/runs` (docs/how-it-works/web.md).

Shows what is running or queued, with a Stop button; today's model calls and known spend by
caller and origin against the caps; calls whose owner died (abandoned); and the "Pause
scheduled runs" switch. Every POST goes through `require_csrf` like the rest of the cockpit.

Stop cancels the run's task, and the gateway's reap kills the model child. Pause makes the
scheduled daily run exit without work; it does not stop a run already in flight.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from blastjob import config as cfg_mod
from blastjob.core import pause
from blastjob.llm import ledger, ledger_stats
from blastjob.models.config import BlastJobConfig
from blastjob.web.deps import Deps
from blastjob.web.runtime import RUNTIME, Run
from blastjob.web.security import require_csrf

log = logging.getLogger(__name__)

ORIGIN_LABELS = {"scheduled": "Scheduled (daily run)", "clicked": "Clicked (cockpit)"}
SECONDS_PER_MINUTE = 60


def elapsed_text(seconds: float) -> str:
    whole = max(int(seconds), 0)
    return f"{whole // SECONDS_PER_MINUTE}m {whole % SECONDS_PER_MINUTE:02d}s"


def _run_row(run: Run, now: float) -> dict[str, Any]:
    return {
        "id": run.id,
        "name": run.name,
        "key": run.key or "",
        "queued": run.queued,
        "elapsed": elapsed_text(now - run.started_at),
        "stopping": run.stop_requested,
        "stopping_for": elapsed_text(now - run.stop_requested_at) if run.stop_requested else "",
    }


def panel_context(deps: Deps) -> dict[str, Any]:
    cfg = deps.load_config()
    now = time.monotonic()
    rows = [_run_row(r, now) for r in RUNTIME.running_runs()]
    day = dt.date.today()
    caps = {
        "scheduled": cfg.limits.scheduled_calls_per_day,
        "clicked": cfg.limits.clicked_calls_per_day,
    }
    conn = deps.conn()
    try:
        origins = [
            {
                "label": ORIGIN_LABELS[t.origin],
                "calls": t.calls,
                "cap": caps[t.origin],
                "spend": ledger_stats.describe_spend(t),
                "refused": t.refused,
            }
            for t in ledger_stats.origin_totals(conn, day)
        ]
        groups = ledger_stats.by_caller(conn, day)
        abandoned = ledger_stats.abandoned_calls(conn, day)
    finally:
        conn.close()
    data_dir = cfg_mod.data_dir(cfg)
    return {
        "running": [r for r in rows if not r["queued"]],
        "queued": [r for r in rows if r["queued"]],
        "origins": origins,
        "groups": groups,
        "abandoned": abandoned,
        "paused": pause.is_paused(data_dir),
        "paused_since": pause.paused_since(data_dir),
    }


def pause_state(load_config: Callable[[], BlastJobConfig]) -> Callable[[], str | None]:
    """For the nav: None when not paused, else when the PAUSE file was made ("" if unreadable)."""

    def state() -> str | None:
        data_dir = cfg_mod.data_dir(load_config())
        return pause.paused_since(data_dir) if pause.is_paused(data_dir) else None

    return state


def reconcile_on_startup(
    load_config: Callable[[], BlastJobConfig],
) -> Callable[[], Awaitable[None]]:
    """A startup hook: model calls whose owner process died (a crash, a restart) become
    'abandoned'. Loading the config installs the ledger path, so this reads the injected
    loader's database. (A hook on `app.router`, as the debrief store's shutdown hook is: a
    `lifespan=` argument would silence that one.)"""

    async def on_startup() -> None:
        try:
            load_config()
            ledger.reconcile_abandoned()
        except Exception:  # noqa: BLE001 - startup must not fail on the ledger
            log.warning("could not reconcile abandoned model calls at startup", exc_info=True)

    return on_startup


def register(app: FastAPI, deps: Deps) -> None:
    def panel(request: Request) -> HTMLResponse:
        return deps.render(request, "_runs_panel.html", **panel_context(deps))

    # Registered before app.py's `/runs/{run_id}` so these fixed paths are not read as run ids.
    @app.get("/runs", response_class=HTMLResponse)
    async def runs_page(request: Request) -> HTMLResponse:
        return deps.render(request, "runs.html", **panel_context(deps))

    @app.get("/runs/panel", response_class=HTMLResponse)
    async def runs_panel(request: Request) -> HTMLResponse:
        return panel(request)

    @app.post("/runs/pause", response_class=HTMLResponse)
    async def pause_scheduled(request: Request, _: None = Depends(require_csrf)) -> HTMLResponse:
        try:
            pause.set_paused(cfg_mod.data_dir(deps.load_config()), True)
        except pause.PauseError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return panel(request)

    @app.post("/runs/resume", response_class=HTMLResponse)
    async def resume_scheduled(request: Request, _: None = Depends(require_csrf)) -> HTMLResponse:
        pause.set_paused(cfg_mod.data_dir(deps.load_config()), False)
        return panel(request)

    @app.post("/runs/{run_id}/stop", response_class=HTMLResponse)
    async def stop_run(
        request: Request, run_id: str, _: None = Depends(require_csrf)
    ) -> HTMLResponse:
        RUNTIME.stop(run_id)
        return panel(request)

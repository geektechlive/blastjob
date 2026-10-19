"""The job page's Activity + Files panel.

Activity is the job's application events (`core/events.py`), newest first,
with a small form for the ones only the user knows about (a thank-you note, a
call). Files is the artifact index (`core/artifacts.py`); a file links to the
existing download route only when that route would serve exactly this file
(`web/downloads.is_downloadable`), otherwise its path shows as text. There is
no new file-serving route here, on purpose.

`detail.html` includes `_activity_files.html`, which lazy-loads this panel, so
the detail route itself is untouched.
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from blastjob import config as cfg_mod
from blastjob.core import artifacts, events
from blastjob.core.paths import locate
from blastjob.models.config import BlastJobConfig
from blastjob.web.deps import Deps
from blastjob.web.downloads import is_downloadable
from blastjob.web.security import require_csrf

MAX_NOTE_CHARS = 2000


def _job_exists(conn: sqlite3.Connection, job_id: int) -> bool:
    return conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is not None


def _event_rows(conn: sqlite3.Connection, job_id: int) -> list[dict[str, Any]]:
    return [
        {**dict(e), "label": events.KIND_LABELS.get(e["kind"], e["kind"])}
        for e in events.events_for(conn, job_id)
    ]


def _file_rows(conn: sqlite3.Connection, cfg: BlastJobConfig, job_id: int) -> list[dict[str, Any]]:
    rows = []
    out_dir = cfg_mod.output_dir(cfg)
    for a in artifacts.artifacts_for(conn, job_id):
        path = Path(a["path"])
        linked = is_downloadable(conn, cfg, job_id, path)
        rows.append(
            {
                "kind": a["kind"],
                "name": path.name,
                # Where it is NOW: the row is root form, the entry may be filed.
                "path": str(locate(path, out_dir)),
                "date": (a["updated_at"] or "")[:10],
                "href": f"/jobs/{job_id}/files/{quote(path.name)}" if linked else "",
            }
        )
    return rows


def _panel(request: Request, deps: Deps, conn: sqlite3.Connection, job_id: int) -> HTMLResponse:
    return deps.render(
        request,
        "_activity_panel.html",
        job_id=job_id,
        events=_event_rows(conn, job_id),
        files=_file_rows(conn, deps.load_config(), job_id),
        kinds=events.KIND_LABELS,
        today=datetime.date.today().isoformat(),
    )


def _validated(kind: str, at: str, note: str) -> tuple[str, str, str]:
    """(kind, at, note) or ValueError with a message fit to show."""
    if kind not in events.EVENT_KINDS:
        raise ValueError("Pick what happened from the list.")
    try:
        stamp = events.normalize_at(at)
    except ValueError as exc:
        raise ValueError("Give a date like 2026-10-09.") from exc
    text = note.strip()
    if len(text) > MAX_NOTE_CHARS:
        raise ValueError(f"Keep the note under {MAX_NOTE_CHARS} characters.")
    return kind, stamp, text


def register(app: FastAPI, deps: Deps) -> None:
    @app.get("/jobs/{job_id}/activity", response_class=HTMLResponse)
    async def activity_panel(request: Request, job_id: int) -> HTMLResponse:
        c = deps.conn()
        try:
            if not _job_exists(c, job_id):
                raise HTTPException(status_code=404, detail="job not found")
            return _panel(request, deps, c, job_id)
        finally:
            c.close()

    @app.post("/jobs/{job_id}/events", response_class=HTMLResponse)
    async def add_event(
        request: Request,
        job_id: int,
        kind: str = Form(""),
        at: str = Form(""),
        note: str = Form(""),
        _: None = Depends(require_csrf),
    ) -> HTMLResponse:
        c = deps.conn()
        try:
            if not _job_exists(c, job_id):
                raise HTTPException(status_code=404, detail="job not found")
            try:
                checked_kind, stamp, text = _validated(kind, at, note)
            except ValueError as exc:
                return HTMLResponse(str(exc), status_code=400)
            events.add_event(c, job_id, checked_kind, stamp, note=text, source="manual")
            return _panel(request, deps, c, job_id)
        finally:
            c.close()

    @app.post("/jobs/{job_id}/events/{event_id}/delete", response_class=HTMLResponse)
    async def delete_event(
        request: Request,
        job_id: int,
        event_id: int,
        _: None = Depends(require_csrf),
    ) -> HTMLResponse:
        c = deps.conn()
        try:
            if not events.delete_manual_event(c, job_id, event_id):
                raise HTTPException(status_code=404, detail="no such manual event on this job")
            return _panel(request, deps, c, job_id)
        finally:
            c.close()

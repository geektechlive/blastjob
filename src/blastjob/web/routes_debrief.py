"""The interview debrief routes (#84): paste a transcript, say which speaker you are, read the
figures on the job page.

The transcript arrives as pasted text in an urlencoded form (never a file upload), is held in
memory only (`core/debrief_store.py`), and is popped before anything is computed. Only the
figures and the owner's own short excerpts are stored. Every POST goes through `require_csrf`
(token plus Origin check); the body is capped by `PathBodyCapMiddleware`. Jobs are scoped by
the path id, and an upload id from another job is treated as unknown. No LLM and no network.
See docs/how-it-works/debrief.md.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from blastjob import config as cfg_mod
from blastjob.core import debrief
from blastjob.core.debrief_store import UploadStore
from blastjob.core.transcript import TranscriptError, parse_transcript
from blastjob.web.deps import Deps
from blastjob.web.security import require_csrf

log = logging.getLogger(__name__)

WORK_HISTORY_FILE = "work_history.md"
PREVIEW_CHARS = 80
UPLOAD_AGAIN = "That upload has expired or is gone. Paste the transcript and upload again."
STORE_FAILED = (
    "The debrief could not be saved, and the transcript has already been dropped from memory. "
    "Please upload it again."
)


def _job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT id, title, company FROM jobs WHERE id=?", (job_id,)).fetchone()


def _speaker_rows(entry_speakers: dict[str, int], previews: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {"name": name, "turns": turns, "preview": previews.get(name, "")}
        for name, turns in entry_speakers.items()
    ]


def register(app: FastAPI, deps: Deps) -> None:
    store = UploadStore()
    app.state.debrief_store = store
    app.router.on_shutdown.append(store.clear)

    def _page(
        request: Request,
        job: sqlite3.Row,
        step: str,
        status: int = 200,
        **extra: Any,
    ) -> HTMLResponse:
        response = deps.render(request, "debrief.html", job=job, step=step, **extra)
        response.status_code = status
        return response

    def _with_job(job_id: int) -> sqlite3.Row | None:
        c = deps.conn()
        try:
            return _job(c, job_id)
        finally:
            c.close()

    def _trusted_text() -> str | None:
        """The work history, or None when it cannot be read (the numbers check then says so)."""
        path = cfg_mod.data_dir(deps.load_config()) / WORK_HISTORY_FILE
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            log.warning("debrief: work history unreadable; numbers check unavailable")
            return None

    @app.get("/jobs/{job_id}/debrief", response_class=HTMLResponse)
    async def debrief_paste(request: Request, job_id: int):
        job = _with_job(job_id)
        if job is None:
            return HTMLResponse("Not found", status_code=404)
        return _page(request, job, "paste")

    @app.post("/jobs/{job_id}/debrief/upload", response_class=HTMLResponse)
    async def debrief_upload(
        request: Request,
        job_id: int,
        transcript: str = Form(""),
        label: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        job = _with_job(job_id)
        if job is None:
            return HTMLResponse("Not found", status_code=404)
        clean_label = label.strip()[: debrief.MAX_LABEL_CHARS]
        try:
            parsed = await run_in_threadpool(parse_transcript, transcript)
            upload_id = store.put(job_id, parsed, clean_label)
        except (TranscriptError, ValueError) as err:
            return _page(
                request, job, "paste", 400, error=str(err), transcript=transcript, label=label
            )
        return RedirectResponse(f"/jobs/{job_id}/debrief/{upload_id}", status_code=303)

    @app.get("/jobs/{job_id}/debrief/{upload_id}", response_class=HTMLResponse)
    async def debrief_choose(request: Request, job_id: int, upload_id: str):
        job = _with_job(job_id)
        if job is None:
            return HTMLResponse("Not found", status_code=404)
        entry = store.peek(upload_id, job_id)
        if entry is None:
            return _page(request, job, "gone", 410, error=UPLOAD_AGAIN)
        previews: dict[str, str] = {}
        for turn in entry.transcript.turns:
            if turn.speaker not in previews and turn.text.strip():
                previews[turn.speaker] = " ".join(turn.text.split())[:PREVIEW_CHARS]
        rows = _speaker_rows(entry.transcript.speakers, previews)
        return _page(request, job, "choose", upload_id=upload_id, speakers=rows)

    @app.post("/jobs/{job_id}/debrief/{upload_id}/compute", response_class=HTMLResponse)
    async def debrief_compute(
        request: Request,
        job_id: int,
        upload_id: str,
        speaker: str = Form(""),
        _: None = Depends(require_csrf),
    ):
        job = _with_job(job_id)
        if job is None:
            return HTMLResponse("Not found", status_code=404)
        entry = store.peek(upload_id, job_id)
        if entry is None:
            return _page(request, job, "gone", 410, error=UPLOAD_AGAIN)
        if speaker not in entry.speakers:
            return _page(request, job, "gone", 400, error="Pick one of the speakers listed.")
        # Popped before anything is computed: from here a failure means "upload again".
        claimed = store.claim(upload_id, job_id, speaker)
        if claimed is None:
            return _page(request, job, "gone", 410, error=UPLOAD_AGAIN)
        result = debrief.compute_debrief(claimed.transcript, speaker, _trusted_text())
        c = deps.conn()
        try:
            debrief.insert_debrief(c, job_id, claimed.id, claimed.label, result)
        except sqlite3.Error:
            log.exception("debrief: could not store the debrief for job %s", job_id)
            return _page(request, job, "gone", 500, error=STORE_FAILED)
        finally:
            c.close()
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/debrief/{debrief_id}/delete", response_class=HTMLResponse)
    async def debrief_delete(
        job_id: int,
        debrief_id: int,
        _: None = Depends(require_csrf),
    ):
        c = deps.conn()
        try:
            removed = debrief.delete_debrief(c, job_id, debrief_id)
        finally:
            c.close()
        if not removed:
            return HTMLResponse("Not found", status_code=404)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

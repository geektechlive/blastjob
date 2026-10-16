"""Tailor-on-keep + outreach draft orchestration for the web layer.

Tailoring reuses the existing, tested core.build.run_build (resume + cover letter
+ fit score + per-application folder). It runs as an idempotent background run:
a CAS build-claim + lease means a browser refresh mid-build can't double-start it,
and the streamed output replays from the run's event log (Codex #3/#12).

Outreach emails are UNADDRESSED by default (Codex #15): we never invent a
recipient. An ADDRESSED draft is produced only for a stored contact — a real
person found by `web/people.py` with the URL they were found at, which the user
verifies. Draft-only — nothing is ever sent.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from blastjob import config as cfg_mod
from blastjob.db import client as dbc
from blastjob.llm import caching
from blastjob.llm.cost import CostTracker
from blastjob.llm.providers import make_stream
from blastjob.models.config import BlastJobConfig
from blastjob.web.apply_file import write_apply_file
from blastjob.web.runtime import Event, Run

log = logging.getLogger(__name__)

# docx is what recruiters/ATS want; PDF is deliberately omitted (ATS parsing
# issues, and it avoids the WeasyPrint system-lib dependency).
TAILOR_FORMATS = {"md", "docx", "ats"}

OUTREACH_SYSTEM = """You write concise, professional cold-outreach notes for a \
candidate pursuing a role. Never invent a recipient name, title, or email address. \
If NO recipient block is supplied, open with "Dear Hiring Manager,". If a recipient \
block IS supplied, address that person by first name, acknowledge their role in one \
natural clause (no flattery), and shape the ask to them: a recruiter gets a crisp \
fit summary and availability; a hiring manager gets a peer-level note about the \
mandate and a request for a short conversation. ~150 words, LinkedIn-message \
length: specific interest in the role, 2-3 strengths grounded in the candidate's \
work history, a courteous call to connect. Output only the message body."""


def recipient_block(contact: sqlite3.Row | None) -> str:
    if contact is None:
        return ""
    return (
        "\n\nRecipient (verified by the candidate):\n"
        f"Name: {contact['name']}\nTitle: {contact['title']}\n"
        f"Relationship to the role: {contact['role'].replace('_', ' ')}\n"
        f"Why them: {contact['why']}\n"
    )


def sse_format(ev: Event) -> str:
    """Serialize a runtime Event as one SSE frame (multiline data split per spec)."""
    lines = ev.data.split("\n") if ev.data else [""]
    frame = f"id: {ev.id}\nevent: {ev.kind}\n"
    frame += "".join(f"data: {ln}\n" for ln in lines)
    return frame + "\n"


def _job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, company, title, raw_jd, jd_pasted_at, review_state, build_state "
        "FROM jobs WHERE id=?",
        (job_id,),
    ).fetchone()


def _register_outputs(
    conn: sqlite3.Connection, job: sqlite3.Row, run_id: str, out_dir: Path
) -> None:
    """Index the files this build wrote for THIS role (`core/artifacts`).
    Best-effort: the build is already recorded as done, and bookkeeping never
    changes that or reaches the user as an error."""
    from blastjob.core import artifacts

    try:
        files = artifacts.role_files(
            out_dir, company=job["company"] or "", role=job["title"] or "", job_id=job["id"]
        )
        if not files:
            log.warning(
                "build %s: no files matched job %s's role in %s", run_id, job["id"], out_dir
            )
        apply_page = write_apply_file(conn, job["id"], files)
        if apply_page is not None:
            files = [*files, apply_page]
        artifacts.register_paths(
            conn, files, job_id=job["id"], build_run_id=artifacts.build_run_pk(conn, run_id)
        )
    except Exception:  # noqa: BLE001 -- see docstring
        log.warning("artifact registration failed for build %s", run_id, exc_info=True)


def make_tailor_work(job_id: int, load_config=cfg_mod.load_config):
    async def work(run: Run):
        from blastjob.core.build import run_build

        cfg: BlastJobConfig = load_config()
        conn = dbc.init_db(cfg)
        try:
            job = _job(conn, job_id)
            if job is None:
                run.emit("error", "job not found")
                return None
            if dbc.is_jd_snippet(job["raw_jd"], job["jd_pasted_at"]):
                run.emit(
                    "log",
                    "Not tailored: we only have a snippet of this job description. "
                    "Open the full posting, paste the full description below, then tailor.",
                )
                return None
            if not dbc.claim_for_build(conn, job_id, run.id):
                run.emit("log", "A tailoring build is already in progress for this job.")
                return None

            run.emit("log", f"Tailoring for {job['title']} @ {job['company']}...")
            ct = CostTracker()
            ok = False
            out_dir = None
            try:
                out_dir, _fit = await run_build(
                    company=job["company"],
                    role=job["title"],
                    job_description=job["raw_jd"] or "",
                    formats=TAILOR_FORMATS,
                    use_ats=False,
                    app_config=cfg,
                    cost_tracker=ct,
                    on_text=lambda t: run.emit("output", t),
                    include_cover_letter=True,
                )
                ok = True
            except Exception as e:  # noqa: BLE001 — record on the run, mark build failed
                run.emit("error", f"{type(e).__name__}: {e}")
            dbc.finish_build(conn, run.id, artifact_dir=str(out_dir or ""), ok=ok)
            if ok:
                run.emit("log", f"Saved bundle to {out_dir}")
                if out_dir is not None:
                    _register_outputs(conn, job, run.id, out_dir)
            return {"artifact_dir": str(out_dir or ""), "ok": ok}
        finally:
            conn.close()

    return work


def make_outreach_work(job_id: int, load_config=cfg_mod.load_config, contact_id: int | None = None):
    async def work(run: Run):
        from blastjob.web import people

        cfg: BlastJobConfig = load_config()
        conn = dbc.init_db(cfg)
        try:
            job = _job(conn, job_id)
            if job is None:
                run.emit("error", "job not found")
                return None
            recipient = people.contact(conn, contact_id) if contact_id else None
            if contact_id and (recipient is None or int(recipient["job_id"]) != int(job["id"])):
                run.emit("error", "contact not found for this job")
                return None
            wh_path = cfg_mod.data_dir(cfg) / "work_history.md"
            wh = wh_path.read_text(encoding="utf-8") if wh_path.exists() else ""

            if recipient is not None:
                run.emit("log", f"Drafting a note addressed to {recipient['name']}...")
            else:
                run.emit("log", "Drafting an unaddressed outreach email...")
            system = [caching.cached_text(OUTREACH_SYSTEM)]
            user = [
                caching.cached_text(wh),
                caching.plain_text(
                    f"Role: {job['title']}\nCompany: {job['company']}\n\n"
                    f"Job description:\n{(job['raw_jd'] or '')[:2000]}" + recipient_block(recipient)
                ),
            ]
            text = ""
            stream = await make_stream(
                cfg, system, [{"role": "user", "content": user}], max_tokens=1024
            )
            async with stream:
                async for chunk in stream.text_stream:
                    text += chunk
                    run.emit("output", chunk)
            if recipient is not None:
                run.emit(
                    "log", f"Draft ready — addressed to {recipient['name']}; verify before sending."
                )
            else:
                run.emit(
                    "log", "Draft ready (unaddressed — add a verified recipient before sending)."
                )
            return {"email": text, "contact_id": contact_id}
        finally:
            conn.close()

    return work

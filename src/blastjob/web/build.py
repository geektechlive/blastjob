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

import contextlib
import logging
import sqlite3
from pathlib import Path

from blastjob import config as cfg_mod
from blastjob.core import filing_lock, network_views, outreach_asks, packet_filing
from blastjob.core.packet_verify import PacketNotQuarantinedError
from blastjob.core.paths import canonical
from blastjob.db import client as dbc
from blastjob.llm import caching
from blastjob.llm.cost import CostTracker
from blastjob.llm.providers import make_stream
from blastjob.models.config import BlastJobConfig
from blastjob.web.apply_file import write_apply_file
from blastjob.web.research_context import job_research_context
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
work history, a courteous call to connect. If a Request line is supplied, make that the \
ask. State no relationship with the recipient beyond the facts listed. Output only the \
message body."""

_INTENTS = {
    "intro": "introduce the candidate to the hiring manager for this role",
    "referral": "refer the candidate for this role",
    "recruiter": "talk with the candidate about their search and the roles they place",
    "advice": "give the candidate advice on their search or this role (no job is requested)",
}


FIELD_CAP = 120
WHY_CAP = 300


def _one_line(value: object, cap: int = FIELD_CAP) -> str:
    """An export or contact field as one capped line, so its text cannot start a new prompt
    line (a forged "Request:" line)."""
    return " ".join(str(value or "").split())[:cap]


def network_recipient_block(person: dict, company: str, kind: str) -> str:
    """The recipient block for a person from the candidate's own network export. Facts from
    the export only (no invented relationship text), plus the ask's intent."""
    lines = [
        "\n\nRecipient (from the candidate's LinkedIn connections export; "
        "the candidate verifies before sending):",
        f"Name: {_one_line(person['name'])}",
        f"Position: {_one_line(person['position'])}",
        f"Company: {_one_line(company)}",
    ]
    if person.get("recommended_me"):
        lines.append("This person wrote the candidate a LinkedIn recommendation.")
    if person.get("last_message_at"):
        lines.append(f"Last messaged on LinkedIn: {_one_line(person['last_message_at'])}")
    lines.append(f"Request: ask them to {_INTENTS[kind]}.")
    return "\n".join(lines) + "\n"


def recipient_block(contact: sqlite3.Row | None) -> str:
    if contact is None:
        return ""
    return (
        "\n\nRecipient (verified by the candidate):\n"
        f"Name: {_one_line(contact['name'])}\nTitle: {_one_line(contact['title'])}\n"
        f"Relationship to the role: {_one_line(contact['role'].replace('_', ' '))}\n"
        f"Why them: {_one_line(contact['why'], WHY_CAP)}\n"
    )


def sse_format(ev: Event) -> str:
    """Serialize a runtime Event as one SSE frame (multiline data split per spec)."""
    # A bare \r is a line terminator to an SSE parser too, so fold CRLF/CR to LF
    # before splitting or a stray one would end a data line early.
    data = ev.data.replace("\r\n", "\n").replace("\r", "\n")
    lines = data.split("\n") if data else [""]
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
    conn: sqlite3.Connection, job: sqlite3.Row, run_id: str, out_dir: Path, output_root: Path
) -> None:
    """Index the files this build wrote for THIS role (`core/artifacts`), in
    root form. Best-effort: the build is already recorded as done, and
    bookkeeping never changes that or reaches the user as an error."""
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
            conn,
            files,
            job_id=job["id"],
            build_run_id=artifacts.build_run_pk(conn, run_id),
            output_root=output_root,
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
                run.emit(
                    "log", "A tailoring build or a revision is already in progress for this job."
                )
                return None

            run.emit("log", f"Tailoring for {job['title']} @ {job['company']}...")
            ct = CostTracker()
            ok = False
            out_dir = None
            root = cfg_mod.output_dir(cfg)
            # No filing lock while the model streams: the claim's running
            # build_runs row makes the filer skip this job's entries and
            # today's, and `run_build` takes the lock only to pick the folder.
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
                    job_context=job_research_context(conn, job_id),
                )
                ok = True
            except PacketNotQuarantinedError as e:
                # Failed, never 'tailored': a held document is still public. The
                # folder is recorded anyway so the job page points at the packet.
                out_dir = e.out_dir
                run.emit("error", f"{type(e).__name__}: {e}")
            except Exception as e:  # noqa: BLE001 — record on the run, mark build failed
                run.emit("error", f"{type(e).__name__}: {e}")
            # Shared lock while the build is marked done and its last file (the
            # apply-link page) is written: once done, nothing else protects it.
            async with contextlib.AsyncExitStack() as held:
                try:
                    await held.enter_async_context(filing_lock.shared_async(root))
                except OSError:
                    # The build must still be recorded, lock or no lock.
                    log.warning("filing lock unavailable for build %s", run.id, exc_info=True)
                # Stored in root form (it is at the root now; canonical is the rule).
                stored = str(canonical(out_dir, root)) if out_dir else ""
                dbc.finish_build(conn, run.id, artifact_dir=stored, ok=ok)
                if ok:
                    run.emit("log", f"Saved bundle to {out_dir}")
                    if out_dir is not None:
                        _register_outputs(conn, job, run.id, out_dir, root)
            if ok and out_dir is not None:
                # After the lock is released: a rebuild of a submitted job lands
                # at the root, and this files it now.
                packet_filing.file_after_change(conn, root, job_id)
            return {"artifact_dir": stored, "ok": ok}
        finally:
            conn.close()

    return work


def _network_block(
    conn: sqlite3.Connection, job: sqlite3.Row, nid: int, kind: str
) -> tuple[str, str]:
    """(recipient block, name) for a network contact; raises AskError unless the person is on
    this job's current warm path (the same validator `log_ask` uses)."""
    outreach_asks.validate_recipient(
        conn, job_id=int(job["id"]), network_contact_id=nid, contact_id=None
    )
    person = next(
        p for p in network_views.warm_paths_for_job(conn, int(job["id"])) if p["id"] == nid
    )
    return network_recipient_block(person, job["company"] or "", kind), person["name"]


def make_outreach_work(
    job_id: int,
    load_config=cfg_mod.load_config,
    contact_id: int | None = None,
    network_contact_id: int | None = None,
    kind: str = "",
):
    async def work(run: Run):
        from blastjob.web import people

        cfg: BlastJobConfig = load_config()
        conn = dbc.init_db(cfg)
        try:
            job = _job(conn, job_id)
            if job is None:
                run.emit("error", "job not found")
                return None
            if contact_id and network_contact_id:
                run.emit("error", "name one person, not two")
                return None
            recipient = people.contact(conn, contact_id) if contact_id else None
            if contact_id and (recipient is None or int(recipient["job_id"]) != int(job["id"])):
                run.emit("error", "contact not found for this job")
                return None
            block = recipient_block(recipient)
            name = recipient["name"] if recipient is not None else ""
            if network_contact_id:
                try:
                    outreach_asks.validate_kind(kind)
                    block, name = _network_block(conn, job, network_contact_id, kind)
                except outreach_asks.AskError as e:
                    run.emit("error", str(e))
                    return None
            wh_path = cfg_mod.data_dir(cfg) / "work_history.md"
            wh = wh_path.read_text(encoding="utf-8") if wh_path.exists() else ""

            if name:
                run.emit("log", f"Drafting a note addressed to {name}...")
            else:
                run.emit("log", "Drafting an unaddressed outreach email...")
            system = [caching.cached_text(OUTREACH_SYSTEM)]
            user = [
                caching.cached_text(wh),
                caching.plain_text(
                    f"Role: {job['title']}\nCompany: {job['company']}\n\n"
                    f"Job description:\n{(job['raw_jd'] or '')[:2000]}" + block
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
            if name:
                run.emit("log", f"Draft ready — addressed to {name}; verify before sending.")
            else:
                run.emit(
                    "log", "Draft ready (unaddressed — add a verified recipient before sending)."
                )
            return {
                "email": text,
                "contact_id": contact_id,
                "network_contact_id": network_contact_id,
            }
        finally:
            conn.close()

    return work

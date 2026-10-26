"""Background Run wrapper around the follow-up drafter.

Mirrors `make_prep_work`: the web layer does the database lookups (job row,
follow-up reason, optional stored contact) and hands `core.followup` plain
values, so nothing in `core/` reaches back into the web layer.

`artifact_dir` is resolved by the CALLER (the route looks it up with
`web.packets.packet_for_job`) and passed in. This module only validates that the
directory is still there and files the draft under a clearly-named fallback when
it isn't, so a deleted packet folder degrades into a findable file rather than a
stack trace on a button click.
"""

from __future__ import annotations

import logging
from pathlib import Path

from blastjob import config as cfg_mod
from blastjob.core import followup as core_followup
from blastjob.core.paths import locate
from blastjob.db import client as dbc
from blastjob.web import service, stage_lists
from blastjob.web.runtime import Run

log = logging.getLogger(__name__)


def _resolve_dir(artifact_dir: str | Path | None, cfg, run: Run) -> Path:
    if artifact_dir:
        directory = locate(artifact_dir, cfg_mod.output_dir(cfg))
        if directory.is_dir():
            return directory
    directory = core_followup.fallback_dir(cfg_mod.output_dir(cfg))
    run.emit("log", f"No tailored packet on file for this job. Filing the draft in {directory}")
    return directory


def _register(conn, cfg, job_id: int, path: Path) -> None:
    """Index the draft against this job (`core/artifacts`), and against the
    build whose folder it landed in, if any (compared in root form: the folder
    may be filed). Best-effort: never fails the draft."""
    from blastjob.core import artifacts

    try:
        root = cfg_mod.output_dir(cfg)
        run_pk = artifacts.run_for_dir(conn, job_id, Path(path).parent, root)
        artifacts.register_paths(
            conn, [Path(path)], job_id=job_id, build_run_id=run_pk, output_root=root
        )
    except Exception:  # noqa: BLE001
        log.warning("artifact registration failed for follow-up %s", path, exc_info=True)


def make_followup_work(
    job_id: int,
    load_config=cfg_mod.load_config,
    *,
    artifact_dir: str | Path | None = None,
    contact_id: int | None = None,
    to: str = "",
):
    async def work(run: Run):
        from blastjob.web import people

        cfg = load_config()
        conn = dbc.init_db(cfg)
        try:
            row = service.job_detail(conn, job_id)
            if row is None:
                run.emit("error", "job not found")
                return None

            contact = None
            if contact_id:
                contact = people.contact(conn, contact_id)
                if contact is None or int(contact["job_id"]) != int(row["id"]):
                    run.emit("error", "contact not found for this job")
                    return None

            job = dict(row)
            reason = stage_lists.followup_reason(row, conn)
            directory = _resolve_dir(artifact_dir, cfg, run)

            who = f" addressed to {contact['name']}" if contact is not None else ""
            run.emit("log", f"Drafting a follow-up{who} for {job['title']} @ {job['company']}...")
            draft = await core_followup.generate_followup(
                cfg,
                job=job,
                reason=reason,
                contact=dict(contact) if contact is not None else None,
                artifact_dir=directory,
                to=to,
                on_text=lambda t: run.emit("output", t),
            )
            _register(conn, cfg, job_id, draft.path)
            run.emit("log", f"Saved {draft.path}")
            run.emit("log", "Draft only. Read it, edit it, then send it from your mail app.")
            return {
                "path": str(draft.path),
                "mailto": draft.mailto,
                "subject": draft.subject,
                "to": draft.to,
                "email": draft.body,
            }
        finally:
            conn.close()

    return work

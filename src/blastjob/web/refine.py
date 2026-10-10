"""Background wrapper for a corrective resume revision.

`core/refine.py` was dead in production for months -- it read filenames no build
had written since the output layout changed -- so this is the first caller that
could ever have worked. It exists mainly to close the groundedness loop: the
packet panel tells the user which claims are unsupported, and this is the action that
does something about it without a full re-tailor.

`job_id` is passed by the route so the revised and rotated files are indexed
against the job explicitly (`core/artifacts`). It is never inferred from the
folder: a current-layout folder is shared by every role at that company that
day.
"""

from __future__ import annotations

import logging
from pathlib import Path

from blastjob import config as cfg_mod
from blastjob.llm.cost import CostTracker
from blastjob.web.runtime import Run

log = logging.getLogger(__name__)


def _register(cfg, job_id: int, run_dir: Path, artifact_dir: str) -> None:
    """Index this job's files in the packet folder after a revision. A re-scan
    by the role's naming rules, not refine's own pick of "newest resume", so
    another role's files in a shared folder are never attributed here.
    Best-effort: never fails the revision."""
    from blastjob.core import artifacts
    from blastjob.db import client as dbc

    try:
        conn = dbc.init_db(cfg)
    except Exception:  # noqa: BLE001
        log.warning("artifact registration skipped for job %s", job_id, exc_info=True)
        return
    try:
        job = conn.execute("SELECT company, title FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return
        files = artifacts.role_files(
            run_dir, company=job["company"] or "", role=job["title"] or "", job_id=job_id
        )
        if not files:
            log.warning("refine: no files matched job %s's role in %s", job_id, run_dir)
        run_pk = artifacts.run_for_dir(conn, job_id, artifact_dir)
        artifacts.register_paths(conn, files, job_id=job_id, build_run_id=run_pk)
    except Exception:  # noqa: BLE001
        log.warning("artifact registration failed for job %s", job_id, exc_info=True)
    finally:
        conn.close()


def make_refine_work(
    artifact_dir: str | None,
    feedback: str,
    load_config=cfg_mod.load_config,
    *,
    job_id: int | None = None,
):
    async def work(run: Run):
        from blastjob.core.refine import run_refine

        if not artifact_dir:
            run.emit("error", "No packet to revise — tailor this role first.")
            return None
        run_dir = Path(artifact_dir).expanduser()
        if not run_dir.is_dir():
            run.emit("error", f"Packet folder is gone: {run_dir}")
            return None

        cfg = load_config()
        run.emit("log", f"Revising the resume in {run_dir.name}...")
        try:
            new_path, fit = await run_refine(
                run_dir, feedback, cfg, CostTracker(), on_text=lambda t: run.emit("output", t)
            )
        except FileNotFoundError as exc:
            run.emit("error", str(exc))
            return None
        if job_id is not None:
            _register(cfg, job_id, run_dir, artifact_dir)
        score = getattr(fit, "groundedness_score", None)
        run.emit(
            "log",
            f"Saved {new_path.name}"
            + (f" — groundedness now {score}" if score is not None else ""),
        )
        return {"path": str(new_path), "groundedness": score}

    return work

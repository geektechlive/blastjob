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

from blastjob import config as cfg_mod
from blastjob.core import filing_lock
from blastjob.core.paths import locate
from blastjob.llm.cost import CostTracker, describe_cost
from blastjob.web.runtime import Run

log = logging.getLogger(__name__)


def _register(cfg, job_id: int, artifact_dir: str) -> None:
    """Index this job's files in the packet folder after a revision. A re-scan
    by the role's naming rules, not refine's own pick of "newest resume", so
    another role's files in a shared folder are never attributed here. The
    folder is located afresh: the caller holds the shared filing lock (the
    refine's claim is already released). Best-effort: never fails the
    revision."""
    from blastjob.core import artifacts
    from blastjob.db import client as dbc

    root = cfg_mod.output_dir(cfg)
    try:
        conn = dbc.init_db(cfg)
    except Exception:  # noqa: BLE001
        log.warning("artifact registration skipped for job %s", job_id, exc_info=True)
        return
    try:
        job = conn.execute("SELECT company, title FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return
        run_dir = locate(artifact_dir, root)
        files = artifacts.role_files(
            run_dir, company=job["company"] or "", role=job["title"] or "", job_id=job_id
        )
        if not files:
            log.warning("refine: no files matched job %s's role in %s", job_id, run_dir)
        run_pk = artifacts.run_for_dir(conn, job_id, artifact_dir, root)
        artifacts.register_paths(conn, files, job_id=job_id, build_run_id=run_pk, output_root=root)
    except Exception:  # noqa: BLE001
        log.warning("artifact registration failed for job %s", job_id, exc_info=True)
    finally:
        conn.close()


def _refile(cfg, job_id: int) -> None:
    """Re-file the job's packets after a revision (issue #20). Best-effort."""
    from blastjob.core import packet_filing
    from blastjob.db import client as dbc

    try:
        conn = dbc.init_db(cfg)
    except Exception:  # noqa: BLE001
        log.warning("refine: filing skipped for job %s", job_id, exc_info=True)
        return
    try:
        packet_filing.file_after_change(conn, cfg_mod.output_dir(cfg), job_id)
    finally:
        conn.close()


def _job_identity(cfg, job_id: int | None) -> tuple[str, str, str] | None:
    """(company, title, build_state) of the job being revised, or None.

    The role comes from the JOB, never from "the newest resume in the folder": a
    current-layout folder is shared by every role at that company that day."""
    if job_id is None:
        return None
    from blastjob.db import client as dbc

    try:
        conn = dbc.init_db(cfg)
    except Exception:  # noqa: BLE001
        log.warning("refine: could not look up job %s", job_id, exc_info=True)
        return None
    try:
        row = conn.execute(
            "SELECT company, title, build_state FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return row["company"] or "", row["title"] or "", row["build_state"] or ""


def _claim(cfg, job_id: int, run_id: str) -> bool:
    """Take the job's DB claim (`db.client.claim_for_refine`), so a tailor started
    meanwhile is refused instead of racing this revision's publish."""
    from blastjob.db import client as dbc

    try:
        conn = dbc.init_db(cfg)
    except Exception:  # noqa: BLE001
        log.warning("refine: could not claim job %s", job_id, exc_info=True)
        return False
    try:
        return dbc.claim_for_refine(conn, job_id, run_id)
    finally:
        conn.close()


def _release(cfg, run_id: str) -> None:
    from blastjob.db import client as dbc

    try:
        conn = dbc.init_db(cfg)
    except Exception:  # noqa: BLE001
        log.warning("refine: could not release claim %s", run_id, exc_info=True)
        return
    try:
        dbc.release_refine(conn, run_id)
    except Exception:  # noqa: BLE001
        log.warning("refine: could not release claim %s", run_id, exc_info=True)
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
        from blastjob.core.packet_verify import PacketNotQuarantinedError
        from blastjob.core.refine import run_refine
        from blastjob.core.refine_files import RefineBusyError

        if not artifact_dir:
            run.emit("error", "No packet to revise — tailor this role first.")
            return None
        cfg = load_config()
        root = cfg_mod.output_dir(cfg)
        # Where the packet is now: filing may have moved it into a bucket.
        run_dir = locate(artifact_dir, root)
        if not run_dir.is_dir():
            run.emit("error", f"Packet folder is gone: {run_dir}")
            return None

        identity = _job_identity(cfg, job_id)
        if job_id is not None and identity is None:
            # Without the job's own company and role the only way to pick a resume
            # is "the newest one in the folder", which in a shared company-day
            # folder can be another role's. Refuse rather than guess.
            run.emit("error", "Could not look up this job, so its resume cannot be picked safely.")
            return None
        if identity is not None and identity[2] == "tailoring":
            # A tailor in flight is writing this very folder.
            run.emit("error", "A tailoring build is running for this job; revise after it ends.")
            return None
        who = {"company": identity[0], "role": identity[1]} if identity else {}
        if job_id is not None and not _claim(cfg, job_id, run.id):
            run.emit(
                "error", "A tailor or another revision holds this job; try again after it ends."
            )
            return None
        # Located again, under the shared lock, now that the claim exists: its
        # running build_runs row keeps the filer off this job's entries while
        # the model streams, so the lock is never held across an LLM call.
        async with filing_lock.shared_async(root):
            run_dir = locate(artifact_dir, root)
        run.emit("log", f"Revising the resume in {run_dir.name}...")
        tracker = CostTracker()
        try:
            new_path, fit = await run_refine(
                run_dir,
                feedback,
                cfg,
                tracker,
                on_text=lambda t: run.emit("output", t),
                **who,
            )
        except (FileNotFoundError, RefineBusyError) as exc:
            run.emit("error", str(exc))
            return None
        except PacketNotQuarantinedError as exc:
            run.emit("error", f"{exc} The revision was NOT accepted; do not send this packet.")
            return None
        finally:
            if job_id is not None:
                _release(cfg, run.id)
        if job_id is not None:
            async with filing_lock.shared_async(root):
                _register(cfg, job_id, artifact_dir)
            _refile(cfg, job_id)
        score = getattr(fit, "groundedness_score", None)
        run.emit(
            "log",
            f"Saved {new_path.name}"
            + (f" — groundedness now {score}" if score is not None else " — not re-scored")
            + f". Cost {describe_cost(tracker.total_call_cost)}.",
        )
        if new_path.parent.name == "quarantine":
            run.emit(
                "error",
                "The revision failed the packet checks and is held in quarantine; "
                "see the packet report for why.",
            )
        return {"path": str(new_path), "groundedness": score}

    return work

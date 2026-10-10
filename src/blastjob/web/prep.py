"""Background Run wrapper around the interview prep pack builder.

Mirrors `make_research_work`: open the config + db, stream the model's output to
the browser as `output` events, and log where the file landed.
"""

from __future__ import annotations

from blastjob import config as cfg_mod
from blastjob.db import client as dbc
from blastjob.web import service
from blastjob.web.runtime import Run


def make_prep_work(job_id: int, load_config=cfg_mod.load_config):
    async def work(run: Run):
        from blastjob.core.prep import generate_prep

        cfg = load_config()
        conn = dbc.init_db(cfg)
        try:
            job = service.job_detail(conn, job_id)
            if job is None:
                run.emit("error", "job not found")
                return None
            if dbc.is_jd_snippet(job["raw_jd"], job["jd_pasted_at"]):
                # The JD-to-STAR table and likely questions would read as fact
                # about a posting we have only seen one paragraph of.
                run.emit(
                    "log",
                    "No prep pack: we only have a snippet of this job description. "
                    "Paste the full description on the job page first.",
                )
                return None
            run.emit("log", f"Preparing interview pack for {job['title']} @ {job['company']}...")
            path = await generate_prep(cfg, conn, job_id, on_text=lambda t: run.emit("output", t))
            run.emit("log", f"Saved {path}")
            return {"path": str(path)}
        finally:
            conn.close()

    return work

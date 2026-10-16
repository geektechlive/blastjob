"""Background re-score after a pasted JD (issue #4).

Runs under the same `score-job` name as a manually added job, so the global run
panel (app.BANNER_RUNS) shows it with no second progress system, and it is not
single-flight, so it still runs while a full matching pass is in flight. If it
fails, an undecided row's score stays NULL and the daily `only_unscored` pass
picks it up; a kept row keeps its previous score (see `dbc.set_pasted_jd`).
The route starts it only when the pasted text actually changed.
"""

from __future__ import annotations

from collections.abc import Callable

from blastjob.db import client as dbc
from blastjob.llm.cost import CostTracker
from blastjob.matching import rescore
from blastjob.models.config import BlastJobConfig
from blastjob.web.manual_job import MANUAL_SCORE_RUN
from blastjob.web.runtime import RUNTIME, Run


async def _work(run: Run, load_config: Callable[[], BlastJobConfig], job_id: int) -> dict:
    cfg = load_config()
    conn = dbc.init_db(cfg)
    try:
        run.emit("log", f"Re-scoring job {job_id} against the pasted description...")
        scored = await rescore.rescore_job(cfg, conn, CostTracker(), job_id)
        run.emit("log", f"Scored {scored}.")
        return {"scored": scored}
    finally:
        conn.close()


def start_rescore(load_config: Callable[[], BlastJobConfig], job_id: int) -> Run:
    return RUNTIME.start(MANUAL_SCORE_RUN, lambda run: _work(run, load_config, job_id))

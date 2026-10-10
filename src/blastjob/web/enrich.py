"""Background wrapper for the LinkedIn enrichment pass.

Same shape as web/prep.py. It runs rarely -- the import is
infrequent, so the cost is fine -- but it is ~28 batched calls over 1,100
distinct titles, which is long enough that it needs visible progress rather
than a spinner.
"""

from __future__ import annotations

from blastjob import config as cfg_mod
from blastjob.core import network_enrich as ne
from blastjob.db import client as dbc
from blastjob.llm.cost import CostTracker
from blastjob.web import progress as progress_mod
from blastjob.web.runtime import Run


def make_enrich_work(load_config=cfg_mod.load_config, limit: int | None = None):
    async def work(run: Run):
        cfg = cfg_mod.load_config() if load_config is None else load_config()
        conn = dbc.init_db(cfg)
        try:
            pending = ne.pending_pairs(conn)
            if not pending:
                run.emit("log", "Every title is already classified — nothing to do.")
                return {"pairs": 0}
            run.emit(
                "log",
                f"Classifying {len(pending)} distinct title/company pairs "
                f"(titles only — no names are sent).",
            )

            def on_progress(batch_idx: int, batch_count: int) -> None:
                # Item counts, not batch counts: "480 / 1,333 titles" is what the
                # user actually wants to see on a pass this long, and `pending` is
                # already known before it starts.
                done = min(batch_idx * ne.BATCH_SIZE, len(pending))
                run.emit(
                    "progress",
                    progress_mod.format_payload(
                        done, len(pending), label=f"batch {batch_idx}/{batch_count}"
                    ),
                )

            res = await ne.run_enrichment(
                cfg, conn, CostTracker(), on_progress=on_progress, limit=limit
            )
            summary = (
                f"Classified {res.pairs_classified} of {res.pairs_sent} pairs "
                f"across {res.batches} batches; {res.contacts_updated} contacts updated."
            )
            if res.failed_batches:
                summary += f" {res.failed_batches} batch(es) failed and stay pending."
            run.emit("log", summary)
            return {
                "pairs": res.pairs_classified,
                "contacts": res.contacts_updated,
                "failed": res.failed_batches,
            }
        finally:
            conn.close()

    return work

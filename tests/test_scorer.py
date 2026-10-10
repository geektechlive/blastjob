"""The alignment scorer must not silently swallow a failed batch.

A batch whose LLM call always raises should be counted in `failed_batches` (so the
web layer can surface it), not vanish into a smaller "scored N".
"""

from __future__ import annotations

from blastjob.llm.cost import CostTracker
from blastjob.matching import scorer
from blastjob.matching.scorer import ScoringResult, score_candidates
from blastjob.models.config import BlastJobConfig


async def test_failed_batch_is_counted_not_silent(monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(scorer, "make_stream", boom)

    candidates = [{"job_id": 1, "title": "VP Ops", "company": "Acme", "jd": "Lead Ops."}]
    res = await score_candidates(BlastJobConfig(), "history", candidates, CostTracker())

    assert isinstance(res, ScoringResult)
    assert res.scores == {}  # nothing scored
    assert res.failed_batches == 1  # but the loss is reported, not hidden


async def test_empty_candidates_no_failures(monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("should not be called")

    monkeypatch.setattr(scorer, "make_stream", boom)

    res = await score_candidates(BlastJobConfig(), "history", [], CostTracker())
    assert res.scores == {} and res.failed_batches == 0

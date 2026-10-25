"""Once the deadline refuses a call, the scorer makes no further attempts (#95)."""

from __future__ import annotations

import asyncio

from blastjob.llm import gateway
from blastjob.llm.cost import CostTracker
from blastjob.matching import scorer
from blastjob.models.config import BlastJobConfig


def _candidates(n: int) -> list[dict]:
    return [
        {"job_id": i, "title": "VP", "company": f"Co{i}", "jd": "Lead a team. " * 20}
        for i in range(1, n + 1)
    ]


def test_no_attempt_follows_the_first_refused_call(monkeypatch):
    calls: list[int] = []

    async def refusing(*_a, **_k):
        calls.append(1)
        gateway.call_limit(900.0)  # raises DeadlineReached and marks the scope as cut
        raise AssertionError("unreachable")

    monkeypatch.setattr(scorer, "make_stream", refusing)

    async def go() -> scorer.ScoringResult:
        with gateway.deadline_scope(gateway.MIN_START_SECONDS - 1):
            return await scorer.score_candidates(
                BlastJobConfig(), "history", _candidates(6), CostTracker(), batch_size=2
            )

    out = asyncio.run(go())

    assert calls == [1]  # one refused call: no retry, no later batch, no per-job fallback
    assert out.scores == {} and out.failed_batches == 0  # a refused batch is not a failed one

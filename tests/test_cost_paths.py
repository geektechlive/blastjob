"""Cost surfaced on the model paths #30 left out (issue #71): the daily employer-JD
re-score pass and the cockpit enrich and ingest runs. Docs: docs/how-it-works/llm.md."""

from __future__ import annotations

import asyncio

import pytest

from blastjob import daily, daily_employer_jd
from blastjob.core import employer_jd
from blastjob.llm.cost import CallCost, CostTracker
from blastjob.matching import rescore
from blastjob.matching.rescore import RescoreCounts
from blastjob.models.config import BlastJobConfig
from blastjob.web import enrich as web_enrich
from blastjob.web import routes_settings


class _Run:
    def __init__(self) -> None:
        self.logs: list[str] = []

    def emit(self, kind: str, data: str) -> None:
        if kind == "log":
            self.logs.append(data)


def _cfg(tmp_path) -> BlastJobConfig:
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    return cfg


def _lookup(monkeypatch, tracker_calls: list[CallCost]) -> daily.DailyResult:
    async def fake_upgrade(*_a, **_k):
        return employer_jd.UpgradeResult(upgraded=[1])

    async def fake_rescore(_cfg, _conn, tracker: CostTracker, _ids):
        for c in tracker_calls:
            tracker.record(c)
        return RescoreCounts(kept=1)

    monkeypatch.setattr(employer_jd, "run_upgrade", fake_upgrade)
    monkeypatch.setattr(rescore, "rescore_jobs", fake_rescore)
    res = daily.DailyResult()
    asyncio.run(
        daily_employer_jd.lookup_step(
            BlastJobConfig(),
            None,  # type: ignore[arg-type]
            res,
            None,
            None,  # type: ignore[arg-type]
            free=None,
            dry_run=False,
            score=True,
        )
    )
    return res


def test_daily_total_includes_the_employer_jd_rescore_pass(monkeypatch):
    res = _lookup(monkeypatch, [CallCost(cost_usd=0.02), CallCost(cost_usd=0.03)])
    res.scoring_cost = CallCost(cost_usd=0.1)
    res.scoring_batches = 1

    assert res.rescore_cost.cost_usd == pytest.approx(0.05)
    assert res.total_cost.cost_usd == pytest.approx(0.15)
    assert "cost $0.1500" in daily.format_run_footer(res)


def test_daily_total_is_unknown_when_a_rescore_call_was_unknown(monkeypatch):
    res = _lookup(monkeypatch, [CallCost(cost_usd=0.02), CallCost(known=False)])
    res.scoring_cost = CallCost(cost_usd=0.1)
    res.scoring_batches = 1

    assert res.total_cost.known is False
    assert "cost unknown" in daily.format_run_footer(res)


def test_footer_shows_rescore_cost_even_when_nothing_was_scored(monkeypatch):
    res = _lookup(monkeypatch, [CallCost(cost_usd=0.02)])

    assert "cost $0.0200" in daily.format_run_footer(res)


def test_footer_has_no_cost_when_no_model_call_ran():
    assert "cost" not in daily.format_run_footer(daily.DailyResult(duration_s=5, fetched=1))


def test_enrich_run_log_reports_cost(monkeypatch, tmp_path):
    async def fake_enrichment(_cfg, _conn, tracker: CostTracker, **_k):
        tracker.record(CallCost(cost_usd=0.0123))

        class _Res:
            pairs_classified = pairs_sent = 1
            batches = 1
            contacts_updated = 0
            failed_batches = 0

        return _Res()

    monkeypatch.setattr(web_enrich.ne, "pending_pairs", lambda _c: [("Chief", "Acme")])
    monkeypatch.setattr(web_enrich.ne, "run_enrichment", fake_enrichment)
    run = _Run()

    asyncio.run(web_enrich.make_enrich_work(lambda: _cfg(tmp_path))(run))  # type: ignore[arg-type]

    assert any("cost $0.0123" in line.lower() for line in run.logs)


def test_ingest_run_log_reports_unknown_cost(monkeypatch, tmp_path):
    async def fake_ingestion(path, cfg, tracker: CostTracker, on_text=None):
        tracker.record(CallCost(cost_usd=0.01))
        tracker.record(CallCost(known=False))
        return tmp_path / "work_history.md"

    monkeypatch.setattr("blastjob.core.ingest.run_ingestion", fake_ingestion)
    run = _Run()
    spool = tmp_path / "spool"
    spool.mkdir()

    work = routes_settings._make_ingest_work(spool, lambda: _cfg(tmp_path))
    asyncio.run(work(run))  # type: ignore[arg-type]

    assert any("cost unknown" in line.lower() for line in run.logs)

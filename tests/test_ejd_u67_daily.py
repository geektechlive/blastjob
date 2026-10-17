"""Employer-JD units U6 + U7 (issue #17): the backfill CLI flags and the daily-run steps.

Discovery, matching, closure and the LLM are stubbed; the upgrade module runs for
real against an injected `Net` double. Nothing here touches the network.
"""

from __future__ import annotations

import argparse
import tempfile
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import tests.geo_fixture as geo_fixture
from blastjob import daily, daily_tools
from blastjob.core import closure, closure_links
from blastjob.core import employer_jd as ej
from blastjob.core.closure import ClosureResult
from blastjob.db import client as dbc
from blastjob.discovery.pipeline import DiscoveryResult
from blastjob.models.config import BlastJobConfig, DiscoveryConfig
from tests.test_ejd_u4_upgrade import JD, SNIPPET, Net, _dump, _job, _pair, _row


@pytest.fixture
def cfg():
    c = BlastJobConfig()
    geo_fixture.apply_region(c)
    c.paths.data_dir = tempfile.mkdtemp()
    c.paths.output_dir = tempfile.mkdtemp()
    c.candidate.target_titles = ["VP of Operations"]
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


class Spy:
    """Shared call log for the stubbed pipeline stages."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.matching_jd: list[str] = []
        self.closure_deps: list[object] = []
        self.upgrades: list[dict] = []
        self.llm_jobs: list[int] = []


@pytest.fixture
def spy(monkeypatch, conn):
    s = Spy()

    async def fake_discovery(cfg, conn, adapters=None):
        s.log.append("discovery")
        return DiscoveryResult()

    async def fake_matching(cfg, conn, ct, *, job_ids=None, **kw):
        s.log.append("matching")
        rows = conn.execute("SELECT raw_jd FROM jobs WHERE source='adzuna'").fetchall()
        s.matching_jd = [r["raw_jd"] for r in rows]
        return SimpleNamespace(
            scored=0, scoring_failures=0, notes=[], surfaced=0, queued=0, job_ids=job_ids
        )

    async def fake_closure(cfg, conn, *, deps=None, shadow=False, now=None):
        s.log.append("closure")
        s.closure_deps.append(deps)
        return ClosureResult()

    async def fake_score(cfg, history, jobs, ct, **kw):
        s.llm_jobs += [j["job_id"] for j in jobs]
        scores = {
            j["job_id"]: {"alignment": 91, "rationale": "r", "strengths": [], "gaps": []}
            for j in jobs
        }
        return SimpleNamespace(scores=scores)

    real_upgrade = ej.run_upgrade

    async def spying_upgrade(*a, **kw):
        s.log.append("upgrade-net" if kw.get("network") else "upgrade-free")
        s.upgrades.append(kw)
        return await real_upgrade(*a, **kw)

    monkeypatch.setattr("blastjob.discovery.pipeline.run_discovery", fake_discovery)
    monkeypatch.setattr("blastjob.matching.pipeline.run_matching", fake_matching)
    monkeypatch.setattr(closure, "run_closure", fake_closure)
    monkeypatch.setattr("blastjob.matching.rescore.score_candidates", fake_score)
    monkeypatch.setattr(ej, "run_upgrade", spying_upgrade)
    return s


async def _noop_send(_text: str) -> None:
    return None


def _use_net(monkeypatch, net: Net) -> None:
    monkeypatch.setattr(closure_links, "default_deps", net.deps)


# --- settings ----------------------------------------------------------------------


def test_employer_jd_settings_default_off_and_validate():
    d = DiscoveryConfig()
    assert d.employer_jd_lookup is False
    assert d.employer_jd_min_score == 60
    assert d.employer_jd_daily_requests == 300
    with pytest.raises(ValidationError):
        DiscoveryConfig(employer_jd_min_score=101)
    with pytest.raises(ValidationError):
        DiscoveryConfig(employer_jd_min_score=-1)


# --- U6: backfill CLI ----------------------------------------------------------------


def _parse(*argv: str) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    daily_tools.add_arguments(ap)
    return ap.parse_args(list(argv))


def test_flags_parse_with_defaults():
    a = _parse("--employer-jd-report")
    assert a.employer_jd_report and not a.employer_jd
    assert a.min_score is None and a.max_requests == 1000
    b = _parse("--employer-jd", "--min-score", "70", "--max-requests", "5")
    assert b.employer_jd and b.min_score == 70 and b.max_requests == 5


@pytest.mark.asyncio
async def test_report_writes_nothing(cfg, conn):
    _pair(conn, review="kept", score=50)
    _job(conn, ext="x", company="Acme", title="CIO", review="kept")
    before = _dump(conn)
    net = Net()
    out = await daily_tools.employer_jd_cli(
        cfg, conn, apply=False, min_score=None, max_requests=50, deps=net.deps()
    )
    assert "report only" in out
    assert "would_upgrade" in out
    assert _dump(conn) == before


@pytest.mark.asyncio
async def test_apply_stores_and_rescores(cfg, conn, spy):
    agg, _board = _pair(conn, review="kept", score=50)
    out = await daily_tools.employer_jd_cli(
        cfg, conn, apply=True, min_score=None, max_requests=50, deps=Net().deps()
    )
    row = _row(conn, agg)
    assert row["raw_jd"] == JD.strip()
    assert row["alignment_score"] == 91 and row["score_stale"] == 0
    assert agg in spy.llm_jobs
    assert "requests used" in out and "re-scored" in out and "cost" in out.lower()


def test_dispatch_routes_flags(cfg, conn, monkeypatch):
    seen = {}

    async def fake(cfg, conn, *, apply, min_score, max_requests, deps=None):
        seen.update(apply=apply, min=min_score, cap=max_requests)
        return "REPORT"

    monkeypatch.setattr(daily_tools, "employer_jd_cli", fake)
    assert daily_tools.dispatch(_parse("--employer-jd-report"), cfg, conn) == "REPORT"
    assert seen == {"apply": False, "min": None, "cap": 1000}
    assert daily_tools.dispatch(_parse("--employer-jd", "--max-requests", "9"), cfg, conn) == (
        "REPORT"
    )
    assert seen["apply"] is True and seen["cap"] == 9


# --- U7: daily run -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_free_copy_happens_before_matching(cfg, conn, spy):
    agg, _ = _pair(conn, review="new")
    await daily.run_daily(cfg, conn, send=_noop_send)
    assert spy.log.index("upgrade-free") < spy.log.index("matching")
    assert spy.matching_jd == [JD.strip()]  # matching saw the copied full text, once
    assert _row(conn, agg)["raw_jd"] == JD.strip()


@pytest.mark.asyncio
async def test_network_step_runs_after_closure_with_shared_deps(cfg, conn, spy, monkeypatch):
    cfg.discovery.employer_jd_lookup = True
    _job(conn, ext="x", company="Acme", title="VP of Ops", review="kept")
    net = Net(hits=False)
    _use_net(monkeypatch, net)
    await daily.run_daily(cfg, conn, send=_noop_send)
    assert spy.log == ["discovery", "upgrade-free", "matching", "closure", "upgrade-net"]
    shared = spy.closure_deps[0]
    assert shared is not None
    assert spy.upgrades[-1]["deps"] is shared
    assert spy.upgrades[-1]["write"] is True
    assert spy.upgrades[-1]["request_limit"] == 300 and spy.upgrades[-1]["min_score"] == 60
    assert net.probes  # the network step really looked


@pytest.mark.asyncio
async def test_lookup_off_never_touches_the_network(cfg, conn, spy, monkeypatch):
    _job(conn, ext="x", company="Acme", title="VP of Ops", review="kept")
    net = Net()
    _use_net(monkeypatch, net)
    await daily.run_daily(cfg, conn, send=_noop_send)
    assert net.calls == []
    assert spy.upgrades[-1]["network"] is False


@pytest.mark.asyncio
async def test_kept_row_with_free_copy_is_rescored(cfg, conn, spy):
    agg, _ = _pair(conn, review="kept", score=50)
    res = await daily.run_daily(cfg, conn, send=_noop_send)
    row = _row(conn, agg)
    assert row["raw_jd"] == JD.strip() and row["alignment_score"] == 91 and row["score_stale"] == 0
    assert "stored" in res.notes["employer_jd"] and "Re-scored 1" in res.notes["employer_jd"]


@pytest.mark.asyncio
async def test_closure_reverted_rows_are_rescored(cfg, conn, spy, monkeypatch):
    kept = _job(conn, ext="k", review="kept", score=70)

    async def fake_closure(cfg, conn, *, deps=None, shadow=False, now=None):
        return ClosureResult(reverted_jd=[kept])

    monkeypatch.setattr(closure, "run_closure", fake_closure)
    await daily.run_daily(cfg, conn, send=_noop_send)
    assert kept in spy.llm_jobs


@pytest.mark.asyncio
async def test_dry_run_writes_no_jd_and_makes_no_llm_call(cfg, conn, spy):
    agg, _ = _pair(conn, review="kept", score=50)
    res = await daily.run_daily(cfg, conn, send=_noop_send, dry_run=True)
    row = _row(conn, agg)
    assert row["raw_jd"] == SNIPPET and row["alignment_score"] == 50
    assert spy.llm_jobs == []
    assert "would store" in res.notes["employer_jd"]
    assert spy.upgrades and all(u["write"] is False for u in spy.upgrades)


@pytest.mark.asyncio
async def test_a_raising_step_becomes_an_error_and_the_run_finishes(cfg, conn, spy, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ej, "run_upgrade", boom)
    res = await daily.run_daily(cfg, conn, send=_noop_send)
    assert "kaboom" in res.errors["employer_jd"]
    assert res.duration_s > 0
    assert "closure" in spy.log  # the rest of the run still happened

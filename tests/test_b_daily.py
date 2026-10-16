"""B5: `blastjob-daily --dry-run` must not run a live closure pass; and a
discovery note (e.g. a snapshot guard) reaches the digest instead of dying in
DiscoveryResult."""

from __future__ import annotations

import datetime as dt
import tempfile

import pytest

from blastjob import daily
from blastjob.core import closure
from blastjob.core.closure import ClosureResult
from blastjob.db import client as dbc
from blastjob.discovery import pipeline
from blastjob.matching import pipeline as mpipe
from blastjob.matching.pipeline import MatchResult
from blastjob.models.config import BlastJobConfig


@pytest.fixture
def cfg():
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _stub(monkeypatch, notes=None):
    seen: dict[str, object] = {}

    async def fake_discovery(cfg, conn, adapters=None):
        return pipeline.DiscoveryResult(notes=dict(notes or {}))

    async def fake_matching(cfg, conn, tracker, on_progress=None, **kw):
        return MatchResult()

    async def fake_closure(cfg, conn, *, deps=None, shadow=False, now=None):
        seen["shadow"] = shadow
        return ClosureResult()

    monkeypatch.setattr(pipeline, "run_discovery", fake_discovery)
    monkeypatch.setattr(mpipe, "run_matching", fake_matching)
    monkeypatch.setattr(closure, "run_closure", fake_closure)
    return seen


async def _nosend(_text):
    return None


@pytest.mark.asyncio
async def test_dry_run_runs_closure_in_shadow(cfg, conn, monkeypatch):
    seen = _stub(monkeypatch)
    await daily.run_daily(cfg, conn, send=_nosend, dry_run=True, today=dt.date(2026, 9, 8))
    assert seen["shadow"] is True


@pytest.mark.asyncio
async def test_real_run_closure_is_not_shadow(cfg, conn, monkeypatch):
    seen = _stub(monkeypatch)
    await daily.run_daily(cfg, conn, send=_nosend, today=dt.date(2026, 9, 8))
    assert seen["shadow"] is False


@pytest.mark.asyncio
async def test_discovery_notes_reach_the_digest(cfg, conn, monkeypatch):
    _stub(monkeypatch, notes={"greenhouse:acme": "snapshot returned 0 postings; skipped"})
    sent: list[str] = []

    async def send(text):
        sent.append(text)

    res = await daily.run_daily(cfg, conn, send=send, today=dt.date(2026, 9, 8))
    assert res.notes == {"greenhouse:acme": "snapshot returned 0 postings; skipped"}
    assert res.sent and "greenhouse:acme" in sent[0] and "skipped" in sent[0]


def test_digest_with_only_a_note_is_not_empty():
    assert daily.build_digest([], {}, [], notes={"x": "y"}) != ""
    assert daily.build_digest([], {}, []) == ""

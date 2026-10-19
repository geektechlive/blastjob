"""`blastjob-daily --file-packets[-report]` and the daily run's filing pass
(issue #20). Invented data only."""

from __future__ import annotations

import argparse
import hashlib
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from blastjob import daily, daily_filing, daily_tools
from blastjob.core.paths import SUBMITTED
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig


@pytest.fixture
def env():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    cfg.paths.output_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)
    jid = dbc.upsert_job(conn, source="s", external_id="e1", company="Acme", title="VP IT")
    conn.execute(
        "UPDATE jobs SET review_state='kept', application_state='applied', "
        "applied_at='2026-09-01' WHERE id=?",
        (jid,),
    )
    folder = Path(cfg.paths.output_dir) / "acme-2026-09-01"
    folder.mkdir()
    (folder / "vp-it.metadata.json").write_text("{}")
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO build_runs (job_id, run_id, status, lease_expires_at, artifact_dir, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (jid, "r1", "done", now, str(folder), now, now),
    )
    conn.commit()
    yield cfg, conn, folder
    conn.close()


def _args(*flags: str) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    daily_tools.add_arguments(ap)
    return ap.parse_args(list(flags))


def _snapshot(cfg, conn) -> str:
    root = Path(cfg.paths.output_dir)
    listing = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    return hashlib.sha256(("\n".join(listing) + "\n".join(conn.iterdump())).encode()).hexdigest()


def test_report_flag_prints_the_plan_and_writes_nothing(env) -> None:
    cfg, conn, folder = env
    before = _snapshot(cfg, conn)
    text = daily_tools.dispatch(_args("--file-packets-report"), cfg, conn)
    assert text is not None
    assert "Would move 1 entry" in text and "acme-2026-09-01" in text
    assert "Would rename 0" in text
    assert _snapshot(cfg, conn) == before
    assert folder.is_dir()


def test_apply_flag_files_the_entry(env) -> None:
    cfg, conn, folder = env
    text = daily_tools.dispatch(_args("--file-packets"), cfg, conn)
    assert text is not None and "Moved 1 entry" in text
    assert (Path(cfg.paths.output_dir) / SUBMITTED / folder.name).is_dir()


def test_daily_pass_dry_run_moves_nothing_real_run_files(env) -> None:
    cfg, conn, folder = env
    daily_filing.filing_pass(cfg, conn, dry_run=True)
    assert folder.is_dir()
    daily_filing.filing_pass(cfg, conn, dry_run=False)
    assert not folder.exists()


@pytest.mark.asyncio
async def test_run_daily_files_after_closure(env, monkeypatch) -> None:
    cfg, conn, folder = env
    calls: list[bool] = []
    monkeypatch.setattr(daily_filing, "filing_pass", lambda c, k, *, dry_run: calls.append(dry_run))

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(daily, "_closure_pass", nothing)
    monkeypatch.setattr(daily, "_replies_pass", nothing)
    monkeypatch.setattr(daily.daily_employer_jd, "lookup_step", nothing)
    await daily.run_daily(cfg, conn, send=nothing, score=False, dry_run=True)
    assert calls == [True]

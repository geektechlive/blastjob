"""Round 2, R1/R5: a refine and a tailor of the same job exclude each other
durably (the DB claim), and a job that cannot be identified is never refined by
guessing "the newest resume in the folder"."""

from __future__ import annotations

from pathlib import Path

import pytest

from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig, PathsConfig
from blastjob.web import refine as web_refine
from blastjob.web.runtime import Run


def _setup(tmp_path: Path, build_state: str = "none"):
    # "none" is where a Re-tailor's reset leaves a job, i.e. claimable by a tailor.
    data = tmp_path / "data"
    data.mkdir()
    run_dir = tmp_path / "out" / "acme-2026-10-16"
    run_dir.mkdir(parents=True)
    cfg = BlastJobConfig(paths=PathsConfig(data_dir=str(data), output_dir=str(tmp_path / "out")))
    conn = dbc.init_db(cfg)
    jid = dbc.upsert_job(conn, source="s", external_id="e", title="VP IT", company="Acme")
    conn.execute("UPDATE jobs SET build_state=? WHERE id=?", (build_state, jid))
    conn.commit()
    return cfg, conn, jid, run_dir


async def test_a_tailor_cannot_claim_the_job_while_a_refine_runs(tmp_path, monkeypatch):
    cfg, conn, jid, run_dir = _setup(tmp_path)
    seen: dict = {}

    async def fake_refine(run_dir_, feedback, cfg_, tracker, **kw):
        other = dbc.init_db(cfg)
        try:
            seen["tailor_claim_during"] = dbc.claim_for_build(other, jid, "tailor-run")
        finally:
            other.close()
        return run_dir_ / "x.md", None

    monkeypatch.setattr("blastjob.core.refine.run_refine", fake_refine)
    work = web_refine.make_refine_work(str(run_dir), "tighten", lambda: cfg, job_id=jid)

    await work(Run(id="r1", name="refine"))

    assert seen["tailor_claim_during"] is False
    # Released afterwards: a tailor may now claim.
    assert dbc.claim_for_build(conn, jid, "tailor-run-2") is True


async def test_the_refine_claim_is_released_when_the_refine_raises(tmp_path, monkeypatch):
    cfg, conn, jid, run_dir = _setup(tmp_path)

    async def boom(*_a, **_kw):
        raise RuntimeError("model down")

    monkeypatch.setattr("blastjob.core.refine.run_refine", boom)
    work = web_refine.make_refine_work(str(run_dir), "tighten", lambda: cfg, job_id=jid)

    with pytest.raises(RuntimeError):
        await work(Run(id="r1", name="refine"))

    assert dbc.claim_for_build(conn, jid, "tailor-run") is True


async def test_a_refine_is_refused_while_a_tailor_holds_the_claim(tmp_path, monkeypatch):
    cfg, conn, jid, run_dir = _setup(tmp_path, build_state="none")
    assert dbc.claim_for_build(conn, jid, "tailor-run") is True

    async def must_not_run(*_a, **_kw):
        raise AssertionError("refine must not start during a tailor")

    monkeypatch.setattr("blastjob.core.refine.run_refine", must_not_run)
    work = web_refine.make_refine_work(str(run_dir), "tighten", lambda: cfg, job_id=jid)

    assert await work(Run(id="r1", name="refine")) is None


async def test_two_refines_of_one_job_cannot_both_claim(tmp_path):
    cfg, conn, jid, _run_dir = _setup(tmp_path)

    assert dbc.claim_for_refine(conn, jid, "r1") is True
    assert dbc.claim_for_refine(conn, jid, "r2") is False
    dbc.release_refine(conn, "r1")
    assert dbc.claim_for_refine(conn, jid, "r2") is True


async def test_a_restart_clears_a_refine_claim_left_by_a_dead_process(tmp_path):
    cfg, conn, jid, _run_dir = _setup(tmp_path)
    assert dbc.claim_for_refine(conn, jid, "r1") is True
    conn.execute("UPDATE build_runs SET owner_token='some-dead-process'")
    conn.commit()

    dbc.recover_stale_builds(conn)

    assert dbc.claim_for_build(conn, jid, "tailor-run") is True
    # No failed "refine" row is left behind to masquerade as the latest build.
    assert (
        conn.execute("SELECT COUNT(*) FROM build_runs WHERE run_id LIKE 'refine:%'").fetchone()[0]
        == 0
    )


async def test_a_refine_with_a_job_id_but_no_resolvable_job_is_an_error(tmp_path, monkeypatch):
    cfg, _conn, _jid, run_dir = _setup(tmp_path)

    async def must_not_run(*_a, **_kw):
        raise AssertionError("must not fall back to a newest-mtime search")

    monkeypatch.setattr("blastjob.core.refine.run_refine", must_not_run)
    work = web_refine.make_refine_work(str(run_dir), "tighten", lambda: cfg, job_id=99999)
    run = Run(id="r1", name="refine")

    assert await work(run) is None
    assert any(e.kind == "error" for e in run.events)


async def test_a_lookup_crash_with_a_job_id_is_an_error_not_a_legacy_search(tmp_path, monkeypatch):
    cfg, _conn, jid, run_dir = _setup(tmp_path)

    def broken_init(_cfg):
        raise OSError("db locked")

    monkeypatch.setattr(dbc, "init_db", broken_init)

    async def must_not_run(*_a, **_kw):
        raise AssertionError("must not fall back to a newest-mtime search")

    monkeypatch.setattr("blastjob.core.refine.run_refine", must_not_run)
    work = web_refine.make_refine_work(str(run_dir), "tighten", lambda: cfg, job_id=jid)
    run = Run(id="r1", name="refine")

    assert await work(run) is None
    assert any(e.kind == "error" for e in run.events)

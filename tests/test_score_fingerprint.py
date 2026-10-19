"""Score versioning (issue #29).

A stored score is replayed for an unchanged posting only while everything that
shaped it is unchanged too: the scorer schema version, the scoring system
prompt, the candidate profile, `work_history.md`, the provider and its model.
Before this, the Match button replayed every old score after a profile or
work-history edit, so no band ever moved. The archive-reason calibration block
is deliberately NOT part of the fingerprint, and the daily run never treats an
old fingerprint as unscored.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from blastjob import config as cfg_mod
from blastjob.db import client as dbc
from blastjob.llm.cost import CostTracker
from blastjob.matching import pipeline, rescore
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from tests.geo_fixture import apply_region
from tests.profile_fixture import apply_profile

LONG_JD = "VP of Ops. Lead a team of 20+, own enterprise ops strategy. " + (
    "Own the roadmap, budget and vendor relationships. " * 12
)
_JOB_ID = re.compile(r"<JOB id=(\d+)>")


class _Usage:
    input_tokens = 0
    output_tokens = 0
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _Final:
    usage = _Usage()


class _Stream:
    def __init__(self, text: str):
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def text_stream(self):
        return self._iter()

    async def _iter(self):
        yield self._text

    async def get_final_message(self):
        return _Final()


class Recorder:
    """Stands in for `make_stream`: scores every job id in the prompt at 70."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, cfg, system, messages, max_tokens=4096, **kwargs):
        self.calls += 1
        prompt = "\n".join(b["text"] for b in messages[0]["content"])
        items = ",".join(
            f'{{"job_id": {j}, "alignment": 70, "rationale": "fresh", '
            f'"strengths": [], "gaps": [], "comp_min": 0, "comp_max": 0}}'
            for j in _JOB_ID.findall(prompt)
        )
        return _Stream(f"[{items}]")


@pytest.fixture
def cfg() -> BlastJobConfig:
    c = BlastJobConfig()
    apply_region(c)
    apply_profile(c)
    c.llm.provider = "anthropic"
    c.llm.anthropic_model = "model-a"
    c.paths.data_dir = tempfile.mkdtemp()
    cfg_mod.data_dir(c).mkdir(parents=True, exist_ok=True)
    (cfg_mod.data_dir(c) / "work_history.md").write_text(
        "Operations executive, 20 years.", encoding="utf-8"
    )
    return c


@pytest.fixture
def conn(cfg):
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(conn, ext: str = "e1", company: str = "Acme Widgets") -> int:
    return dbc.upsert_job(
        conn,
        source="t",
        external_id=ext,
        title="VP Ops",
        company=company,
        raw_jd=LONG_JD,
        geo_remote=1,
    )


def _match(cfg, conn, *, only_unscored: bool = False):
    return asyncio.run(pipeline.run_matching(cfg, conn, CostTracker(), only_unscored=only_unscored))


def _score_once(cfg, conn, monkeypatch) -> int:
    """Score one job for real (fake model), so it carries the current fingerprint."""
    jid = _job(conn)
    first = Recorder()
    monkeypatch.setattr("blastjob.matching.scorer.make_stream", first)
    _match(cfg, conn)
    assert first.calls == 1
    return jid


def _write_history(cfg, text: str) -> None:
    (cfg_mod.data_dir(cfg) / "work_history.md").write_text(text, encoding="utf-8")


def _change_schema(cfg, mp) -> None:
    mp.setattr("blastjob.matching.fingerprint.SCORER_SCHEMA_VERSION", "test-bump")


def _change_system_prompt(cfg, mp) -> None:
    mp.setattr(
        "blastjob.matching.scorer.SCORING_DETERMINISM_RULES",
        "\nA new rubric sentence.\n",
    )


def _change_profile(cfg, mp) -> None:
    cfg.candidate.field = "finance"


def _change_work_history(cfg, mp) -> None:
    _write_history(cfg, "Operations executive, 20 years. Added a new role.")


def _change_provider(cfg, mp) -> None:
    cfg.llm.provider = "openai"


def _change_model(cfg, mp) -> None:
    cfg.llm.anthropic_model = "model-b"


@pytest.mark.parametrize(
    "change",
    [
        _change_schema,
        _change_system_prompt,
        _change_profile,
        _change_work_history,
        _change_provider,
        _change_model,
    ],
    ids=["schema", "system_prompt", "profile", "work_history", "provider", "model"],
)
def test_changing_a_fingerprint_input_forces_a_fresh_score(
    cfg, conn, monkeypatch, change: Callable
):
    jid = _score_once(cfg, conn, monkeypatch)
    change(cfg, monkeypatch)
    second = Recorder()
    monkeypatch.setattr("blastjob.matching.scorer.make_stream", second)

    _match(cfg, conn)

    assert second.calls == 1, "an old score was replayed after its inputs changed"
    row = conn.execute("SELECT rationale FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["rationale"] == "fresh"


def test_an_unchanged_fingerprint_still_reuses_the_score(cfg, conn, monkeypatch):
    _score_once(cfg, conn, monkeypatch)
    second = Recorder()
    monkeypatch.setattr("blastjob.matching.scorer.make_stream", second)

    _match(cfg, conn)

    assert second.calls == 0


def test_a_new_calibration_archive_does_not_invalidate_scores(cfg, conn, monkeypatch):
    _score_once(cfg, conn, monkeypatch)
    other = _job(conn, ext="e2", company="Other Co")
    conn.execute(
        "UPDATE jobs SET review_state='archived', archive_reason='level', "
        "archive_note='too junior' WHERE id=?",
        (other,),
    )
    conn.commit()
    assert pipeline.load_rejections(conn), "the archive must reach the calibration block"
    second = Recorder()
    monkeypatch.setattr("blastjob.matching.scorer.make_stream", second)

    _match(cfg, conn)

    assert second.calls == 0


def test_the_daily_path_does_not_rescore_an_old_fingerprint(cfg, conn, monkeypatch):
    jid = _job(conn)
    conn.execute(
        "UPDATE jobs SET alignment_score=84, rationale='old', score_model_version='' WHERE id=?",
        (jid,),
    )
    conn.commit()
    rec = Recorder()
    monkeypatch.setattr("blastjob.matching.scorer.make_stream", rec)

    _match(cfg, conn, only_unscored=True)

    assert rec.calls == 0
    row = conn.execute("SELECT alignment_score FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["alignment_score"] == 84


def test_run_matching_writes_the_current_fingerprint(cfg, conn, monkeypatch):
    from blastjob.matching.fingerprint import scoring_fingerprint

    jid = _score_once(cfg, conn, monkeypatch)
    row = conn.execute("SELECT score_model_version FROM jobs WHERE id=?", (jid,)).fetchone()
    expected = scoring_fingerprint(cfg, pipeline.work_history_text(cfg))
    assert row["score_model_version"] == expected != ""


def test_score_kept_writes_the_fingerprint(cfg, conn, monkeypatch):
    from blastjob.matching.fingerprint import scoring_fingerprint

    jid = _job(conn)
    conn.execute("UPDATE jobs SET review_state='kept', score_stale=1 WHERE id=?", (jid,))
    conn.commit()
    rec = Recorder()
    monkeypatch.setattr("blastjob.matching.scorer.make_stream", rec)
    row = conn.execute(
        "SELECT id, title, company, raw_jd, review_state FROM jobs WHERE id=?", (jid,)
    ).fetchone()

    landed = asyncio.run(rescore.score_kept(cfg, conn, CostTracker(), row))

    assert landed == 1
    got = conn.execute("SELECT score_model_version FROM jobs WHERE id=?", (jid,)).fetchone()
    assert got["score_model_version"] == scoring_fingerprint(cfg, pipeline.work_history_text(cfg))


def test_stale_score_notice_counts_only_rows_match_would_rescore(cfg, conn, monkeypatch):
    current = _score_once(cfg, conn, monkeypatch)
    conn.execute("UPDATE jobs SET review_state='queued' WHERE id=?", (current,))
    old = _job(conn, ext="e2", company="Old Co")
    kept = _job(conn, ext="e3", company="Kept Co")
    conn.execute(
        "UPDATE jobs SET alignment_score=80, score_model_version='', review_state='surfaced' "
        "WHERE id=?",
        (old,),
    )
    conn.execute(
        "UPDATE jobs SET alignment_score=80, score_model_version='', review_state='kept' "
        "WHERE id=?",
        (kept,),
    )
    conn.commit()
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))

    body = client.get("/scores/stale").text
    assert "1 score predates your last profile or work-history change" in body

    _write_history(cfg, "A rewritten work history.")
    body = client.get("/scores/stale").text
    assert "2 scores predate your last profile or work-history change" in body


def test_stale_score_notice_is_empty_when_nothing_is_stale(cfg, conn, monkeypatch):
    _score_once(cfg, conn, monkeypatch)
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))

    resp = client.get("/scores/stale")

    assert resp.status_code == 200
    assert "predate" not in resp.text


def test_every_page_lazy_loads_the_stale_score_notice(cfg, conn):
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))
    assert 'hx-get="/scores/stale"' in client.get("/").text


def test_match_clears_the_stale_count_even_for_rows_it_no_longer_scores(cfg, conn, monkeypatch):
    # Scored before #29, now outside the configured area: Match resets it to
    # 'new' without a model call, so its old score is never re-stamped. It must
    # not keep the notice lit for a row on no stage page.
    from blastjob.matching.fingerprint import scoring_fingerprint

    far = dbc.upsert_job(
        conn,
        source="t",
        external_id="far",
        title="VP Ops",
        company="Far Co",
        raw_jd=LONG_JD,
        geo_city="New York",
        geo_region="NY",
        geo_location_raw="New York, NY",
    )
    conn.execute(
        "UPDATE jobs SET alignment_score=88, review_state='surfaced', score_model_version='' "
        "WHERE id=?",
        (far,),
    )
    conn.commit()
    monkeypatch.setattr("blastjob.matching.scorer.make_stream", Recorder())

    _match(cfg, conn)

    fp = scoring_fingerprint(cfg, pipeline.work_history_text(cfg))
    assert pipeline.stale_score_count(conn, fp) == 0


def test_fingerprint_does_not_depend_on_when_the_secrets_file_was_loaded(
    cfg, monkeypatch, isolated_secrets
):
    # provider=auto picks the provider from os.environ, and the cockpit loads the
    # secrets file lazily: without loading it first, the same process would stamp
    # "claude-cli" before a lazy load and "anthropic / <model>" after it, making
    # every earlier score look old.
    from blastjob import secrets as secrets_mod
    from blastjob.matching.fingerprint import scoring_fingerprint

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    isolated_secrets.write_text("ANTHROPIC_API_KEY=sk-test-not-real\n", encoding="utf-8")
    cfg.llm.provider = "auto"
    wh = pipeline.work_history_text(cfg)

    before = scoring_fingerprint(cfg, wh)
    secrets_mod.load_secrets_env()
    after = scoring_fingerprint(cfg, wh)

    assert before == after
    cfg.llm.provider = "anthropic"
    assert before == scoring_fingerprint(cfg, wh), "the secrets file's provider must be used"


def test_stale_score_notice_survives_an_undecodable_work_history(cfg, conn):
    (cfg_mod.data_dir(cfg) / "work_history.md").write_bytes(b"\xff\xfe not utf-8 \xc3")
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))

    resp = client.get("/scores/stale")

    assert resp.status_code == 200
    assert "predate" not in resp.text

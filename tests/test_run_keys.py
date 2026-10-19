"""Routes that start a per-subject run pass a single-flight key (#24)."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blastjob.core import company as co
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.runtime import Run
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN


@pytest.fixture
def env(monkeypatch):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    Path(cfg.paths.data_dir).mkdir(parents=True, exist_ok=True)
    conn = dbc.init_db(cfg)
    jid = dbc.upsert_job(
        conn, source="t", external_id="1", company="Acme Widgets", title="VP Ops", raw_jd="Lead."
    )
    conn.execute("UPDATE jobs SET review_state='kept' WHERE id=?", (jid,))
    conn.commit()
    conn.close()
    seen: list[tuple[str, str | None]] = []

    def fake_start(name, work, key=None):
        seen.append((name, key))
        return Run(id=f"{name}-1", name=name, key=key)

    monkeypatch.setattr("blastjob.web.routes_packets.RUNTIME.start", fake_start)
    monkeypatch.setattr("blastjob.web.routes_network.RUNTIME.start", fake_start)
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))
    return client, jid, seen


def _post(client, path, **kw):
    return client.post(path, headers={CSRF_HEADER: LAUNCH_TOKEN}, **kw)


@pytest.mark.parametrize(
    ("path", "name"),
    [("prep", "prep"), ("followup/draft", "followup")],
)
def test_per_job_runs_are_keyed_by_job_id(env, path, name):
    client, jid, seen = env
    assert _post(client, f"/jobs/{jid}/{path}").status_code == 200
    assert seen == [(name, str(jid))]


def test_research_is_keyed_by_company_key(env):
    client, jid, seen = env
    assert _post(client, f"/jobs/{jid}/research").status_code == 200
    assert seen == [("research", co.company_key("Acme Widgets"))]


def test_enrich_has_one_scope(env):
    client, _, seen = env
    _post(client, "/network/enrich")
    assert seen == [("enrich", "network")]


def test_outreach_key_includes_the_recipient(env):
    """A draft addressed to Bob must not attach to Alice's running draft."""
    client, jid, seen = env
    _post(client, f"/jobs/{jid}/outreach")
    _post(client, f"/jobs/{jid}/outreach", data={"contact_id": "5"})
    _post(client, f"/jobs/{jid}/outreach", data={"contact_id": "6"})
    assert seen == [
        ("outreach", f"{jid}:"),
        ("outreach", f"{jid}:5"),
        ("outreach", f"{jid}:6"),
    ]


async def test_outreach_runs_dedupe_per_job_and_contact():
    from blastjob.web import runtime as rt

    reg = rt.Runtime()
    gate = asyncio.Event()

    async def work(run):
        await gate.wait()

    alice = reg.start("outreach", work, key="1:5")
    bob = reg.start("outreach", work, key="1:6")
    again = reg.start("outreach", work, key="1:5")
    assert alice is not bob and again is alice
    gate.set()

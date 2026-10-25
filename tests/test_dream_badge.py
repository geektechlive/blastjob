"""The Dream badge on job rows and the job page (#90): current membership, truthful location."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from blastjob.core import target_store as ts
from blastjob.core.company import company_key
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web import service
from blastjob.web.app import create_app


@pytest.fixture
def env(tmp_path):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    conn = dbc.init_db(cfg)
    ts.add_targets(conn, [ts.TargetInput(name="Northwind Traders")], "dream")
    client = TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))
    yield client, conn, cfg
    conn.close()


def _job(
    conn: sqlite3.Connection,
    n: int,
    *,
    company: str = "Northwind Traders",
    far: int = 0,
    location: str = "Phoenix, AZ",
) -> int:
    jid = dbc.upsert_job(
        conn,
        source="t",
        external_id=str(n),
        company=company,
        title=f"VP of Technology {n}",
        url=f"https://example.com/{n}",
        geo_location_raw=location,
        geo_city="Phoenix",
        geo_region="AZ",
    )
    conn.execute(
        "UPDATE jobs SET review_state='surfaced', alignment_score=90, dream_far=? WHERE id=?",
        (far, jid),
    )
    conn.commit()
    return jid


def _badge(conn, cfg, jid: int) -> str:
    rows = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchall()
    return service.annotate_network(conn, rows, cfg.candidate)[0]["dream_badge"]


def test_a_dream_company_role_that_passes_geo_reads_dream(env):
    _, conn, cfg = env
    assert _badge(conn, cfg, _job(conn, 1, far=0)) == "Dream"


def test_a_far_role_names_its_location_so_it_never_looks_local(env):
    _, conn, cfg = env
    assert _badge(conn, cfg, _job(conn, 1, far=1)) == "Dream, on-site Phoenix, AZ"


def test_no_badge_for_a_company_that_is_not_a_dream(env):
    _, conn, cfg = env
    assert _badge(conn, cfg, _job(conn, 1, company="Fabrikam", far=1)) == ""


def test_the_badge_follows_current_membership(env):
    _, conn, cfg = env
    jid = _job(conn, 1, far=1)
    assert ts.set_tier(conn, company_key("Northwind Traders"), "core")
    assert _badge(conn, cfg, jid) == ""
    assert ts.remove_target(conn, company_key("Northwind Traders"))
    assert _badge(conn, cfg, jid) == ""


def test_the_list_and_the_job_page_show_the_badge(env):
    client, conn, _ = env
    far = _job(conn, 1, far=1)
    near = _job(conn, 2, far=0, location="Fairhaven, OH")
    page = client.get("/").text
    assert "Dream, on-site Phoenix, AZ" in page
    assert ">Dream<" in page
    assert "Dream, on-site Phoenix, AZ" in client.get(f"/jobs/{far}").text
    assert ">Dream<" in client.get(f"/jobs/{near}").text


def test_the_all_page_shows_the_badge_once_per_row(env):
    client, conn, _ = env
    _job(conn, 1, far=1)
    page = client.get("/all").text
    assert page.count("Dream, on-site Phoenix, AZ") == 1


def test_a_stage_list_row_shows_the_badge_once(env):
    client, conn, _ = env
    _job(conn, 1, far=0, location="Fairhaven, OH")
    assert client.get("/").text.count(">Dream<") == 1


def test_a_hostile_location_is_escaped(env):
    client, conn, _ = env
    jid = _job(conn, 1, far=1, location="<script>alert(1)</script>")
    body = client.get(f"/jobs/{jid}").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body

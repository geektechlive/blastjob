"""One recruiter definition for the desk, the list and the count; route hardening (#72)."""

from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

from blastjob.core import network as net
from blastjob.core import network_views as nv
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN
from tests.test_recruiter_desk_labels import _add


@pytest.fixture
def env():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)
    yield conn, cfg
    conn.close()


@pytest.fixture
def client(env):
    _, cfg = env
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))


def _tok():
    return {CSRF_HEADER: LAUNCH_TOKEN}


def test_summary_count_and_recruiters_match_desk(env):
    conn, _ = env
    _add(conn, "Title Rule", "Contoso", "Recruiter", is_recruiter=1)
    _add(conn, "Enr Only", "Acme Talent", "Managing Partner", enrich="agency")
    _add(conn, "Hid Den", "Contoso", "Recruiter", is_recruiter=1, override="not_recruiter")
    _add(conn, "Pres Ident", "Northwind", "President", enrich="none", override="agency")
    _add(conn, "Plain", "Contoso", "Engineer")
    desk = nv.recruiter_desk(conn)
    assert len(desk) == 3
    assert net.network_summary(conn)["recruiter_count"] == len(desk)
    assert {r["full_name"] for r in net.recruiters(conn)} == {d["name"] for d in desk}


def test_route_rejects_invalid_ctx(env, client):
    conn, _ = env
    cid = _add(conn, "Pat Partner", "Acme", "Partner")
    r = client.post(
        f"/network/contacts/{cid}/recruiter", data={"value": "agency", "ctx": "x"}, headers=_tok()
    )
    row = conn.execute("SELECT recruiter_override FROM network_contacts WHERE id=?", (cid,))
    assert r.status_code == 400 and row.fetchone()[0] is None


def test_search_context_response_also_refreshes_the_desk(env, client):
    conn, _ = env
    cid = _add(conn, "Pres Ident", "Northwind Partners", "President", enrich="none")
    r = client.post(
        f"/network/contacts/{cid}/recruiter",
        data={"value": "agency", "ctx": "search", "q": "northwind"},
        headers=_tok(),
    )
    assert r.status_code == 200
    assert 'id="contact-search"' in r.text
    assert 'id="recruiters"' in r.text and 'hx-swap-oob="outerHTML"' in r.text

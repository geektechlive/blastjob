"""Recruiter desk uses the enrichment labels and a manual override (#72)."""

from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

from blastjob.core import network_views as nv
from blastjob.core.company import company_key
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN


@pytest.fixture
def env():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    conn = dbc.init_db(cfg)
    yield conn, cfg
    conn.close()


def _add(
    conn,
    name: str,
    company: str,
    position: str = "",
    *,
    is_recruiter: int = 0,
    enrich: str | None = None,
    override: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO network_contacts (full_name, url, company, company_key, position, "
        "seniority, is_recruiter, enrich_recruiter, recruiter_override, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            name,
            f"https://linkedin.com/in/example-{name.lower().replace(' ', '')}",
            company,
            company_key(company),
            position,
            "ic",
            is_recruiter,
            enrich,
            override,
            "2026-01-01T00:00:00",
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _groups(conn) -> dict[str, str]:
    return {d["name"]: d["group"] for d in nv.recruiter_desk(conn)}


def test_enriched_agency_with_non_recruiter_title_is_listed_as_agency(env):
    conn, _ = env
    _add(conn, "Pat Partner", "Acme Talent Group", "Managing Partner", enrich="agency")
    assert _groups(conn) == {"Pat Partner": "agency"}


def test_exec_search_is_its_own_group_and_sorts_first(env):
    conn, _ = env
    _add(conn, "Ina House", "Contoso", "Recruiter", is_recruiter=1, enrich="in_house")
    _add(conn, "Ag Ent", "Acme Staffing", "Recruiter", is_recruiter=1, enrich="agency")
    _add(conn, "Esa Search", "Northwind Search, LLC", "Practice Director", enrich="exec_search")
    desk = nv.recruiter_desk(conn)
    assert [d["group"] for d in desk] == ["exec_search", "agency", "in_house"]


def test_enrich_label_beats_company_keyword(env):
    conn, _ = env
    _add(conn, "Kay Keyword", "Acme Staffing", "Recruiter", is_recruiter=1, enrich="in_house")
    assert _groups(conn) == {"Kay Keyword": "in_house"}


def test_unenriched_contact_keeps_keyword_behaviour(env):
    conn, _ = env
    _add(conn, "Una Nriched", "Acme Staffing", "Recruiter", is_recruiter=1)
    _add(conn, "Ned Inhouse", "Contoso", "Recruiter", is_recruiter=1, enrich="")
    _add(conn, "Not Recruiter", "Contoso", "Engineer")
    assert _groups(conn) == {"Una Nriched": "agency", "Ned Inhouse": "in_house"}


def test_enrich_none_with_recruiter_title_uses_keyword_grouping(env):
    conn, _ = env
    _add(conn, "Nia None", "Acme Staffing", "Recruiter", is_recruiter=1, enrich="none")
    assert _groups(conn) == {"Nia None": "agency"}


def test_override_not_a_recruiter_hides_contact(env):
    conn, _ = env
    _add(conn, "Hid Den", "Acme Staffing", "Recruiter", is_recruiter=1, override="not_recruiter")
    _add(conn, "Ag Hidden", "Acme Talent", "Partner", enrich="agency", override="not_recruiter")
    assert nv.recruiter_desk(conn) == []


def test_override_recruiter_includes_and_groups_contact(env):
    conn, _ = env
    _add(conn, "Pres Ident", "Northwind Partners", "President", enrich="none", override="agency")
    _add(conn, "Ex Ec", "Fabrikam", "Engineer", override="exec_search")
    assert _groups(conn) == {"Pres Ident": "agency", "Ex Ec": "exec_search"}


def test_override_group_beats_enrichment_group(env):
    conn, _ = env
    _add(conn, "Mo Ved", "Acme Talent", "Partner", enrich="agency", override="in_house")
    assert _groups(conn) == {"Mo Ved": "in_house"}


# ----- route -----


@pytest.fixture
def client(env):
    _, cfg = env
    return TestClient(create_app(load_config=lambda: cfg, save_config=lambda c: None))


def _tok():
    return {CSRF_HEADER: LAUNCH_TOKEN}


def _override(conn, cid):
    return conn.execute(
        "SELECT recruiter_override FROM network_contacts WHERE id=?", (cid,)
    ).fetchone()[0]


def test_route_sets_and_resets_override(env, client):
    conn, _ = env
    cid = _add(conn, "Pres Ident", "Northwind Partners", "President", enrich="none")
    url = f"/network/contacts/{cid}/recruiter"
    r = client.post(url, data={"value": "agency"}, headers=_tok())
    assert r.status_code == 200 and _override(conn, cid) == "agency"
    assert "Pres Ident" in client.get("/network/recruiters").text
    client.post(url, data={"value": "not_recruiter"}, headers=_tok())
    assert _override(conn, cid) == "not_recruiter"
    assert "Pres Ident" not in client.get("/network/recruiters").text
    r = client.post(url, data={"value": "auto"}, headers=_tok())
    assert r.status_code == 200 and _override(conn, cid) is None


def test_route_rejects_invalid_value_and_unknown_contact(env, client):
    conn, _ = env
    cid = _add(conn, "Pat Partner", "Acme", "Partner")
    url = f"/network/contacts/{cid}/recruiter"
    assert client.post(url, data={"value": "vibes"}, headers=_tok()).status_code == 400
    assert _override(conn, cid) is None
    r = client.post("/network/contacts/9999/recruiter", data={"value": "agency"}, headers=_tok())
    assert r.status_code == 404


def test_route_requires_csrf_token(env, client):
    conn, _ = env
    cid = _add(conn, "Pat Partner", "Acme", "Partner")
    r = client.post(f"/network/contacts/{cid}/recruiter", data={"value": "agency"})
    assert r.status_code in (400, 403)
    assert _override(conn, cid) is None


def test_search_finds_contact_not_on_desk_with_control(env, client):
    conn, _ = env
    cid = _add(conn, "Pres Ident", "Northwind Partners", "President", enrich="none")
    r = client.get("/network/contacts", params={"q": "northwind"})
    assert r.status_code == 200
    assert "Pres Ident" in r.text and f"/network/contacts/{cid}/recruiter" in r.text
    assert "Pres Ident" not in client.get("/network/contacts", params={"q": "zzz"}).text


def test_desk_rows_carry_the_control(env, client):
    conn, _ = env
    cid = _add(conn, "Ag Ent", "Acme Staffing", "Recruiter", is_recruiter=1)
    assert f"/network/contacts/{cid}/recruiter" in client.get("/network/recruiters").text


def test_migration_56_adds_override_column(env):
    conn, _ = env
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(network_contacts)")}
    assert "recruiter_override" in cols

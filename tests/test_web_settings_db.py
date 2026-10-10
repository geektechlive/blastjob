"""/settings on the real database-backed config: every section saves, bad
lines are refused by name, a stale page gets 409, and the first-run banner
shows until a profile exists."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from blastjob import config as cfg_mod
from blastjob import settings_store as ss
from blastjob.db import client as dbc
from blastjob.web import settings_form as sf
from blastjob.web.app import create_app
from blastjob.web.security import LAUNCH_TOKEN

_REVISION = re.compile(r'name="revision" value="([^"]*)"')


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(tmp_path / "data"))
    return TestClient(create_app())


def _form(client: TestClient, **over) -> dict:
    page = client.get("/settings").text
    revision = _REVISION.search(page).group(1)
    base = {
        "_token": LAUNCH_TOKEN,
        "revision": revision,
        "_sections": "core,profile,location,search,service",
        "comp_floor_usd": "120000",
        "alignment_threshold": "75",
        "commute_radius_miles": "30",
        "extended_radius_miles": "50",
        "remote_ok": "on",
        "exclude_companies": "Northwind Traders",
        "target_titles": "Director of Operations\nVP of Operations",
        "seniority": "director",
        "field": "operations",
        "former_employers": "Contoso",
        "home_location": "Harrowgate, OH",
        "home_state": "oh",
        "home_lat": "39.92",
        "home_lon": "-83.0",
        "towns": "harrowgate, 39.92, -83.0\nWest Fairhaven, 39.94, -83.06",
        "target_boards": "Fabrikam, workday, fabrikam/wd5/External",
        "adzuna_queries": "director of operations",
        "adzuna_where": "Harrowgate, OH",
        "title_keep_terms": "director\nvp",
        "title_drop_terms": "engineer",
        "search_firm_queries": "",
        "search_firm_regions": "",
        "apple_queries": "",
        "notify_mode": "stdout",
        "web_host": "127.0.0.1",
        "web_port": "8765",
    }
    return {**base, **over}


def _stored() -> dict:
    conn = dbc.init_db_at(cfg_mod.resolve_data_dir() / cfg_mod.DB_NAME)
    try:
        return ss.read_rows(conn)
    finally:
        conn.close()


def test_first_run_banner_until_a_profile_exists(client):
    assert "blastjob is not set up yet" in client.get("/").text

    r = client.post("/settings", data=_form(client), follow_redirects=False)

    assert r.status_code == 303
    assert "blastjob is not set up yet" not in client.get("/").text


def test_every_section_saves(client):
    client.post("/settings", data=_form(client), follow_redirects=False)

    cfg = cfg_mod.load_config()
    assert cfg.candidate.target_titles == ["Director of Operations", "VP of Operations"]
    assert cfg.discovery.home_state == "OH"
    assert cfg.discovery.towns == {"harrowgate": [39.92, -83.0], "west fairhaven": [39.94, -83.06]}
    assert [b.token for b in cfg.discovery.target_boards] == ["fabrikam/wd5/External"]
    assert cfg.discovery.title_keep_terms == ["director", "vp"]
    assert cfg.discovery.extended_radius_miles == 50
    page = client.get("/settings").text
    assert "west fairhaven, 39.94, -83.06" in page
    assert "Director of Operations\nVP of Operations</textarea>" in page


def test_a_bad_town_line_is_refused_by_name_and_nothing_is_saved(client):
    r = client.post("/settings", data=_form(client, towns="harrowgate, north"))
    assert r.status_code == 400
    assert "towns line 1" in r.text
    assert "candidate.target_titles" not in _stored()


def test_out_of_range_values_are_refused(client):
    r = client.post("/settings", data=_form(client, extended_radius_miles="10"))
    assert r.status_code == 400 and "extended_radius_miles" in r.text
    r = client.post("/settings", data=_form(client, alignment_threshold="140"))
    assert r.status_code == 400


def test_stale_page_gets_409_and_does_not_overwrite_a_newer_edit(client):
    """Lost update: two tabs load the page; the second save must not undo the
    first tab's change."""
    tab_a = _form(client, comp_floor_usd="150000")
    tab_b = _form(client, alignment_threshold="60")
    assert client.post("/settings", data=tab_a, follow_redirects=False).status_code == 303

    r = client.post("/settings", data=tab_b, follow_redirects=False)

    assert r.status_code == 409
    assert "Someone else changed the settings" in r.text
    cfg = cfg_mod.load_config()
    assert cfg.discovery.comp_floor_usd == 150000
    assert cfg.discovery.alignment_threshold == 75


def test_a_post_without_sections_only_touches_the_core_block(client):
    client.post("/settings", data=_form(client), follow_redirects=False)
    page = client.get("/settings").text
    core_only = {
        "_token": LAUNCH_TOKEN,
        "revision": _REVISION.search(page).group(1),
        "comp_floor_usd": "99000",
        "alignment_threshold": "70",
        "commute_radius_miles": "30",
    }
    assert client.post("/settings", data=core_only, follow_redirects=False).status_code == 303
    cfg = cfg_mod.load_config()
    assert cfg.discovery.comp_floor_usd == 99000
    assert cfg.candidate.target_titles == ["Director of Operations", "VP of Operations"]


def test_host_and_port_say_they_need_a_restart(client):
    page = client.get("/settings").text
    assert page.count("Takes effect after restarting the cockpit") == 2
    assert "Non-WI" not in page


def test_parse_boards_rejects_short_lines():
    with pytest.raises(sf.FormError, match="boards line 2"):
        sf.parse_boards("Fabrikam, workday, x/wd5/y\nContoso, lever")

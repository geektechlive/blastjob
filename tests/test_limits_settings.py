"""`limits.daily_deadline_minutes` (#95): default, bounds, `setup set`, and the settings page."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from blastjob import config as cfg_mod
from blastjob import settings_store as ss
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig, LimitsConfig
from blastjob.web.app import create_app
from tests.test_web_settings_db import _form


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(tmp_path / "data"))
    c = dbc.init_db_at(cfg_mod.resolve_data_dir() / cfg_mod.DB_NAME)
    yield c
    c.close()


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(tmp_path / "data"))
    return TestClient(create_app())


def test_default_is_45_minutes():
    assert BlastJobConfig().limits.daily_deadline_minutes == 45


@pytest.mark.parametrize("bad", [0, -1, 4, 241, 100000])
def test_out_of_range_is_rejected(bad):
    with pytest.raises(ValidationError):
        LimitsConfig(daily_deadline_minutes=bad)


@pytest.mark.parametrize("ok", [5, 45, 240])
def test_the_bounds_themselves_are_accepted(ok):
    assert LimitsConfig(daily_deadline_minutes=ok).daily_deadline_minutes == ok


def test_setup_set_stores_a_valid_value(conn):
    stored = ss.set_value(
        conn, "limits.daily_deadline_minutes", ss.parse_value("limits.daily_deadline_minutes", "90")
    )
    assert stored == 90
    assert ss.load(conn, cfg_mod.resolve_data_dir()).cfg.limits.daily_deadline_minutes == 90


@pytest.mark.parametrize("text", ["0", "-5", "241", "45.5", "abc", "true"])
def test_setup_set_refuses_bad_values_and_stores_nothing(conn, text):
    with pytest.raises(ss.SettingsError):
        ss.set_value(
            conn,
            "limits.daily_deadline_minutes",
            ss.parse_value("limits.daily_deadline_minutes", text),
        )
    assert "limits.daily_deadline_minutes" not in ss.read_rows(conn)


def test_settings_page_saves_a_valid_limit(client):
    r = client.post(
        "/settings",
        data=_form(client, limits_daily_deadline_minutes="60"),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert cfg_mod.load_config().limits.daily_deadline_minutes == 60
    assert 'name="limits_daily_deadline_minutes" value="60"' in client.get("/settings").text


@pytest.mark.parametrize("bad", ["0", "-3", "241", "x", ""])
def test_settings_page_refuses_a_bad_limit_and_saves_nothing(client, bad):
    r = client.post("/settings", data=_form(client, limits_daily_deadline_minutes=bad))
    assert r.status_code == 400
    assert "Daily run time limit" in r.text
    assert cfg_mod.load_config().limits.daily_deadline_minutes == 45


def test_a_form_without_the_field_keeps_the_stored_limit(client):
    client.post("/settings", data=_form(client, limits_daily_deadline_minutes="60"))
    client.post("/settings", data=_form(client))
    assert cfg_mod.load_config().limits.daily_deadline_minutes == 60

"""`notify.recruiter_touch_days` and `notify.recruiter_lookback_days` on the settings page and
through `setup set` (#98)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from blastjob import config as cfg_mod
from blastjob import settings_store as ss
from blastjob.db import client as dbc
from blastjob.web.app import create_app
from tests.test_web_settings_db import _form


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(tmp_path / "data"))
    return TestClient(create_app())


def test_the_page_saves_and_shows_both_values(client):
    r = client.post(
        "/settings",
        data=_form(client, notify_recruiter_touch_days="30", notify_recruiter_lookback_days="200"),
        follow_redirects=False,
    )
    assert r.status_code == 303
    notify = cfg_mod.load_config().notify
    assert (notify.recruiter_touch_days, notify.recruiter_lookback_days) == (30, 200)
    page = client.get("/settings").text
    assert 'name="notify_recruiter_touch_days" value="30"' in page
    assert 'name="notify_recruiter_lookback_days" value="200"' in page


@pytest.mark.parametrize(
    "field,bad,label",
    [
        ("notify_recruiter_touch_days", "13", "Recruiter touch-again reminder"),
        ("notify_recruiter_touch_days", "121", "Recruiter touch-again reminder"),
        ("notify_recruiter_touch_days", "x", "Recruiter touch-again reminder"),
        ("notify_recruiter_lookback_days", "89", "Recruiter reminder lookback"),
        ("notify_recruiter_lookback_days", "1096", "Recruiter reminder lookback"),
    ],
)
def test_the_page_refuses_out_of_range_values_and_saves_nothing(client, field, bad, label):
    r = client.post("/settings", data=_form(client, **{field: bad}))
    assert r.status_code == 400 and label in r.text
    notify = cfg_mod.load_config().notify
    assert (notify.recruiter_touch_days, notify.recruiter_lookback_days) == (42, 365)


def test_a_form_without_the_fields_keeps_the_stored_values(client):
    client.post("/settings", data=_form(client, notify_recruiter_touch_days="30"))
    client.post("/settings", data=_form(client))
    assert cfg_mod.load_config().notify.recruiter_touch_days == 30


def test_setup_set_validates_the_range(tmp_path, monkeypatch):
    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(tmp_path / "data"))
    conn = dbc.init_db_at(cfg_mod.resolve_data_dir() / cfg_mod.DB_NAME)
    key = "notify.recruiter_touch_days"
    assert ss.set_value(conn, key, ss.parse_value(key, "60")) == 60
    with pytest.raises(ss.SettingsError):
        ss.set_value(conn, key, ss.parse_value(key, "13"))
    conn.close()


def test_the_page_refuses_a_lookback_shorter_than_the_touch_days(client):
    r = client.post(
        "/settings",
        data=_form(client, notify_recruiter_touch_days="100", notify_recruiter_lookback_days="95"),
    )
    assert r.status_code == 400 and "Recruiter reminder lookback" in r.text
    assert cfg_mod.load_config().notify.recruiter_touch_days == 42

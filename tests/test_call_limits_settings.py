"""The call-budget limits (#96) are validated on every write path: the model, `setup set`, a
TOML import and the settings page. A save of one field never resets the others."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from blastjob import config as cfg_mod
from blastjob import settings_store as ss
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig, LimitsConfig
from blastjob.web.app import create_app
from tests.test_setup_cli import _run, data_dir  # noqa: F401
from tests.test_web_settings_db import _form

# field -> (default, lowest, highest, label on the settings page)
FIELDS = {
    "scheduled_calls_per_day": (150, 1, 5000, "Scheduled model calls per day"),
    "clicked_calls_per_day": (300, 1, 5000, "Clicked model calls per day"),
    "cockpit_concurrency": (3, 1, 8, "Cockpit runs at once"),
    "sdk_timeout_seconds": (300, 30, 900, "API call timeout"),
}
CASES = [(f, v) for f, (_d, lo, hi, _l) in FIELDS.items() for v in (lo - 1, 0, -1, hi + 1, 10**9)]


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


def test_the_defaults():
    limits = BlastJobConfig().limits
    assert {f: getattr(limits, f) for f in FIELDS} == {f: d for f, (d, *_rest) in FIELDS.items()}


@pytest.mark.parametrize(("field", "bad"), CASES)
def test_the_model_refuses_out_of_range_values(field, bad):
    with pytest.raises(ValidationError):
        LimitsConfig(**{field: bad})


@pytest.mark.parametrize("field", FIELDS)
def test_the_bounds_themselves_are_accepted(field):
    _d, low, high, _l = FIELDS[field]
    assert getattr(LimitsConfig(**{field: low}), field) == low
    assert getattr(LimitsConfig(**{field: high}), field) == high


@pytest.mark.parametrize(("field", "bad"), CASES)
def test_setup_set_refuses_out_of_range_values_and_stores_nothing(conn, field, bad):
    key = f"limits.{field}"
    with pytest.raises(ss.SettingsError):
        ss.set_value(conn, key, ss.parse_value(key, str(bad)))
    assert key not in ss.read_rows(conn)


@pytest.mark.parametrize("field", FIELDS)
def test_setup_set_stores_a_valid_value(data_dir, capsys, field):  # noqa: F811
    key = f"limits.{field}"
    _d, low, _h, _l = FIELDS[field]
    assert _run(capsys, "set", key, str(low))[0] == 0
    code, out, _ = _run(capsys, "get", key)
    assert code == 0 and json.loads(out) == low
    code, _out, err = _run(capsys, "set", key, "0")
    assert code != 0 and key in err


@pytest.mark.parametrize("field", FIELDS)
def test_a_toml_import_with_an_out_of_range_value_is_refused(data_dir, capsys, tmp_path, field):  # noqa: F811
    src = tmp_path / "in.toml"
    src.write_text(f"[limits]\n{field} = 99999\n")
    code, _out, err = _run(capsys, "import", str(src))
    assert code != 0 and field in err
    _code, out, _ = _run(capsys, "get", f"limits.{field}")
    assert json.loads(out) == FIELDS[field][0]


@pytest.mark.parametrize("field", FIELDS)
def test_the_settings_page_saves_a_valid_value(client, field):
    _d, low, _h, _l = FIELDS[field]
    r = client.post("/settings", data=_form(client, **{f"limits_{field}": str(low)}))
    assert r.status_code in (200, 303)
    assert getattr(cfg_mod.load_config().limits, field) == low
    assert f'name="limits_{field}" value="{low}"' in client.get("/settings").text


@pytest.mark.parametrize(
    ("field", "bad"), [(f, v) for f, v in CASES if v != 10**9] + [("cockpit_concurrency", "x")]
)
def test_the_settings_page_refuses_a_bad_value_by_name_and_saves_nothing(client, field, bad):
    r = client.post("/settings", data=_form(client, **{f"limits_{field}": str(bad)}))
    assert r.status_code == 400 and FIELDS[field][3] in r.text
    assert getattr(cfg_mod.load_config().limits, field) == FIELDS[field][0]


def test_saving_one_limit_does_not_reset_the_others(client):
    client.post("/settings", data=_form(client, limits_scheduled_calls_per_day="10"))
    client.post("/settings", data=_form(client, limits_cockpit_concurrency="2"))
    limits = cfg_mod.load_config().limits
    assert (limits.scheduled_calls_per_day, limits.cockpit_concurrency) == (10, 2)
    assert limits.daily_deadline_minutes == 45


def test_the_installed_limits_follow_the_loaded_config(conn):
    from blastjob.llm import ledger

    ss.set_value(conn, "limits.clicked_calls_per_day", 7)
    cfg_mod.load_config()
    inst = ledger.installed()
    assert inst is not None and inst.limits.clicked_calls_per_day == 7

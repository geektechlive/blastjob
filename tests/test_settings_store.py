"""Settings in the database: row format, legacy import, partial saves,
revisions, validation (blastjob/settings_store.py, blastjob/config.py)."""

from __future__ import annotations

import json
import sqlite3
import threading
import tomllib
from pathlib import Path

import pytest
import tomli_w

from blastjob import config as cfg_mod
from blastjob import settings_store as ss
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "data"
    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(d))
    return d


@pytest.fixture
def conn(data_dir):
    c = dbc.init_db_at(data_dir / "blastjob.db")
    yield c
    c.close()


def _legacy(tmp_path, monkeypatch, data: dict) -> Path:
    path = tmp_path / "legacy" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(tomli_w.dumps(data).encode())
    monkeypatch.setattr(cfg_mod, "_CONFIG_FILE", path)
    return path


def _rows(conn) -> dict[str, str]:
    return {k: v for k, v in conn.execute("SELECT key, value FROM settings")}


# --- row format ----------------------------------------------------------------


def test_one_row_per_dotted_leaf_with_a_json_value(conn, data_dir):
    cfg = ss.load(conn, data_dir).cfg
    cfg.discovery.comp_floor_usd = 150_000
    cfg.discovery.towns = {"fairhaven": [39.95, -83.05]}
    cfg.candidate.target_titles = ["Director of Operations"]

    written = ss.save(conn, cfg)

    assert written == [
        "candidate.target_titles",
        "discovery.comp_floor_usd",
        "discovery.towns",
    ]
    rows = _rows(conn)
    assert json.loads(rows["discovery.comp_floor_usd"]) == 150_000
    assert json.loads(rows["discovery.towns"]) == {"fairhaven": [39.95, -83.05]}


def test_data_dir_is_never_stored(conn, data_dir):
    cfg = ss.load(conn, data_dir).cfg
    cfg.paths.data_dir = "/somewhere/else"
    ss.save(conn, cfg)
    assert "paths.data_dir" not in _rows(conn)
    assert ss.load(conn, data_dir).cfg.paths.data_dir == str(data_dir)


# --- legacy import (C3) ----------------------------------------------------------


def test_first_start_imports_a_legacy_toml_once_with_a_marker(tmp_path, monkeypatch, data_dir):
    legacy = _legacy(
        tmp_path,
        monkeypatch,
        {"discovery": {"comp_floor_usd": 123_000, "max_commute_minutes": 45}},
    )

    cfg = cfg_mod.load_config()
    assert cfg.discovery.comp_floor_usd == 123_000

    conn = dbc.init_db_at(data_dir / "blastjob.db")
    marker = json.loads(_rows(conn)[ss.IMPORT_MARKER])
    assert marker["path"] == str(legacy)
    # A later edit to the TOML is never re-imported.
    legacy.write_bytes(tomli_w.dumps({"discovery": {"comp_floor_usd": 1}}).encode())
    assert cfg_mod.load_config().discovery.comp_floor_usd == 123_000
    assert legacy.exists()
    conn.close()


def test_no_import_when_the_table_already_has_rows(tmp_path, monkeypatch, conn, data_dir):
    ss.set_value(conn, "discovery.comp_floor_usd", 90_000)
    legacy = _legacy(tmp_path, monkeypatch, {"discovery": {"comp_floor_usd": 5}})
    assert not ss.import_legacy_once(conn, legacy)
    assert ss.load(conn, data_dir).cfg.discovery.comp_floor_usd == 90_000


def test_simultaneous_first_starts_import_exactly_once(tmp_path, monkeypatch, data_dir):
    legacy = _legacy(tmp_path, monkeypatch, {"discovery": {"comp_floor_usd": 77_000}})
    dbc.init_db_at(data_dir / "blastjob.db").close()  # schema, empty table
    results: list[bool] = []
    barrier = threading.Barrier(4)

    def start() -> None:
        c = dbc.connect_at(data_dir / "blastjob.db")
        barrier.wait()
        results.append(ss.import_legacy_once(c, legacy))
        c.close()

    threads = [threading.Thread(target=start) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [False, False, False, True]
    c = dbc.connect_at(data_dir / "blastjob.db")
    n = c.execute("SELECT COUNT(*) FROM settings WHERE key=?", (ss.IMPORT_MARKER,)).fetchone()[0]
    assert n == 1
    c.close()


def test_resolve_data_dir_prefers_env_then_legacy_then_default(tmp_path, monkeypatch):
    _legacy(tmp_path, monkeypatch, {"paths": {"data_dir": str(tmp_path / "legacy-data")}})
    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(tmp_path / "env-data"))
    assert cfg_mod.resolve_data_dir() == tmp_path / "env-data"
    monkeypatch.delenv("BLASTJOB_DATA_DIR")
    assert cfg_mod.resolve_data_dir() == tmp_path / "legacy-data"
    monkeypatch.setattr(cfg_mod, "_CONFIG_FILE", tmp_path / "missing.toml")
    assert cfg_mod.resolve_data_dir() == Path("~/.blastjob").expanduser()


def test_init_db_needs_no_config(data_dir):
    conn = dbc.init_db()
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='settings'").fetchone()
    conn.close()
    assert (data_dir / "blastjob.db").exists()


# --- unknown keys + partial saves (C4) --------------------------------------------


def test_unknown_keys_are_kept_reported_and_untouched_by_saves(tmp_path, monkeypatch, data_dir):
    _legacy(
        tmp_path,
        monkeypatch,
        {"generation": {"stream_to_tui": True}, "future": {"knob": 3}},
    )
    cfg_mod.load_config()
    conn = dbc.init_db_at(data_dir / "blastjob.db")
    loaded = ss.load(conn, data_dir)
    assert loaded.unknown == {"generation.stream_to_tui": True, "future.knob": 3}
    before = _rows(conn)

    cfg = loaded.cfg
    cfg.discovery.comp_floor_usd = 1
    ss.save(conn, cfg)

    after = _rows(conn)
    assert after["generation.stream_to_tui"] == before["generation.stream_to_tui"]
    assert after["future.knob"] == before["future.knob"]
    conn.close()


@pytest.mark.parametrize(
    ("key", "bad"),
    [("mail_password_env", "hunter2 hunter2"), ("personal_mail_user_env", "me@example.com")],
)
def test_save_runs_the_same_reference_checks_as_set_and_import(conn, data_dir, key, bad):
    """Defense in depth: a future /settings field for an *_env name must not be
    a way around the check set_value and import_mapping already make."""
    cfg = ss.load(conn, data_dir).cfg
    setattr(cfg.discovery, key, bad)

    with pytest.raises(ss.SettingsError, match="NAME of an environment variable") as err:
        ss.save(conn, cfg)

    assert bad not in str(err.value)
    assert f"discovery.{key}" not in ss.read_rows(conn)


def test_save_refuses_a_secrets_path_inside_the_checkout(conn, data_dir):
    cfg = ss.load(conn, data_dir).cfg
    cfg.paths.secrets_file = str(ss.REPO_ROOT / "secrets.env")

    with pytest.raises(ss.SettingsError, match="outside the blastjob checkout"):
        ss.save(conn, cfg)


def test_save_writes_only_changed_keys(conn, data_dir):
    ss.set_value(conn, "discovery.alignment_threshold", 80)
    stamp = conn.execute(
        "SELECT updated_at FROM settings WHERE key='discovery.alignment_threshold'"
    ).fetchone()[0]
    cfg = ss.load(conn, data_dir).cfg
    cfg.discovery.remote_ok = False

    assert ss.save(conn, cfg) == ["discovery.remote_ok"]
    assert (
        conn.execute(
            "SELECT updated_at FROM settings WHERE key='discovery.alignment_threshold'"
        ).fetchone()[0]
        == stamp
    )


def test_stale_revision_refuses_a_lost_update(conn, data_dir):
    """Two editors load the same snapshot and change different fields: the
    second save must not silently undo the first."""
    first = ss.load(conn, data_dir).cfg
    second = ss.load(conn, data_dir).cfg

    first.discovery.comp_floor_usd = 111
    ss.save(conn, first)
    second.discovery.alignment_threshold = 60

    with pytest.raises(ss.StaleSettings):
        ss.save(conn, second)
    final = ss.load(conn, data_dir).cfg
    assert final.discovery.comp_floor_usd == 111
    assert final.discovery.alignment_threshold == 78


def test_every_write_moves_the_revision(conn):
    r0 = ss.revision(conn)
    ss.set_value(conn, "discovery.comp_floor_usd", 1)
    r1 = ss.revision(conn)
    ss.set_value(conn, "discovery.comp_floor_usd", 2)
    assert r0 == "" and r0 < r1 < ss.revision(conn)


# --- validation (C8) --------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("discovery.alignment_threshold", 101),
        ("discovery.alignment_threshold", -1),
        ("discovery.home_lat", 91),
        ("discovery.home_lon", -181),
        ("discovery.commute_radius_miles", -5),
        ("discovery.comp_floor_usd", -1),
        ("discovery.extended_radius_miles", 10),  # below the 35-mile commute radius
        ("notify.mode", "disocrd"),
        ("llm.provider", "gpt"),
        ("discovery.towns", {"fairhaven": [999, 0]}),
        ("web.port", 0),
    ],
)
def test_set_value_rejects_out_of_range_and_unknown_choices(conn, key, value):
    with pytest.raises(ss.SettingsError):
        ss.set_value(conn, key, value)
    assert _rows(conn) == {}


def test_set_value_refuses_unknown_keys(conn):
    with pytest.raises(ss.SettingsError, match="unknown setting"):
        ss.set_value(conn, "discovery.nope", 1)
    with pytest.raises(ss.SettingsError):
        ss.set_value(conn, "paths.data_dir", "/x")


@pytest.mark.parametrize(
    "key",
    [
        "discovery.mail_password_env",
        "notify.discord_token_env",
        "llm.anthropic_api_key_env",
        "discovery.adzuna_app_key_env",
    ],
)
def test_secret_reference_fields_take_names_only(conn, key):
    with pytest.raises(ss.SettingsError, match="NAME of an environment variable"):
        ss.set_value(conn, key, "abcd efgh ijkl mnop")
    assert ss.set_value(conn, key, "MY_SECRET_NAME") == "MY_SECRET_NAME"


def test_secrets_file_must_be_an_absolute_path_outside_the_checkout(conn):
    with pytest.raises(ss.SettingsError, match="file path"):
        ss.set_value(conn, "paths.secrets_file", "API_KEY=sk-123")
    with pytest.raises(ss.SettingsError, match="file path"):
        ss.set_value(conn, "paths.secrets_file", "secrets.env")
    with pytest.raises(ss.SettingsError, match="outside the blastjob checkout"):
        ss.set_value(conn, "paths.secrets_file", str(ss.REPO_ROOT / "secrets.env"))
    assert ss.set_value(conn, "paths.secrets_file", "~/x/secrets.env") == "~/x/secrets.env"
    assert ss.set_value(conn, "paths.secrets_file", "") == ""


def test_secrets_file_is_not_an_env_name_field():
    assert "paths.secrets_file" not in ss.SECRET_REF_KEYS


def test_parse_value_keeps_bare_strings_and_reads_json():
    assert ss.parse_value("discovery.home_state", "OH") == "OH"
    assert ss.parse_value("discovery.home_state", "123") == "123"
    assert ss.parse_value("discovery.comp_floor_usd", "200000") == 200_000
    assert ss.parse_value("candidate.target_titles", '["A", "B"]') == ["A", "B"]
    with pytest.raises(ss.SettingsError):
        ss.parse_value("discovery.comp_floor_usd", "lots")


# --- import / export ---------------------------------------------------------------


def test_import_then_export_round_trips(tmp_path, conn, data_dir):
    src = tmp_path / "mine.toml"
    src.write_text(
        '[candidate]\ntarget_titles = ["Director of Operations"]\n'
        '[discovery]\nhome_state = "OH"\n[discovery.towns]\nfairhaven = [39.95, -83.05]\n'
    )
    assert ss.import_toml(conn, src) == 3

    exported = tomllib.loads(ss.export_toml(conn))
    assert exported["discovery"]["towns"] == {"fairhaven": [39.95, -83.05]}
    cfg = ss.load(conn, data_dir).cfg
    assert cfg.candidate.is_configured()


def test_import_validates_together_with_what_is_stored(conn):
    ss.set_value(conn, "discovery.commute_radius_miles", 30)
    with pytest.raises(ss.SettingsError, match="extended_radius_miles"):
        ss.import_mapping(conn, {"discovery": {"extended_radius_miles": 20}})


def test_import_refuses_a_secret_value_in_a_name_field(conn):
    with pytest.raises(ss.SettingsError):
        ss.import_mapping(conn, {"discovery": {"mail_password_env": "hunter2 hunter2"}})
    assert _rows(conn) == {}


def test_load_config_from_db_reads_a_copy_read_only(tmp_path, conn, data_dir):
    ss.set_value(conn, "discovery.comp_floor_usd", 42)
    cfg = cfg_mod.load_config_from_db(data_dir / "blastjob.db")
    assert cfg.discovery.comp_floor_usd == 42
    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()
    assert cfg_mod.load_config_from_db(bare) == BlastJobConfig(paths={"data_dir": str(tmp_path)})


def test_the_shipped_example_imports_and_counts_as_unconfigured(conn, data_dir):
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    assert ss.import_toml(conn, example) > 0
    cfg = ss.load(conn, data_dir).cfg
    assert not cfg.candidate.is_configured()
    assert cfg.notify.mode == "stdout" and cfg.web.host == "127.0.0.1"


def test_loading_settings_never_waits_on_another_writer(tmp_path, monkeypatch, data_dir):
    """The cockpit loads config several times per page; a daily run holding a
    write transaction must not stall (or lock out) any of those loads."""
    import time

    _legacy(tmp_path, monkeypatch, {"discovery": {"comp_floor_usd": 1}})
    cfg_mod.load_config()  # first start: imports, migrates
    writer = dbc.connect_at(data_dir / "blastjob.db")
    writer.execute("BEGIN IMMEDIATE")
    try:
        start = time.monotonic()
        assert cfg_mod.load_config().discovery.comp_floor_usd == 1
        assert time.monotonic() - start < 1.0
    finally:
        writer.rollback()
        writer.close()

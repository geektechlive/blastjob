"""Fixes from a fresh-clone setup run (a new user's AI following SETUP.md):
state names, secrets-file messages, setup CLI ergonomics, doctor text, ATS
validation, the LAN warning, the start-up line, private permissions."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from blastjob import secrets as secrets_mod
from blastjob import settings_store as ss
from blastjob import setup_cli, setup_doctor
from blastjob.db import client as dbc
from blastjob.discovery import resolve
from blastjob.matching.geo import classify_location
from blastjob.models.config import BlastJobConfig, DiscoveryConfig, TargetBoard
from blastjob.web import mail_settings
from blastjob.web.settings_form import FormError, parse_boards

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "blastjob"


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "data"
    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(d))
    return d


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    code = setup_cli.main(["setup", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


# --- 1. home_state: a state name must never turn home into out-of-state --------


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("OH", "OH"),
        ("oh", "OH"),
        ("Ohio", "OH"),
        (" new  york ", "NY"),
        ("Puerto Rico", "PR"),
        ("dc", "DC"),
        ("", ""),
        ("<ST>", ""),
    ],
)
def test_home_state_folds_names_and_case_to_the_code(given, stored):
    assert DiscoveryConfig(home_state=given).home_state == stored


@pytest.mark.parametrize("bad", ["Ohioo", "XX", "Ontario", "O"])
def test_home_state_that_is_not_a_us_state_is_refused(bad):
    with pytest.raises(ValidationError, match="two-letter code"):
        DiscoveryConfig(home_state=bad)


def test_setup_set_stores_a_state_name_as_its_code(data_dir, capsys):
    code, out, _ = _run(capsys, "set", "discovery.home_state", "Ohio")
    assert code == 0 and json.loads(out.split(" = ", 1)[1]) == "OH"


def test_a_full_name_already_in_the_database_loads_as_the_code(data_dir):
    conn = dbc.init_db_at(data_dir / "blastjob.db")
    try:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("discovery.home_state", json.dumps("Ohio"), "2026-10-01T00:00:00"),
        )
        conn.commit()
        cfg = ss.load(conn, data_dir).cfg
    finally:
        conn.close()
    assert cfg.discovery.home_state == "OH"


def test_classify_location_reads_a_full_name_home_state_as_home():
    cfg = DiscoveryConfig(towns={"fairhaven": [39.95, -83.05]})
    cfg.home_state = "Ohio"  # assigned in code: no validator ran

    status, town = classify_location("Fairhaven", "OH", "Fairhaven, OH", cfg)

    assert status == "home" and town == "fairhaven"


# --- 2. messages name the configured secrets file ------------------------------


def test_no_user_facing_text_names_the_old_secrets_path():
    old = "~/.claude/" + "secrets.env"
    hits = [str(p) for p in SRC.rglob("*") if p.is_file() and old in p.read_text(errors="ignore")]
    assert hits == []


def test_secrets_hint_is_the_configured_path(monkeypatch, tmp_path):
    monkeypatch.setattr(secrets_mod, "_configured", tmp_path / "keys" / "s.env")
    assert secrets_mod.secrets_file_hint() == str(tmp_path / "keys" / "s.env")

    monkeypatch.setattr(secrets_mod, "_configured", Path.home() / "x" / "s.env")
    assert secrets_mod.secrets_file_hint() == "~/x/s.env"


def test_mail_settings_error_points_at_the_configured_file(monkeypatch, tmp_path):
    target = tmp_path / "mine.env"
    monkeypatch.setattr(secrets_mod, "_configured", target)

    err = mail_settings.validate_names(
        mail_user_env="not a name",
        mail_password_env="APP_PASSWORD",
        personal_mail_user_env="",
        personal_mail_password_env="",
    )

    assert err and str(target) in err


# --- 3. import reports unknown keys ------------------------------------------


def test_import_prints_unknown_keys_and_still_succeeds(tmp_path, data_dir, capsys):
    src = tmp_path / "old.toml"
    src.write_text('[discovery]\nhome_state = "OH"\nretired_knob = 3\n[legacy]\nx = 1\n')

    code, out, _ = _run(capsys, "import", str(src))

    assert code == 0
    assert "unknown settings" in out
    assert "discovery.retired_knob" in out and "legacy.x" in out


# --- 4 + 13. doctor fix text ------------------------------------------------


def test_doctor_points_at_ingest_on_the_configured_port(data_dir, capsys):
    _run(capsys, "set", "web.port", "9123")
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(data_dir)
    cfg.web.port = 9123

    (wh,) = [c for c in setup_doctor.check_profile(cfg) if c.name == "work_history.md present"]

    assert wh.fix.endswith("http://127.0.0.1:9123/work-history/ingest")


def test_doctor_no_provider_fix_names_all_three_options(monkeypatch):
    from blastjob.llm import providers

    def none(_cfg):
        raise providers.ProviderNotConfiguredError("none")

    monkeypatch.setattr(providers, "detect_provider", none)

    check = setup_doctor.check_llm(BlastJobConfig())

    assert not check.ok
    for name in ("claude CLI", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        assert name in check.fix


# --- 5. list-typed keys from the command line ----------------------------------


def test_a_bare_string_for_a_string_list_is_a_one_item_list(data_dir, capsys):
    code, out, err = _run(capsys, "set", "candidate.target_titles", "Registered Nurse")

    assert code == 0
    assert json.loads(out.split(" = ", 1)[1]) == ["Registered Nurse"]
    assert "one-item list" in err and '\'["First item"' in err


def test_a_json_list_still_works_and_prints_no_note(data_dir, capsys):
    code, out, err = _run(capsys, "set", "candidate.target_titles", '["A", "B"]')
    assert code == 0 and json.loads(out.split(" = ", 1)[1]) == ["A", "B"] and err == ""


def test_a_non_string_list_key_explains_the_quoting(data_dir, capsys):
    code, _, err = _run(capsys, "set", "discovery.target_boards", "Acme")
    assert code == 2
    assert "expects a JSON list" in err and "single quotes" in err


def test_setup_help_keeps_its_line_breaks(capsys):
    with pytest.raises(SystemExit):
        setup_cli.main(["setup", "--help"])
    out = capsys.readouterr().out
    assert "\n    blastjob setup doctor " in out
    assert "\nValues for `set`:\n" in out


# --- 8. target boards: supported ATS only -----------------------------------


def test_target_board_ats_is_normalized_and_checked():
    assert TargetBoard(company="Acme", ats="Greenhouse", token="acme").ats == "greenhouse"
    with pytest.raises(ValidationError, match="unsupported ats 'taleo'"):
        TargetBoard(company="Acme", ats="taleo", token="acme")


def test_settings_form_names_the_bad_board_line():
    with pytest.raises(FormError, match="boards line 2: .*unsupported ats"):
        parse_boards("Acme, greenhouse, acme\nGlobex, taleo, globex")


# --- 9. LAN warning ---------------------------------------------------------


def test_non_loopback_web_host_warns(data_dir, capsys):
    code, _, err = _run(capsys, "set", "web.host", "0.0.0.0")
    assert code == 0 and "NO login" in err


def test_loopback_web_host_does_not_warn(data_dir, capsys):
    _, _, err = _run(capsys, "set", "web.host", "127.0.0.1")
    assert err == ""


# --- 11. start-up line ------------------------------------------------------


def test_blastjob_web_prints_where_it_serves(data_dir, monkeypatch, capsys):
    from blastjob.web import launch

    monkeypatch.setattr(launch.uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(launch, "create_app", lambda *a, **k: None)
    monkeypatch.setattr(launch, "_recover_on_start", lambda _cfg: None)
    monkeypatch.setattr(launch.applog, "setup", lambda *a, **k: None)

    launch.main(["--no-browser", "--port", "9555"])

    assert "blastjob cockpit: http://127.0.0.1:9555" in capsys.readouterr().out


# --- 12. private permissions on first creation -------------------------------


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_new_data_dir_and_db_are_owner_only(tmp_path):
    target = tmp_path / "fresh" / "blastjob.db"

    dbc.connect_at(target).close()

    assert _mode(target.parent) == 0o700
    assert _mode(target) == 0o600


def test_setup_doctor_on_a_fresh_install_leaves_everything_owner_only(
    data_dir, monkeypatch, capsys
):
    """Through the real entry point, not just connect_at: whatever touches the
    data dir first (logging, config load, the DB) must create it 0700."""
    monkeypatch.setattr(setup_doctor.shutil, "which", lambda _name: None)

    _run(capsys, "doctor")

    assert _mode(data_dir) == 0o700
    assert _mode(data_dir / "blastjob.db") == 0o600


def test_logging_creates_a_missing_data_dir_owner_only(tmp_path, monkeypatch):
    from blastjob import logging as applog

    fresh = tmp_path / "logs-first"
    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(fresh))

    assert applog._log_path() == fresh / "blastjob.log"
    assert _mode(fresh) == 0o700


def test_existing_dir_and_db_keep_their_modes(tmp_path):
    existing = tmp_path / "old"
    existing.mkdir()
    os.chmod(existing, 0o755)
    db = existing / "blastjob.db"
    db.write_bytes(b"")
    os.chmod(db, 0o644)

    dbc.connect_at(db).close()

    assert _mode(existing) == 0o755
    assert _mode(db) == 0o644


# --- 6 + 14. page copy -------------------------------------------------------


def test_work_history_page_does_not_mention_the_tui():
    text = (SRC / "web" / "templates" / "work_history.html").read_text()
    assert "TUI" not in text


def test_excluded_companies_help_sends_former_employers_elsewhere():
    text = (SRC / "web" / "templates" / "settings.html").read_text()
    assert "Current + prior employers" not in text
    assert "your current employer and any company you never want to see" in text.lower()
    assert "candidate.former_employers" in text


# --- 7. resolve says when Workday/Dayforce were not probed -------------------


def test_resolve_notes_that_workday_and_dayforce_need_jina(monkeypatch, capsys):
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    monkeypatch.setattr(resolve, "load_secrets_env", lambda *a, **k: 0)
    monkeypatch.setattr(resolve, "probe_company", lambda _name: [])

    assert resolve.main(["Example Widgets"]) == 0

    out = capsys.readouterr()
    assert "Workday and Dayforce" in out.err and "JINA_API_KEY" in out.err
    assert "no boards found" in out.out

"""Tests for `blastjob.web.launch` — the launchd/manual entry point for the cockpit."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from blastjob.web import launch

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _stub_startup(monkeypatch):
    """Neutralize side effects that don't matter for these tests: disk logging
    setup, stale-build recovery (DB access), and the actual uvicorn server."""
    monkeypatch.setattr(launch.applog, "setup", lambda: None)
    monkeypatch.setattr(launch, "_recover_on_start", lambda _cfg: None)

    calls: dict = {}

    def fake_run(app, host, port, log_level):
        calls["host"] = host
        calls["port"] = port
        calls["log_level"] = log_level

    monkeypatch.setattr(launch.uvicorn, "run", fake_run)
    return calls


@pytest.fixture(autouse=True)
def _stub_browser(monkeypatch):
    calls = {"started": False, "port": None}

    def fake_start(port, host="127.0.0.1"):
        calls["started"] = True
        calls["port"] = port

    monkeypatch.setattr(launch, "_start_browser_thread", fake_start)
    return calls


def test_no_browser_flag_binds_localhost_and_skips_browser(_stub_startup, _stub_browser):
    launch.main(["--no-browser", "--port", "8799"])

    assert _stub_startup["host"] == "127.0.0.1"
    assert _stub_startup["port"] == 8799
    assert _stub_browser["started"] is False


def test_default_invocation_opens_browser(_stub_startup, _stub_browser):
    launch.main([])

    assert _stub_startup["host"] == "127.0.0.1"
    assert _stub_startup["port"] == launch.PORT
    assert _stub_browser["started"] is True
    assert _stub_browser["port"] == launch.PORT


def test_host_and_port_come_from_settings(_stub_startup, monkeypatch):
    from blastjob import config as cfg_mod
    from blastjob import settings_store as ss
    from blastjob.db import client as dbc

    conn = dbc.init_db_at(cfg_mod.resolve_data_dir() / cfg_mod.DB_NAME)
    ss.set_value(conn, "web.host", "0.0.0.0")
    ss.set_value(conn, "web.port", 8800)
    conn.close()

    launch.main(["--no-browser"])

    assert (_stub_startup["host"], _stub_startup["port"]) == ("0.0.0.0", 8800)


def test_cli_flags_override_the_settings(_stub_startup):
    from blastjob.models.config import BlastJobConfig

    cfg = BlastJobConfig()
    cfg.web.host, cfg.web.port = "0.0.0.0", 8800
    assert launch.bind_address(cfg, "127.0.0.1", 9000) == ("127.0.0.1", 9000)
    assert launch.bind_address(cfg, None, None) == ("0.0.0.0", 8800)


def test_plist_template_parses_with_expected_keys():
    plist_path = REPO_ROOT / "launchd" / "blastjob.web.plist.template"
    with plist_path.open("rb") as f:
        data = plistlib.load(f)

    assert data["Label"] == "__LABEL_PREFIX__.blastjob.web"
    assert data["KeepAlive"] is True
    assert data["RunAtLoad"] is True
    # No host in the template: the launcher reads web.host from settings
    # (loopback by default; the cockpit has no login).
    assert data["ProgramArguments"] == ["__REPO__/scripts/run-web.sh"]


def test_install_script_renders_every_placeholder():
    import re

    script = (REPO_ROOT / "scripts" / "install-launchd.sh").read_text()
    for name in ("web", "daily"):
        template = REPO_ROOT / "launchd" / f"blastjob.{name}.plist.template"
        placeholders = set(re.findall(r"__[A-Z_]+__", template.read_text()))
        assert placeholders >= {"__REPO__", "__LOG_DIR__", "__DATA_DIR__", "__LABEL_PREFIX__"}
        for placeholder in placeholders:
            assert f"s|{placeholder}|" in script, placeholder


def test_run_web_script_is_executable_and_execs_no_browser():
    script_path = REPO_ROOT / "scripts" / "run-web.sh"
    assert script_path.exists()
    assert script_path.stat().st_mode & 0o111, "run-web.sh must be executable"

    contents = script_path.read_text()
    assert "--no-browser" in contents
    assert "exec " in contents

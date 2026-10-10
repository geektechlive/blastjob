"""scripts/install-launchd.sh renders valid plists that honour BLASTJOB_DATA_DIR.

RENDER_ONLY=1 prints the plists and touches nothing (no files, no launchctl),
so these run anywhere `sh` exists. A fake HOME keeps the real one out of it.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "install-launchd.sh"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")


def _render(tmp_path: Path, service: str, **env: str) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    full = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "USER": "tester"}
    full.update(RENDER_ONLY="1", **env)
    out = subprocess.run(
        ["sh", str(SCRIPT), service], env=full, capture_output=True, text=True, check=True
    )
    assert not (home / "Library").exists()  # render-only wrote nothing
    assert not (home / ".blastjob").exists()
    return plistlib.loads(out.stdout.encode())


@pytest.mark.parametrize("service", ["web", "daily"])
def test_default_logs_under_home_blastjob_and_no_env_var(tmp_path, service):
    plist = _render(tmp_path, service)

    assert "EnvironmentVariables" not in plist
    assert plist["StandardOutPath"] == f"{tmp_path}/home/.blastjob/{service}.launchd.log"
    assert plist["Label"] == f"local.tester.blastjob.{service}"


@pytest.mark.parametrize("service", ["web", "daily"])
def test_data_dir_is_passed_to_the_service_and_holds_its_log(tmp_path, service):
    data = tmp_path / "elsewhere"

    plist = _render(tmp_path, service, BLASTJOB_DATA_DIR=str(data))

    assert plist["EnvironmentVariables"] == {"BLASTJOB_DATA_DIR": str(data)}
    assert plist["StandardErrorPath"] == f"{data}/{service}.launchd.log"


def test_tilde_data_dir_expands_against_home(tmp_path):
    plist = _render(tmp_path, "daily", BLASTJOB_DATA_DIR="~/jobs data")

    assert plist["EnvironmentVariables"]["BLASTJOB_DATA_DIR"] == f"{tmp_path}/home/jobs data"


def test_sed_and_xml_special_characters_survive(tmp_path):
    plist = _render(tmp_path, "web", LABEL_PREFIX="a&b|c", BLASTJOB_DATA_DIR="/x/a&b|c<d>")

    assert plist["Label"] == "a&b|c.blastjob.web"
    assert plist["EnvironmentVariables"]["BLASTJOB_DATA_DIR"] == "/x/a&b|c<d>"


def test_unknown_argument_is_refused(tmp_path):
    full = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "RENDER_ONLY": "1"}
    r = subprocess.run(["sh", str(SCRIPT), "bogus"], env=full, capture_output=True, text=True)
    assert r.returncode == 1
    assert "unknown argument" in r.stderr


# --- conflicting agents (a stub launchctl records what would run) -------------


def _install(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, Path, Path]:
    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "launchctl.log"
    stub = bin_dir / "launchctl"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\n')
    stub.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "HOME": str(home),
        "USER": "tester",
        "BLASTJOB_DATA_DIR": str(tmp_path / "data"),
    }
    r = subprocess.run(["sh", str(SCRIPT), *args], env=env, capture_output=True, text=True)
    return r, agents, calls


def _calls(calls: Path) -> list[str]:
    return calls.read_text().splitlines() if calls.exists() else []


def _old_agent(tmp_path: Path) -> Path:
    old = tmp_path / "home" / "Library" / "LaunchAgents" / "com.old.blastjob.web.plist"
    old.parent.mkdir(parents=True)
    old.write_text("<plist/>")
    return old


def test_another_blastjob_agent_blocks_install_and_nothing_is_unloaded(tmp_path):
    old = _old_agent(tmp_path)

    r, agents, calls = _install(tmp_path, "web")

    assert r.returncode == 1
    assert "com.old.blastjob.web" in r.stderr and "--replace" in r.stderr
    assert old.exists()
    assert not (agents / "local.tester.blastjob.web.plist").exists()
    assert not any(c.startswith(("unload", "remove", "load")) for c in _calls(calls))


def test_replace_unloads_and_removes_the_other_agent_then_loads_ours(tmp_path):
    old = _old_agent(tmp_path)

    r, agents, calls = _install(tmp_path, "--replace", "web")

    assert r.returncode == 0, r.stderr
    assert not old.exists()
    assert f"unload {old}" in _calls(calls)
    ours = agents / "local.tester.blastjob.web.plist"
    assert f"load {ours}" in _calls(calls)
    assert plistlib.loads(ours.read_bytes())["Label"] == "local.tester.blastjob.web"
    assert (tmp_path / "data").is_dir()
    assert not (tmp_path / "home" / ".blastjob").exists()  # data dir set: no ~/.blastjob


def test_reinstalling_our_own_label_is_not_a_conflict(tmp_path):
    ours = tmp_path / "home" / "Library" / "LaunchAgents" / "local.tester.blastjob.daily.plist"
    ours.parent.mkdir(parents=True)
    ours.write_text("<plist/>")

    r, _, calls = _install(tmp_path, "daily")

    assert r.returncode == 0, r.stderr
    assert f"load {ours}" in _calls(calls)

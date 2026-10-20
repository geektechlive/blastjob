"""Backups are owner-only; a loose secrets file is flagged (warn, never refuse)."""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest

from blastjob import config as cfg_mod
from blastjob import secrets, setup_doctor
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.workhistory import backup_work_history


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def loose_umask():
    old = os.umask(0o022)
    yield
    os.umask(old)


# ---------- backups ----------


def test_db_backup_dir_and_file_are_owner_only(tmp_path, loose_umask):
    conn = dbc.connect_at(tmp_path / "live.db")
    dest_dir = tmp_path / "backups"

    target = dbc.backup_db(conn, dest_dir)

    assert _mode(dest_dir) == 0o700
    assert _mode(target) == 0o600


def test_db_backup_in_an_existing_dir_still_makes_the_file_0600(tmp_path, loose_umask):
    conn = dbc.connect_at(tmp_path / "live.db")
    dest_dir = tmp_path / "backups"
    dest_dir.mkdir(mode=0o755)

    target = dbc.backup_db(conn, dest_dir)

    assert _mode(target) == 0o600


def test_work_history_backup_is_owner_only_whatever_the_source_mode(tmp_path, loose_umask):
    src = tmp_path / "work_history.md"
    src.write_text("history", encoding="utf-8")
    src.chmod(0o644)

    dest = backup_work_history(src, tmp_path)

    assert dest.read_text(encoding="utf-8") == "history"
    assert _mode(dest.parent) == 0o700
    assert _mode(dest) == 0o600
    assert dest.stat().st_mtime == pytest.approx(src.stat().st_mtime, abs=1)


# ---------- secrets file ----------


def _secrets_file(tmp_path: Path, mode: int) -> Path:
    path = tmp_path / "secrets.env"
    path.write_text("BLASTJOB_TEST_MODE_KEY=hunter2-value\n", encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o604])
def test_loading_a_loose_secrets_file_warns_but_still_loads(
    isolated_secrets, monkeypatch, caplog, mode
):
    # The autouse guard routes every load to its own throwaway file, so write that one.
    monkeypatch.delenv("BLASTJOB_TEST_MODE_KEY", raising=False)
    path = _secrets_file(isolated_secrets.parent, mode)

    with caplog.at_level(logging.WARNING, logger="blastjob.secrets"):
        added = secrets.load_secrets_env(path)

    assert added == 1 and os.environ["BLASTJOB_TEST_MODE_KEY"] == "hunter2-value"
    assert "chmod 600" in caplog.text
    assert "hunter2-value" not in caplog.text
    monkeypatch.delenv("BLASTJOB_TEST_MODE_KEY", raising=False)


def test_loading_a_private_secrets_file_does_not_warn(isolated_secrets, monkeypatch, caplog):
    monkeypatch.delenv("BLASTJOB_TEST_MODE_KEY", raising=False)
    path = _secrets_file(isolated_secrets.parent, 0o600)

    with caplog.at_level(logging.WARNING, logger="blastjob.secrets"):
        secrets.load_secrets_env(path)

    assert caplog.text == ""
    monkeypatch.delenv("BLASTJOB_TEST_MODE_KEY", raising=False)


def test_a_secrets_file_owned_by_someone_else_is_a_problem(tmp_path, monkeypatch):
    path = _secrets_file(tmp_path, 0o600)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(path).st_uid + 1)

    problems = secrets.secrets_file_problems(path)

    assert any("owned by another user" in p for p in problems)


def test_missing_secrets_file_has_no_problems(tmp_path):
    assert secrets.secrets_file_problems(tmp_path / "nope.env") == []


def test_doctor_warns_when_the_secrets_file_is_loose(tmp_path, monkeypatch):
    monkeypatch.delenv("BLASTJOB_TEST_MODE_KEY", raising=False)
    path = _secrets_file(tmp_path, 0o644)
    monkeypatch.setattr(cfg_mod, "secrets_path", lambda cfg: path)

    checks = setup_doctor.check_secrets(BlastJobConfig())

    mode = next(c for c in checks if c.name == "secrets file mode")
    assert not mode.ok and not mode.required
    assert "chmod 600" in mode.fix
    assert "hunter2-value" not in mode.detail + mode.fix
    monkeypatch.delenv("BLASTJOB_TEST_MODE_KEY", raising=False)


def test_doctor_secrets_file_mode_is_ok_when_private(tmp_path, monkeypatch):
    path = _secrets_file(tmp_path, 0o600)
    monkeypatch.setattr(cfg_mod, "secrets_path", lambda cfg: path)

    checks = setup_doctor.check_secrets(BlastJobConfig())

    assert next(c for c in checks if c.name == "secrets file mode").ok
    monkeypatch.delenv("BLASTJOB_TEST_MODE_KEY", raising=False)


# ---------- hardening from the security review ----------

FIXED_NOW = __import__("datetime").datetime(2026, 10, 20, 1, 2, 3)


def test_db_backup_does_not_follow_a_planted_symlink(tmp_path):
    conn = dbc.connect_at(tmp_path / "live.db")
    dest_dir = tmp_path / "backups"
    dest_dir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    (dest_dir / "blastjob-20261020-010203.db").symlink_to(victim)

    target = dbc.backup_db(conn, dest_dir, now=FIXED_NOW)

    assert victim.read_text(encoding="utf-8") == "precious"
    assert not target.is_symlink() and target.name != "blastjob-20261020-010203.db"
    assert _mode(target) == 0o600


def test_db_backup_never_overwrites_a_same_second_backup(tmp_path):
    conn = dbc.connect_at(tmp_path / "live.db")
    first = dbc.backup_db(conn, tmp_path / "backups", now=FIXED_NOW)
    second = dbc.backup_db(conn, tmp_path / "backups", now=FIXED_NOW)

    assert first != second and first.exists() and second.exists()


def test_failed_db_backup_leaves_no_partial_file(tmp_path):
    class _Broken:
        def backup(self, dest):
            raise RuntimeError("disk full")

    dest_dir = tmp_path / "backups"

    with pytest.raises(RuntimeError):
        dbc.backup_db(_Broken(), dest_dir, now=FIXED_NOW)  # type: ignore[arg-type]

    assert list(dest_dir.glob("blastjob-*.db")) == []


def test_unreadable_work_history_source_leaves_no_zero_byte_backup(tmp_path):
    src = tmp_path / "work_history.md"
    src.write_text("history", encoding="utf-8")
    src.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            backup_work_history(src, tmp_path)
    finally:
        src.chmod(0o600)

    assert list((tmp_path / "backups").iterdir()) == []

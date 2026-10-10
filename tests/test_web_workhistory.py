"""Tests for the /work-history cockpit routes (GET render + POST save).

work_history.md is the single input every resume, cover letter, interview prep
pack, and groundedness judgment derives from, so the save path is safety-critical:
tmp-write + os.replace (never truncate the canonical file), a timestamped backup
before overwrite, a rejected empty/whitespace body, and lost-update protection
(a save must be based on the file's current content or it is rejected rather
than silently overwriting a concurrent edit from another tab or the TUI).
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blastjob import config as cfg_mod
from blastjob.models.config import BlastJobConfig
from blastjob.web import workhistory as wh
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN


def _tok():
    return {CSRF_HEADER: LAUNCH_TOKEN}


@pytest.fixture
def client_cfg():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    saved: dict = {}

    def load():
        return cfg

    def save(c):
        saved["cfg"] = c

    app = create_app(load_config=load, save_config=save)
    return TestClient(app), cfg, saved


def _wh_path(cfg: BlastJobConfig) -> Path:
    return cfg_mod.data_dir(cfg) / "work_history.md"


def test_get_renders_existing_content_and_mtime(client_cfg):
    client, cfg, _ = client_cfg
    path = _wh_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("## Experience\n\nOriginal content here.\n", encoding="utf-8")

    r = client.get("/work-history")
    assert r.status_code == 200
    assert "Original content here." in r.text
    # mtime year should show up somewhere in a freshly-written file's timestamp
    year = str(datetime.now().year)
    assert year in r.text
    # the version hash is embedded for the round-trip conflict check
    assert wh.content_hash("## Experience\n\nOriginal content here.\n") in r.text


def test_post_round_trip_save_then_get(client_cfg):
    client, cfg, _ = client_cfg
    path = _wh_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("old content\n", encoding="utf-8")
    version = wh.content_hash("old content\n")

    r = client.post(
        "/work-history",
        data={
            "_token": LAUNCH_TOKEN,
            "content": "brand new work history\n",
            "base_version": version,
        },
    )
    assert r.status_code == 200
    assert "brand new work history" in r.text

    r2 = client.get("/work-history")
    assert "brand new work history" in r2.text
    assert path.read_text(encoding="utf-8") == "brand new work history\n"


def test_failed_temp_write_leaves_original_byte_identical(client_cfg, monkeypatch):
    client, cfg, _ = client_cfg
    path = _wh_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = "untouched original\n"
    path.write_text(original, encoding="utf-8")
    original_bytes = path.read_bytes()
    version = wh.content_hash(original)

    def _boom(self, *a, **kw):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(Path, "write_text", _boom)

    r = client.post(
        "/work-history",
        data={
            "_token": LAUNCH_TOKEN,
            "content": "content that never lands\n",
            "base_version": version,
        },
        headers=_tok(),
    )
    assert r.status_code == 200
    assert "error" in r.text.lower() or "fail" in r.text.lower()
    assert path.read_bytes() == original_bytes


def test_backup_file_created_on_save(client_cfg):
    client, cfg, _ = client_cfg
    path = _wh_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("version one\n", encoding="utf-8")
    version = wh.content_hash("version one\n")

    backups_dir = cfg_mod.data_dir(cfg) / "backups"
    assert not backups_dir.exists() or list(backups_dir.glob("work_history-*.md")) == []

    r = client.post(
        "/work-history",
        data={"_token": LAUNCH_TOKEN, "content": "version two\n", "base_version": version},
    )
    assert r.status_code == 200

    backups = list(backups_dir.glob("work_history-*.md"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "version one\n"


def test_empty_body_rejected_and_original_unchanged(client_cfg):
    client, cfg, _ = client_cfg
    path = _wh_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("keep me\n", encoding="utf-8")
    version = wh.content_hash("keep me\n")

    r = client.post(
        "/work-history",
        data={"_token": LAUNCH_TOKEN, "content": "   \n\t  ", "base_version": version},
    )
    assert r.status_code == 200
    assert "error" in r.text.lower()
    assert path.read_text(encoding="utf-8") == "keep me\n"


def test_post_requires_csrf(client_cfg):
    client, cfg, _ = client_cfg
    r = client.post("/work-history", data={"content": "no token here"})
    assert r.status_code == 403


# ----- lost-update protection ------------------------------------------------


def test_stale_version_rejected_and_file_unchanged(client_cfg):
    client, cfg, _ = client_cfg
    path = _wh_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("current on disk\n", encoding="utf-8")

    # Someone else already changed the file; our submitted version is stale.
    stale_version = wh.content_hash("an older version this browser tab loaded\n")

    r = client.post(
        "/work-history",
        data={
            "_token": LAUNCH_TOKEN,
            "content": "my stale edit\n",
            "base_version": stale_version,
        },
    )
    assert r.status_code == 200
    assert path.read_text(encoding="utf-8") == "current on disk\n"


def test_current_version_succeeds(client_cfg):
    client, cfg, _ = client_cfg
    path = _wh_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = "base content\n"
    path.write_text(original, encoding="utf-8")
    version = wh.content_hash(original)

    r = client.post(
        "/work-history",
        data={
            "_token": LAUNCH_TOKEN,
            "content": "updated content\n",
            "base_version": version,
        },
    )
    assert r.status_code == 200
    assert path.read_text(encoding="utf-8") == "updated content\n"


def test_conflict_response_preserves_submitted_text(client_cfg):
    client, cfg, _ = client_cfg
    path = _wh_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("current on disk\n", encoding="utf-8")
    stale_version = wh.content_hash("an older version this browser tab loaded\n")

    r = client.post(
        "/work-history",
        data={
            "_token": LAUNCH_TOKEN,
            "content": "my careful edit that must not be lost\n",
            "base_version": stale_version,
        },
    )
    assert r.status_code == 200
    assert "my careful edit that must not be lost" in r.text
    # and the disk copy -- someone else's save -- was not clobbered
    assert path.read_text(encoding="utf-8") == "current on disk\n"


def test_two_saves_in_same_second_produce_distinct_backups(client_cfg, monkeypatch):
    """Force both saves to compute the identical backup timestamp (same second)
    and confirm the collision-avoidance suffix keeps both backup files."""
    client, cfg, _ = client_cfg
    path = _wh_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("v1\n", encoding="utf-8")

    fixed = wh.datetime(2026, 1, 1, 12, 0, 0)

    class _FixedDatetime(wh.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(wh, "datetime", _FixedDatetime)

    version1 = wh.content_hash("v1\n")
    r1 = client.post(
        "/work-history",
        data={"_token": LAUNCH_TOKEN, "content": "v2\n", "base_version": version1},
    )
    assert r1.status_code == 200
    assert path.read_text(encoding="utf-8") == "v2\n"

    version2 = wh.content_hash("v2\n")
    r2 = client.post(
        "/work-history",
        data={"_token": LAUNCH_TOKEN, "content": "v3\n", "base_version": version2},
    )
    assert r2.status_code == 200
    assert path.read_text(encoding="utf-8") == "v3\n"

    backups_dir = cfg_mod.data_dir(cfg) / "backups"
    backups = sorted(backups_dir.glob("work_history-*.md"))
    assert len(backups) == 2
    contents = {p.read_text(encoding="utf-8") for p in backups}
    assert contents == {"v1\n", "v2\n"}

"""`web.public_url` (#64): the base of every cockpit link in the digest. Held to the
cockpit's own Host rule (web/security.py `_is_local`), so a value the cockpit would
refuse can never be saved."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from blastjob import config as cfg_mod
from blastjob import daily, setup_cli
from blastjob import settings_store as ss
from blastjob.models.config import BlastJobConfig, WebConfig
from blastjob.web.app import create_app
from blastjob.web.security import LAUNCH_TOKEN

_REVISION = re.compile(r'name="revision" value="([^"]*)"')


def _cfg(**web) -> BlastJobConfig:
    return BlastJobConfig(web=WebConfig(**web))


def test_digest_base_uses_public_url_when_set():
    assert daily.cockpit_base(_cfg(public_url="http://192.168.1.20:8765")) == (
        "http://192.168.1.20:8765"
    )


def test_digest_base_falls_back_to_loopback_and_the_configured_port():
    assert daily.cockpit_base(_cfg(port=9123)) == "http://127.0.0.1:9123"


def test_trailing_slash_is_stripped():
    assert _cfg(public_url="http://localhost:8765/").web.public_url == "http://localhost:8765"


@pytest.mark.parametrize(
    "value",
    [
        "http://cockpit.example.com:8765",  # DNS name: the Host check refuses it
        "ftp://192.168.1.20",
        "http://192.168.1.20/jobs",
        "http://192.168.1.20:8765/?x=1",
        "http://192.168.1.20#top",
        "192.168.1.20:8765",
        "http://8.8.8.8:8765",  # public address: the Host check refuses it
        "http://192.168.1.20:99999",
    ],
)
def test_invalid_public_url_is_rejected(value):
    with pytest.raises(ValidationError):
        WebConfig(public_url=value)


@pytest.mark.parametrize(
    "value", ["https://10.0.0.5", "http://localhost:8765", "http://[fe80::1]:8765", ""]
)
def test_valid_public_url_is_accepted(value):
    assert WebConfig(public_url=value).public_url == value


def test_digest_lines_use_the_derived_base():
    text = daily.build_digest([], {}, [], base_url="http://192.168.1.20:8765", replies=[])
    assert "127.0.0.1" not in text


def test_setup_set_stores_and_rejects(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(tmp_path / "data"))
    assert setup_cli.main(["setup", "set", "web.public_url", "http://192.168.1.20:8765/"]) == 0
    assert cfg_mod.load_config().web.public_url == "http://192.168.1.20:8765"
    capsys.readouterr()

    assert setup_cli.main(["setup", "set", "web.public_url", "http://host.example.com"]) != 0
    assert "web.public_url must look like" in capsys.readouterr().err
    assert cfg_mod.load_config().web.public_url == "http://192.168.1.20:8765"
    assert "web.public_url" in ss.effective(BlastJobConfig())


def test_settings_page_saves_and_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("BLASTJOB_DATA_DIR", str(tmp_path / "data"))
    client = TestClient(create_app(), client=("127.0.0.1", 50000))  # loopback peer (#87)
    page = client.get("/settings").text
    assert "web_public_url" in page
    data = {
        "_token": LAUNCH_TOKEN,
        "revision": _REVISION.search(page).group(1),
        "_sections": "service",
        "notify_mode": "stdout",
        "web_host": "127.0.0.1",
        "web_port": "8765",
        "web_public_url": "http://host.example.com",
    }
    r = client.post("/settings", data=data)
    assert r.status_code == 400 and "web.public_url" in r.text

    r = client.post("/settings", data={**data, "web_public_url": "http://10.0.0.5:8765/"})
    assert r.status_code in (200, 303)
    assert cfg_mod.load_config().web.public_url == "http://10.0.0.5:8765"

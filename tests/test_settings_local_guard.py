"""Settings that steer credentials or the bind change only from this computer (#87).

A LAN device can read the CSRF token and pass the Origin check, so without this a
POST could point the saved mail login at a server it controls.
"""

from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

LAN = ("192.168.1.50", 50000)
LOOPBACK = ("127.0.0.1", 50000)
NOTE = "Read-only from this device"
REFUSAL = "can only be changed on the computer running blastjob"


def _make(peer):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    cfg.discovery.mail_imap_host = "imap.alertmail.example.com"
    cfg.discovery.personal_mail_imap_host = "imap.personalmail.example.com"
    cfg.web.host = "127.0.0.1"
    cfg.web.public_url = ""
    saved: list[BlastJobConfig] = []
    app = create_app(load_config=lambda: cfg, save_config=saved.append)
    return TestClient(app, client=peer, follow_redirects=False), cfg, saved


def _mail_form(**over):
    form = {
        "mail_alerts_enabled": "on",
        "mail_imap_host": "imap.alertmail.example.com",
        "mail_user_env": "ALERT_IMAP_USER",
        "mail_password_env": "ALERT_IMAP_APP_PASSWORD",
        "mail_mailbox": "INBOX",
        "personal_mail_imap_host": "imap.personalmail.example.com",
        "personal_mail_user_env": "PERSONAL_IMAP_USER",
        "personal_mail_password_env": "PERSONAL_IMAP_APP_PASSWORD",
        "personal_mail_mailbox": "INBOX",
        "_token": LAUNCH_TOKEN,
    }
    form.update(over)
    return form


def _service_form(**over):
    form = {
        "_sections": "service",
        "notify_mode": "stdout",
        "web_host": "127.0.0.1",
        "web_port": "8765",
        "web_public_url": "",
        "_token": LAUNCH_TOKEN,
    }
    form.update(over)
    return form


MAIL_CASES = [
    ("mail_imap_host", "evil.example.net"),
    ("personal_mail_imap_host", "evil.example.net"),
]
SERVICE_CASES = [
    ("web_host", "0.0.0.0"),
    ("web_public_url", "http://192.168.1.20:8765"),
]


@pytest.mark.parametrize(("field", "value"), MAIL_CASES)
def test_lan_cannot_change_mail_host(field, value):
    client, cfg, saved = _make(LAN)
    r = client.post("/settings/mail", data=_mail_form(**{field: value, "mail_mailbox": "Other"}))
    assert r.status_code == 403
    assert REFUSAL in r.text and field in r.text
    assert saved == []
    assert cfg.discovery.mail_imap_host == "imap.alertmail.example.com"
    assert cfg.discovery.personal_mail_imap_host == "imap.personalmail.example.com"
    assert cfg.discovery.mail_mailbox == "INBOX"


@pytest.mark.parametrize(("field", "value"), SERVICE_CASES)
def test_lan_cannot_change_service_field(field, value):
    client, cfg, saved = _make(LAN)
    r = client.post("/settings", data=_service_form(**{field: value, "web_port": "9000"}))
    assert r.status_code == 403
    assert REFUSAL in r.text and field in r.text
    assert saved == []
    assert cfg.web.port != 9000
    assert cfg.web.host == "127.0.0.1" and cfg.web.public_url == ""


def test_lan_whitespace_only_difference_is_not_a_change():
    client, _, saved = _make(LAN)
    r = client.post(
        "/settings/mail", data=_mail_form(mail_imap_host=" imap.alertmail.example.com ")
    )
    assert r.status_code == 303 and len(saved) == 1


def test_lan_saves_other_mail_fields_when_hosts_unchanged():
    client, _, saved = _make(LAN)
    r = client.post("/settings/mail", data=_mail_form(mail_mailbox="Alerts"))
    assert r.status_code == 303
    assert saved[-1].discovery.mail_mailbox == "Alerts"


def test_lan_saves_other_service_fields_when_guarded_unchanged():
    client, _, saved = _make(LAN)
    r = client.post("/settings", data=_service_form(web_port="9000"))
    assert r.status_code == 303
    assert saved[-1].web.port == 9000


def test_lan_other_section_without_service_fields_saves():
    client, cfg, saved = _make(LAN)
    data = {"_sections": "profile", "seniority": "VP", "_token": LAUNCH_TOKEN}
    r = client.post("/settings", data=data)
    assert r.status_code == 303 and saved[-1].web.host == cfg.web.host


@pytest.mark.parametrize(("field", "value"), MAIL_CASES)
def test_loopback_can_change_mail_host(field, value):
    client, _, saved = _make(LOOPBACK)
    r = client.post("/settings/mail", data=_mail_form(**{field: value}))
    assert r.status_code == 303
    assert getattr(saved[-1].discovery, field) == value


@pytest.mark.parametrize(("field", "value"), SERVICE_CASES)
def test_loopback_can_change_service_field(field, value):
    client, _, saved = _make(LOOPBACK)
    r = client.post("/settings", data=_service_form(**{field: value}))
    assert r.status_code == 303
    assert getattr(saved[-1].web, field.removeprefix("web_")) == value


def test_note_shown_to_lan_viewer_only():
    lan, _, _ = _make(LAN)
    local, _, _ = _make(LOOPBACK)
    page = lan.get("/settings").text
    assert NOTE in page
    assert 'name="mail_imap_host"' in page and 'name="web_host"' in page
    assert NOTE not in local.get("/settings").text


def test_token_header_post_also_guarded():
    client, _, saved = _make(LAN)
    form = _mail_form(mail_imap_host="evil.example.net")
    form.pop("_token")
    r = client.post("/settings/mail", data=form, headers={CSRF_HEADER: LAUNCH_TOKEN})
    assert r.status_code == 403 and saved == []

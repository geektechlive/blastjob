"""The /settings/mail routes: editing which mailboxes blastjob reads, from the
browser rather than by hand-editing config.toml.

The rail worth the most here is negative: a form field that accepts a pasted
app password would write a live credential into config.toml, which is backed
up in the clear and quoted into logs. So the interesting assertions are that
`save_config` is NOT called, and that no secret value reaches the HTML.
"""

from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

APP_PASSWORD_SHAPED = "abcd efgh ijkl mnop"


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


def _good_form(**over):
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


def test_settings_page_shows_both_mailboxes(client_cfg):
    client, cfg, _ = client_cfg
    cfg.discovery.personal_mail_imap_host = "imap.personalmail.example.com"
    r = client.get("/settings")
    assert r.status_code == 200
    assert "imap.personalmail.example.com" in r.text
    assert "imap.mail.me.com" in r.text


def test_saving_valid_names_updates_the_config(client_cfg):
    client, cfg, saved = client_cfg
    r = client.post("/settings/mail", data=_good_form(), headers=_tok(), follow_redirects=False)
    assert r.status_code == 303
    assert saved["cfg"].discovery.personal_mail_imap_host == "imap.personalmail.example.com"
    assert saved["cfg"].discovery.personal_mail_user_env == "PERSONAL_IMAP_USER"


def test_a_pasted_app_password_is_rejected_and_nothing_is_saved(client_cfg):
    """The defect this route could introduce: a live credential written into
    config.toml, in the clear, forever."""
    client, cfg, saved = client_cfg
    r = client.post(
        "/settings/mail",
        data=_good_form(personal_mail_password_env=APP_PASSWORD_SHAPED),
        headers=_tok(),
    )
    assert r.status_code == 200  # re-renders with an error, not a redirect
    assert "saved" not in saved  # save_config never called
    assert cfg.discovery.personal_mail_password_env != APP_PASSWORD_SHAPED
    assert "must be the NAME" in r.text
    # and the rejected value is not echoed back into the page
    assert APP_PASSWORD_SHAPED not in r.text
    assert "abcd" not in r.text


def test_a_lowercase_env_name_is_rejected(client_cfg):
    client, _cfg, saved = client_cfg
    r = client.post(
        "/settings/mail", data=_good_form(mail_user_env="alert_imap_user"), headers=_tok()
    )
    assert r.status_code == 200
    assert "saved" not in saved


def test_blank_personal_fields_are_allowed_and_mean_inherit(client_cfg):
    client, _cfg, saved = client_cfg
    r = client.post(
        "/settings/mail",
        data=_good_form(
            personal_mail_imap_host="",
            personal_mail_user_env="",
            personal_mail_password_env="",
            personal_mail_mailbox="",
        ),
        headers=_tok(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert saved["cfg"].discovery.personal_mail_user_env == ""


def test_the_page_never_renders_a_credential_value(client_cfg, monkeypatch):
    client, cfg, _ = client_cfg
    monkeypatch.setenv("ICLOUD_IMAP_USER", "user@example.com")
    monkeypatch.setenv("ICLOUD_IMAP_APP_PASSWORD", "super-secret-value")
    r = client.get("/settings")
    assert "super-secret-value" not in r.text
    assert "user@example.com" not in r.text
    # but it does say the credential is present
    assert "ICLOUD_IMAP_APP_PASSWORD" in r.text


def test_mail_settings_require_csrf(client_cfg):
    """`require_csrf` accepts the token by header OR by the `_token` form
    field, so the real no-token case is a form with neither."""
    client, _cfg, saved = client_cfg
    form = _good_form()
    form.pop("_token")
    r = client.post("/settings/mail", data=form, follow_redirects=False)
    assert r.status_code == 403
    assert "saved" not in saved


def test_probe_route_reports_a_classified_result(client_cfg, monkeypatch):
    client, _cfg, _ = client_cfg
    monkeypatch.delenv("ICLOUD_IMAP_USER", raising=False)
    monkeypatch.delenv("ICLOUD_IMAP_APP_PASSWORD", raising=False)
    r = client.post("/settings/mail/test/personal", headers=_tok())
    assert r.status_code == 200
    assert "not set" in r.text.lower()


def test_probe_route_rejects_an_unknown_account(client_cfg):
    client, _cfg, _ = client_cfg
    r = client.post("/settings/mail/test/bogus", headers=_tok())
    assert r.status_code == 404


def test_probe_route_requires_csrf(client_cfg):
    client, _cfg, _ = client_cfg
    assert client.post("/settings/mail/test/personal").status_code == 403


@pytest.mark.parametrize("bad", ["ANTHROPIC_API_KEY", "DISCORD_BOT_TOKEN"])
def test_another_secrets_name_is_rejected_for_the_password_field(client_cfg, bad):
    client, cfg, saved = client_cfg
    for field in ("mail_password_env", "personal_mail_password_env"):
        r = client.post("/settings/mail", data=_good_form(**{field: bad}), headers=_tok())
        assert r.status_code == 200
        assert f"{field} must end in" in r.text
        assert "saved" not in saved
    assert cfg.discovery.mail_password_env == "ICLOUD_IMAP_APP_PASSWORD"


def test_a_user_field_must_end_in_imap_user(client_cfg):
    client, _cfg, saved = client_cfg
    r = client.post(
        "/settings/mail", data=_good_form(mail_user_env="ADZUNA_APP_ID"), headers=_tok()
    )
    assert r.status_code == 200
    assert "mail_user_env must end in" in r.text
    assert "saved" not in saved


@pytest.mark.parametrize(
    ("user", "password"),
    [
        ("ICLOUD_IMAP_USER", "ICLOUD_IMAP_APP_PASSWORD"),
        ("GMAIL_IMAP_USER", "GMAIL_IMAP_APP_PASSWORD"),
        ("FOO_IMAP_USER", "FOO_IMAP_PASSWORD"),
    ],
)
def test_imap_shaped_names_are_accepted(client_cfg, user, password):
    client, _cfg, saved = client_cfg
    r = client.post(
        "/settings/mail",
        data=_good_form(
            mail_user_env=user,
            mail_password_env=password,
            personal_mail_user_env=user,
            personal_mail_password_env=password,
        ),
        headers=_tok(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert saved["cfg"].discovery.mail_password_env == password


def test_a_mail_name_equal_to_another_env_setting_is_rejected(client_cfg):
    """Belt and braces: even with a conforming suffix, a name another setting
    uses for a different secret is refused."""
    client, cfg, saved = client_cfg
    cfg.llm.openai_api_key_env = "SHARED_IMAP_PASSWORD"
    r = client.post(
        "/settings/mail",
        data=_good_form(mail_password_env="SHARED_IMAP_PASSWORD"),
        headers=_tok(),
    )
    assert r.status_code == 200
    assert "different secret" in r.text
    assert "saved" not in saved

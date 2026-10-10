"""The cockpit's mail settings: env-var-NAME editing (never values), which
mailbox actually gets scanned, and a connection probe that diagnoses an app
password without printing it.

The rail these tests exist for: `config.toml` is not a secret store. It is
backed up in the clear and its contents are quoted in logs and terminals. A
form field that accepts a pasted app password would put a live credential
there permanently, so an invalid env var NAME is rejected, never sanitized.
"""

from __future__ import annotations

import imaplib
import tempfile

import pytest

from blastjob.models.config import BlastJobConfig
from blastjob.web import mail_settings as ms

# What Google actually hands you: 16 lowercase chars in four groups.
APP_PASSWORD_SHAPED = "abcd efgh ijkl mnop"


@pytest.fixture
def cfg():
    c = BlastJobConfig()
    c.paths.data_dir = tempfile.mkdtemp()
    return c


# ---------- env var names: reject, never sanitize ----------


@pytest.mark.parametrize(
    "name",
    ["PERSONAL_IMAP_USER", "ICLOUD_IMAP_APP_PASSWORD", "A", "X9_Y"],
)
def test_valid_env_names_are_accepted(name):
    assert ms.valid_env_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        APP_PASSWORD_SHAPED,  # the whole point
        "abcdefghijklmnop",  # the same password without spaces
        "personal_imap_user",  # lowercase
        "PERSONAL IMAP USER",  # spaces
        "PERSONAL-IMAP-USER",  # hyphens
        "9PERSONAL",  # leading digit
        "PERSONAL=USER",
        "alex.rivera@example.com",  # an address, not a name
    ],
)
def test_credential_shaped_input_is_not_a_valid_env_name(name):
    assert ms.valid_env_name(name) is False


def test_blank_is_not_a_valid_env_name_but_is_a_valid_override():
    """Blank is how a personal-mailbox field says "inherit the alert account",
    so it is allowed there -- but it is never a *name*."""
    assert ms.valid_env_name("") is False
    assert ms.valid_override("") is True
    assert ms.valid_override("PERSONAL_IMAP_USER") is True
    assert ms.valid_override(APP_PASSWORD_SHAPED) is False


def test_validate_form_names_the_offending_field(cfg):
    err = ms.validate_names(
        mail_user_env="ICLOUD_IMAP_USER",
        mail_password_env="ICLOUD_IMAP_APP_PASSWORD",
        personal_mail_user_env="PERSONAL_IMAP_USER",
        personal_mail_password_env=APP_PASSWORD_SHAPED,
    )
    assert err is not None
    assert "personal_mail_password_env" in err
    # the rejected value must not be echoed back into the error
    assert "abcd" not in err and APP_PASSWORD_SHAPED not in err


def test_validate_form_passes_on_good_names():
    assert (
        ms.validate_names(
            mail_user_env="ICLOUD_IMAP_USER",
            mail_password_env="ICLOUD_IMAP_APP_PASSWORD",
            personal_mail_user_env="",
            personal_mail_password_env="",
        )
        is None
    )


# ---------- status: booleans only, and say which mailbox is really scanned ----------


def test_status_reports_presence_as_booleans_never_values(cfg, monkeypatch):
    monkeypatch.setenv("ICLOUD_IMAP_USER", "alex@example.com")
    monkeypatch.setenv("ICLOUD_IMAP_APP_PASSWORD", "super-secret-value")
    view = ms.status(cfg)
    alert = view.alert
    assert alert.user_set is True and alert.password_set is True
    # no value, and no length/prefix oracle either
    blob = repr(view)
    assert "super-secret-value" not in blob
    assert "alex@example.com" not in blob
    assert "18" not in blob  # len('super-secret-value')


def test_status_marks_missing_credentials(cfg, monkeypatch):
    monkeypatch.delenv("ICLOUD_IMAP_USER", raising=False)
    monkeypatch.delenv("ICLOUD_IMAP_APP_PASSWORD", raising=False)
    view = ms.status(cfg)
    assert view.alert.user_set is False and view.alert.password_set is False


def test_status_shows_the_resolved_personal_mailbox(cfg, monkeypatch):
    """The page's main job: which mailbox will actually be scanned. A raw
    blank field answers nothing -- that ambiguity is what hid the wrong-mailbox
    bug in the first place."""
    cfg.discovery.personal_mail_imap_host = "imap.mail.example.com"
    cfg.discovery.personal_mail_user_env = "PERSONAL_IMAP_USER"
    cfg.discovery.personal_mail_password_env = "PERSONAL_IMAP_APP_PASSWORD"
    monkeypatch.setenv("PERSONAL_IMAP_USER", "alex.rivera@example.com")
    monkeypatch.setenv("PERSONAL_IMAP_APP_PASSWORD", "pw")
    view = ms.status(cfg)
    assert view.personal.host == "imap.mail.example.com"
    assert view.personal.user_env == "PERSONAL_IMAP_USER"
    assert view.personal.configured is True
    # mailbox was left blank, so it is inherited from the alert account
    assert view.personal.mailbox == cfg.discovery.mail_mailbox
    assert "mailbox" in view.personal.inherited


def test_status_marks_a_fully_inherited_personal_account(cfg):
    """Nothing overridden: the reply scanner reads the ALERT mailbox, and the
    page must say so rather than implying a second account exists."""
    view = ms.status(cfg)
    assert view.personal.host == cfg.discovery.mail_imap_host
    assert set(view.personal.inherited) == {"host", "user_env", "password_env", "mailbox"}
    assert view.personal.fully_inherited is True


# ---------- the probe: classify the failure, never echo the password ----------


class _FakeIMAP:
    def __init__(self, *, login_error=None, select_error=None):
        self._login_error = login_error
        self._select_error = select_error
        self.logged_out = False

    def login(self, user, password):
        if self._login_error:
            raise self._login_error
        return ("OK", [b""])

    def select(self, mailbox, readonly=False):
        if self._select_error:
            raise self._select_error
        return ("OK", [b"1"])

    def logout(self):
        self.logged_out = True


def test_probe_reports_success_and_logs_out(cfg, monkeypatch):
    monkeypatch.setenv("ICLOUD_IMAP_USER", "alex@example.com")
    monkeypatch.setenv("ICLOUD_IMAP_APP_PASSWORD", "pw")
    fake = _FakeIMAP()
    ok, detail = ms.probe(cfg, "personal", connect=lambda host: fake)
    assert ok is True
    assert fake.logged_out is True
    assert "pw" not in detail


def test_probe_classifies_a_rejected_login(cfg, monkeypatch):
    monkeypatch.setenv("ICLOUD_IMAP_USER", "alex@example.com")
    monkeypatch.setenv("ICLOUD_IMAP_APP_PASSWORD", "wrong")
    err = imaplib.IMAP4.error("AUTHENTICATIONFAILED")
    ok, detail = ms.probe(cfg, "personal", connect=lambda host: _FakeIMAP(login_error=err))
    assert ok is False
    assert "rejected" in detail.lower()
    assert "wrong" not in detail


def test_probe_classifies_an_unreachable_host(cfg, monkeypatch):
    monkeypatch.setenv("ICLOUD_IMAP_USER", "alex@example.com")
    monkeypatch.setenv("ICLOUD_IMAP_APP_PASSWORD", "pw")

    def boom(host):
        raise OSError("nodename nor servname provided")

    ok, detail = ms.probe(cfg, "personal", connect=boom)
    assert ok is False
    assert "reach" in detail.lower()


def test_probe_redacts_whatever_the_server_said(cfg, monkeypatch):
    """IMAP error strings sometimes echo the login line back."""
    monkeypatch.setenv("ICLOUD_IMAP_USER", "alex@example.com")
    monkeypatch.setenv("ICLOUD_IMAP_APP_PASSWORD", "pw")
    err = imaplib.IMAP4.error("LOGIN alex@example.com app_key=leaked failed")
    ok, detail = ms.probe(cfg, "personal", connect=lambda host: _FakeIMAP(login_error=err))
    assert ok is False
    assert "leaked" not in detail


def test_probe_without_credentials_does_not_connect(cfg, monkeypatch):
    monkeypatch.delenv("ICLOUD_IMAP_USER", raising=False)
    monkeypatch.delenv("ICLOUD_IMAP_APP_PASSWORD", raising=False)

    def should_not_connect(host):
        raise AssertionError("must not open a connection without credentials")

    ok, detail = ms.probe(cfg, "personal", connect=should_not_connect)
    assert ok is False
    assert "not set" in detail.lower()


# ---------- secrets.env must be consulted, not just os.environ ----------
#
# The live cockpit rendered "missing" for credentials that provably worked: the
# web process never loaded ~/.claude/secrets.env (only the daily runner did), so
# a status page whose entire job is answering "are my credentials there?" gave
# the wrong answer. `web/people.py` and `web/research.py` already load lazily at
# the call site; this module must too.


def _secrets_file(isolated_secrets, body: str):
    """Write the file the `isolated_secrets` fixture already points at."""
    isolated_secrets.write_text(body, encoding="utf-8")
    return isolated_secrets


def test_status_finds_credentials_that_live_only_in_secrets_env(cfg, isolated_secrets, monkeypatch):
    monkeypatch.delenv("ICLOUD_IMAP_USER", raising=False)
    monkeypatch.delenv("ICLOUD_IMAP_APP_PASSWORD", raising=False)
    _secrets_file(
        isolated_secrets,
        'ICLOUD_IMAP_USER="alex@example.com"\nICLOUD_IMAP_APP_PASSWORD="pw"\n',
    )
    view = ms.status(cfg)
    assert view.alert.user_set is True
    assert view.alert.password_set is True
    assert view.alert.configured is True


def test_probe_finds_credentials_that_live_only_in_secrets_env(cfg, isolated_secrets, monkeypatch):
    monkeypatch.delenv("ICLOUD_IMAP_USER", raising=False)
    monkeypatch.delenv("ICLOUD_IMAP_APP_PASSWORD", raising=False)
    _secrets_file(
        isolated_secrets,
        'ICLOUD_IMAP_USER="alex@example.com"\nICLOUD_IMAP_APP_PASSWORD="pw"\n',
    )
    ok, detail = ms.probe(cfg, "personal", connect=lambda host: _FakeIMAP())
    assert ok is True, detail

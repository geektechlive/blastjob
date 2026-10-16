"""Every IMAP login verifies the server certificate.

imaplib.IMAP4_SSL without an ssl_context falls back to an UNVERIFIED context
(no certificate or hostname check), so a man-in-the-middle on the network
would receive the app password at LOGIN. Each default connect path must pass
a verifying context.
"""

from __future__ import annotations

import imaplib
import ssl
from datetime import date

import pytest

from blastjob.discovery import mailalerts, replies
from blastjob.models.config import BlastJobConfig
from blastjob.web import mail_settings


class _Refused(Exception):
    pass


@pytest.fixture
def captured(monkeypatch) -> list[ssl.SSLContext | None]:
    seen: list[ssl.SSLContext | None] = []

    def fake_imap4_ssl(host, *args, ssl_context=None, **kwargs):
        seen.append(ssl_context)
        raise _Refused(host)

    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake_imap4_ssl)
    return seen


def _assert_verifying(seen: list[ssl.SSLContext | None]) -> None:
    assert seen, "no IMAP connection was attempted"
    ctx = seen[0]
    assert ctx is not None, "IMAP4_SSL called without ssl_context (unverified TLS)"
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_alert_fetch_verifies_tls(captured):
    with pytest.raises(_Refused):
        mailalerts.fetch_alert_messages(
            host="imap.example.com",
            user="u",
            password="p",
            since=date(2026, 1, 1),
            skip_message_ids=set(),
        )
    _assert_verifying(captured)


def test_reply_date_fetch_verifies_tls(captured):
    with pytest.raises(_Refused):
        replies.fetch_message_dates(
            host="imap.example.com", user="u", password="p", mailbox="INBOX", uids=["1"]
        )
    _assert_verifying(captured)


def test_settings_probe_verifies_tls(captured, monkeypatch):
    monkeypatch.setenv("BJ_TEST_USER", "u")
    monkeypatch.setenv("BJ_TEST_PASS", "p")
    cfg = BlastJobConfig()
    cfg.discovery.mail_imap_host = "imap.example.com"
    cfg.discovery.mail_user_env = "BJ_TEST_USER"
    cfg.discovery.mail_password_env = "BJ_TEST_PASS"
    with pytest.raises(_Refused):
        mail_settings.probe(cfg, mail_settings.ALERT)
    _assert_verifying(captured)

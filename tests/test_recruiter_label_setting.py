"""The `discovery.recruiter_label` setting (#83): what it may name, refused on save, a safe
fallback when a stored value is bad, and the settings form field."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from blastjob import settings_store as ss
from blastjob.db import client as dbc
from blastjob.label_names import recruiter_label_problem
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app
from blastjob.web.security import CSRF_HEADER, LAUNCH_TOKEN

H = {CSRF_HEADER: LAUNCH_TOKEN}
KEY = "discovery.recruiter_label"


def _problem(label: str, personal: str = "", alert: str = "INBOX") -> str | None:
    return recruiter_label_problem(label, personal, alert)


def test_empty_label_is_off_and_fine():
    assert _problem("") is None


def test_a_plain_label_and_a_nested_one_are_fine():
    assert _problem("Leads") is None
    assert _problem("Inbound/Leads 2026") is None


@pytest.mark.parametrize(
    "label",
    ["INBOX", "inbox", "Inbox", "[Gmail]/All Mail", "[gmail]/sent mail", "[Google Mail]/Trash"],
)
def test_names_that_widen_the_read_are_refused(label):
    assert _problem(label)


def test_the_configured_mailboxes_are_refused_case_insensitively():
    assert _problem("Work", personal="work")
    assert _problem("Archive", personal="", alert="archive")


@pytest.mark.parametrize(
    "label",
    ["Café", "a\nb", "a\rb", 'say "hi"', "back\\slash", "star*", "pct%", "tab\there", "x" * 101],
)
def test_unsafe_or_unencodable_names_are_refused(label):
    assert _problem(label)


def test_the_error_never_echoes_the_value():
    assert "Caf" not in (_problem("Café") or "")


def test_whitespace_only_is_refused_not_treated_as_off():
    assert _problem("   ")


# --- the store ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    conn = dbc.init_db(cfg)
    yield conn, tmp_path
    conn.close()


def test_set_value_refuses_a_bad_label(store):
    conn, _ = store
    with pytest.raises(ss.SettingsError, match="recruiter_label"):
        ss.set_value(conn, KEY, "INBOX")
    assert ss.read_rows(conn).get(KEY) is None


def test_set_value_refuses_a_label_equal_to_the_personal_mailbox(store):
    conn, _ = store
    ss.set_value(conn, "discovery.personal_mail_mailbox", "Work")
    with pytest.raises(ss.SettingsError):
        ss.set_value(conn, KEY, "work")


def test_set_value_stores_a_good_label(store):
    conn, tmp = store
    ss.set_value(conn, KEY, "Inbound/Leads")
    assert ss.load(conn, tmp).cfg.discovery.recruiter_label == "Inbound/Leads"


def test_a_bad_stored_value_loads_as_off_with_one_warning(store, caplog):
    conn, tmp = store
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        (KEY, '"[Gmail]/All Mail"', "2026-10-27T00:00:00"),
    )
    conn.commit()
    ss._warned_labels.clear()
    with caplog.at_level(logging.WARNING):
        first = ss.load(conn, tmp).cfg
        second = ss.load(conn, tmp).cfg
    assert first.discovery.recruiter_label == "" == second.discovery.recruiter_label
    hits = [r for r in caplog.records if "recruiter_label" in r.getMessage()]
    assert len(hits) == 1
    assert "All Mail" not in hits[0].getMessage()


def test_other_stored_settings_survive_a_bad_label(store):
    conn, tmp = store
    ss.set_value(conn, "discovery.remote_ok", False)
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        (KEY, '"INBOX"', "2026-10-27T00:00:00"),
    )
    conn.commit()
    assert ss.load(conn, tmp).cfg.discovery.remote_ok is False


# --- the form ----------------------------------------------------------------------------


def _good_form(**over: str) -> dict[str, str]:
    form = {
        "mail_imap_host": "imap.alertmail.example.com",
        "mail_user_env": "ALERT_IMAP_USER",
        "mail_password_env": "ALERT_IMAP_APP_PASSWORD",
        "mail_mailbox": "INBOX",
        "personal_mail_imap_host": "imap.personalmail.example.com",
        "personal_mail_user_env": "PERSONAL_IMAP_USER",
        "personal_mail_password_env": "PERSONAL_IMAP_APP_PASSWORD",
        "personal_mail_mailbox": "",
        "_token": LAUNCH_TOKEN,
    }
    form.update(over)
    return form


@pytest.fixture
def client_cfg(tmp_path):
    cfg = BlastJobConfig()
    cfg.paths.data_dir = str(tmp_path)
    saved: dict = {}

    def save(c):
        saved["cfg"] = c

    app = create_app(load_config=lambda: cfg, save_config=save)
    return TestClient(app, client=("127.0.0.1", 50000)), cfg, saved


def test_the_settings_page_has_the_label_field(client_cfg):
    client, _, _ = client_cfg
    assert 'name="recruiter_label"' in client.get("/settings").text


def test_saving_a_good_label_stores_it(client_cfg):
    client, _, saved = client_cfg
    r = client.post(
        "/settings/mail",
        data=_good_form(recruiter_label=" Inbound/Leads "),
        headers=H,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert saved["cfg"].discovery.recruiter_label == "Inbound/Leads"


def test_a_form_without_the_field_keeps_the_stored_label(client_cfg):
    client, cfg, saved = client_cfg
    cfg.discovery.recruiter_label = "Inbound/Leads"
    r = client.post("/settings/mail", data=_good_form(), headers=H, follow_redirects=False)
    assert r.status_code == 303
    assert saved["cfg"].discovery.recruiter_label == "Inbound/Leads"


def test_an_empty_field_turns_it_off(client_cfg):
    client, cfg, saved = client_cfg
    cfg.discovery.recruiter_label = "Inbound/Leads"
    client.post("/settings/mail", data=_good_form(recruiter_label=""), headers=H)
    assert saved["cfg"].discovery.recruiter_label == ""


@pytest.mark.parametrize("label", ["INBOX", "[Gmail]/All Mail", 'bad"quote', "Caf\u00e9"])
def test_saving_a_bad_label_is_refused_and_nothing_is_written(client_cfg, label):
    client, _, saved = client_cfg
    r = client.post("/settings/mail", data=_good_form(recruiter_label=label), headers=H)
    assert "cfg" not in saved
    assert "recruiter label" in r.text.lower()


def test_the_label_may_not_equal_the_mailbox_being_saved_in_the_same_form(client_cfg):
    client, _, saved = client_cfg
    r = client.post(
        "/settings/mail",
        data=_good_form(personal_mail_mailbox="Archive", recruiter_label="archive"),
        headers=H,
    )
    assert "cfg" not in saved and "recruiter label" in r.text.lower()


def test_the_form_without_csrf_is_forbidden(client_cfg):
    client, _, saved = client_cfg
    form = _good_form(recruiter_label="Leads")
    form.pop("_token")
    r = client.post("/settings/mail", data=form)
    assert r.status_code == 403 and "cfg" not in saved

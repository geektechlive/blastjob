"""Mail settings for the cockpit: which mailboxes blastjob reads, editable
from the browser instead of by hand-editing `config.toml`.

Two accounts exist and they are not interchangeable (see
`discovery/replies.py`): the ALERT mailbox carries LinkedIn/Indeed job-alert
mail, the PERSONAL mailbox is where recruiters write. Pointing the reply
scanner at the alert account is what made that feature silently useless, and
a settings page that shows a blank override field answers nothing about which
mailbox will actually be scanned. So `status()` reports the RESOLVED account
per role and marks every field that is inherited rather than set. That is this
module's main job.

**Config is not a secret store.** `config.toml` is backed up in the clear and
its contents get quoted into logs and terminals. So the form edits env var
NAMES only; the values stay in the secrets file (`paths.secrets_file`). An input that does not
look like an env var name is REJECTED, never stripped-and-stored -- otherwise
a pasted app password becomes a permanent plaintext credential in a file
nothing treats as sensitive. `status()` returns booleans for presence and
never a value, a length or a prefix: the cockpit is LAN-bound without auth by
the user's standing decision, so anything this page renders is readable by
anything on the LAN.
"""

from __future__ import annotations

import imaplib
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from blastjob import logging as applog
from blastjob.discovery.mailalerts import open_imap
from blastjob.discovery.replies import MailAccount, personal_account
from blastjob.mail_names import name_problem
from blastjob.models.config import BlastJobConfig
from blastjob.secrets import secrets_file_hint

# POSIX-ish env var name. Deliberately strict: uppercase start, then uppercase
# alphanumerics and underscores. An app password ("abcd efgh ijkl mnop"), an
# email address, or a lowercase name all fail, which is the point.
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Which account a route is talking about.
ALERT = "alert"
PERSONAL = "personal"


def valid_env_name(value: str) -> bool:
    """True only for something shaped like an environment variable name."""
    return bool(_ENV_NAME_RE.match(value or ""))


def valid_override(value: str) -> bool:
    """A personal-mailbox field may be blank, which means "inherit the alert
    account". Blank is a valid override; it is never a valid *name*."""
    return value == "" or valid_env_name(value)


def validate_names(
    *,
    mail_user_env: str,
    mail_password_env: str,
    personal_mail_user_env: str,
    personal_mail_password_env: str,
    cfg: BlastJobConfig | None = None,
) -> str | None:
    """The error message for the first bad field, or None. The offending VALUE
    is never included -- if someone pasted a live password in, echoing it into
    an HTML error page would be the same leak by another route."""
    checks = (
        ("mail_user_env", mail_user_env, valid_env_name),
        ("mail_password_env", mail_password_env, valid_env_name),
        ("personal_mail_user_env", personal_mail_user_env, valid_override),
        ("personal_mail_password_env", personal_mail_password_env, valid_override),
    )
    for name, value, ok in checks:
        if not ok(value):
            return (
                f"{name} must be the NAME of an environment variable "
                "(uppercase letters, digits and underscores) — not a password or "
                f"an address. Secret values belong in {secrets_file_hint()}, never "
                "in settings."
            )
        # Shape is fine; now the IMAP-only suffix rule and the collision check
        # (`mail_names`), which keep other secrets out of an IMAP LOGIN.
        if problem := name_problem(name, value, cfg):
            return problem
    return None


@dataclass(frozen=True)
class AccountStatus:
    """What the page may show about one account. Presence is a boolean by
    design: no value, no length, no prefix."""

    role: str
    label: str
    host: str
    mailbox: str
    user_env: str
    password_env: str
    user_set: bool
    password_set: bool
    inherited: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        """Whether a scan of this account can actually run."""
        return self.user_set and self.password_set

    @property
    def fully_inherited(self) -> bool:
        return len(self.inherited) == 4


@dataclass(frozen=True)
class MailStatus:
    alert: AccountStatus
    personal: AccountStatus
    notes: tuple[str, ...] = field(default=())


def _load_secrets() -> None:
    """Make the secrets file visible to THIS process before reading any
    credential env var. The web app never did this (only the daily runner
    did), so the live page reported "missing" for credentials that worked --
    the exact opposite of what a status page is for. Lazy at the call site,
    matching `web/people.py` and `web/research.py`; it never overrides an
    existing variable, and doing it per request means adding a key to
    secrets.env shows up on refresh instead of needing a restart."""
    from blastjob.secrets import load_secrets_env

    load_secrets_env()


def _account_status(
    role: str, label: str, acct: MailAccount, inherited: tuple[str, ...] = ()
) -> AccountStatus:
    user, password = acct.credentials()
    return AccountStatus(
        role=role,
        label=label,
        host=acct.host,
        mailbox=acct.mailbox,
        user_env=acct.user_env,
        password_env=acct.password_env,
        user_set=bool(user),
        password_set=bool(password),
        inherited=inherited,
    )


def _alert_account(cfg: BlastJobConfig) -> MailAccount:
    d = cfg.discovery
    return MailAccount(
        host=d.mail_imap_host,
        user_env=d.mail_user_env,
        password_env=d.mail_password_env,
        mailbox=d.mail_mailbox,
    )


def _inherited_fields(cfg: BlastJobConfig) -> tuple[str, ...]:
    d = cfg.discovery
    pairs = (
        ("host", d.personal_mail_imap_host),
        ("user_env", d.personal_mail_user_env),
        ("password_env", d.personal_mail_password_env),
        ("mailbox", d.personal_mail_mailbox),
    )
    return tuple(name for name, value in pairs if not value)


def account_for(cfg: BlastJobConfig, role: str) -> MailAccount:
    return _alert_account(cfg) if role == ALERT else personal_account(cfg.discovery)


def status(cfg: BlastJobConfig) -> MailStatus:
    """Resolved state of both mailboxes, safe to render."""
    _load_secrets()
    inherited = _inherited_fields(cfg)
    personal = _account_status(
        PERSONAL,
        "Personal mailbox (recruiter replies)",
        personal_account(cfg.discovery),
        inherited,
    )
    notes: list[str] = []
    if personal.fully_inherited:
        notes.append(
            "The reply scanner is reading the ALERT mailbox, because nothing is "
            "overridden below. Recruiters usually write to a different account "
            "than job alerts arrive on."
        )
    if not personal.configured:
        notes.append(
            f"No credentials found for {personal.user_env} / {personal.password_env}, "
            "so recruiter replies are not being scanned at all."
        )
    return MailStatus(
        alert=_account_status(
            ALERT, "Alert mailbox (LinkedIn / Indeed job alerts)", _alert_account(cfg)
        ),
        personal=personal,
        notes=tuple(notes),
    )


def probe(
    cfg: BlastJobConfig,
    role: str,
    *,
    connect: Callable[[str], imaplib.IMAP4] | None = None,
) -> tuple[bool, str]:
    """Try a read-only login to one account and classify the result, so an app
    password can be diagnosed without anyone printing it.

    Reads the SAVED config, never a submitted form field: a host taken straight
    from a POST body would make this route an outbound-connection primitive on
    a surface that has no auth. Whatever the server says back is redacted
    before it is returned, because IMAP errors sometimes echo the login line.
    """
    _load_secrets()
    acct = account_for(cfg, role)
    user, password = acct.credentials()
    if not user or not password:
        return False, (
            f"{acct.user_env} / {acct.password_env} are not set in the environment. "
            f"Add them to {secrets_file_hint()}."
        )
    conn: imaplib.IMAP4 | None = None
    try:
        conn = (connect or open_imap)(acct.host)
    except OSError as e:
        return False, applog.redact(f"Cannot reach {acct.host}: {type(e).__name__}")
    try:
        conn.login(user, password)
    except imaplib.IMAP4.error as e:
        return False, applog.redact(
            f"{acct.host} rejected the login for {acct.user_env}: {e}. "
            "For Gmail this usually means an app password is required, or the "
            "existing one was revoked."
        )
    except OSError as e:
        return False, applog.redact(f"Cannot reach {acct.host}: {type(e).__name__}")
    else:
        try:
            typ, _data = conn.select(acct.mailbox, readonly=True)
        except (imaplib.IMAP4.error, OSError) as e:
            return False, applog.redact(f"Logged in, but mailbox {acct.mailbox!r} failed: {e}")
        if typ != "OK":
            return False, f"Logged in, but mailbox {acct.mailbox!r} could not be opened."
        return True, f"Connected to {acct.host} and opened {acct.mailbox}."
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 -- a failed logout is not a failed probe
            pass


__all__ = [
    "ALERT",
    "PERSONAL",
    "AccountStatus",
    "MailStatus",
    "account_for",
    "probe",
    "status",
    "valid_env_name",
    "valid_override",
    "validate_names",
]

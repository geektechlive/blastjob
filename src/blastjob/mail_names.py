"""Which environment variable NAMES the mail settings may point at.

The mail accounts only ever perform an IMAP login, so a user var must end in
`_IMAP_USER` and a password var in `_IMAP_PASSWORD` / `_IMAP_APP_PASSWORD`.
A looser rule (shape only) let a client of the unauthenticated cockpit save a
host of its choosing plus `ANTHROPIC_API_KEY` as the "password" var, and the
next mailbox login would send that secret to the host. Any name another
`*_env` setting already uses is refused too, so a future secret is covered
without editing this list.

Enforced when the form is saved and reported by `setup doctor`; never at load
time, so an install saved under the older rule keeps scanning.
"""

from __future__ import annotations

from pydantic import BaseModel

from blastjob.models.config import BlastJobConfig

USER_SUFFIXES = ("_IMAP_USER",)
PASSWORD_SUFFIXES = ("_IMAP_PASSWORD", "_IMAP_APP_PASSWORD")

# field -> (suffixes, blank allowed). Personal blank = inherit the alert account.
MAIL_FIELDS: dict[str, tuple[tuple[str, ...], bool]] = {
    "mail_user_env": (USER_SUFFIXES, False),
    "mail_password_env": (PASSWORD_SUFFIXES, False),
    "personal_mail_user_env": (USER_SUFFIXES, True),
    "personal_mail_password_env": (PASSWORD_SUFFIXES, True),
}


def _other_env_values(model: BaseModel, path: str = "") -> set[str]:
    """Every non-empty `*_env` value in the config that is not a mail field."""
    found: set[str] = set()
    for name in type(model).model_fields:
        value = getattr(model, name)
        if isinstance(value, BaseModel):
            found |= _other_env_values(value, f"{path}{name}.")
        elif name.endswith("_env") and isinstance(value, str) and value:
            if path == "discovery." and name in MAIL_FIELDS:
                continue
            found.add(value)
    return found


def name_problem(field: str, value: str, cfg: BlastJobConfig | None = None) -> str | None:
    """Why `value` is not acceptable for mail field `field`, or None. The value
    itself is never echoed."""
    suffixes, blank_ok = MAIL_FIELDS[field]
    if value == "":
        return None if blank_ok else f"{field} must not be blank."
    if not value.endswith(suffixes):
        return f"{field} must end in {' or '.join(suffixes)}."
    if cfg is not None and value in _other_env_values(cfg):
        return f"{field} names a variable another setting uses for a different secret."
    return None

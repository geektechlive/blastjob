"""The one rule for settings that only this computer may change (#87).

A device on the LAN can read the per-launch CSRF token and passes the Origin check, so
it could point the saved mail login at a server it controls. These settings decide where
credentials are sent or where the cockpit listens; see web.md (security model).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from blastjob.models.config import BlastJobConfig
from blastjob.web import packets

# Form field name -> how to read the stored value. One list, so every route shares it.
GUARDED_FIELDS: dict[str, Callable[[BlastJobConfig], str]] = {
    "mail_imap_host": lambda c: c.discovery.mail_imap_host,
    "personal_mail_imap_host": lambda c: c.discovery.personal_mail_imap_host,
    "web_host": lambda c: c.web.host,
    "web_public_url": lambda c: c.web.public_url,
}

REFUSAL = "can only be changed on the computer running blastjob"


def guarded_changes(request: Any, current: BlastJobConfig, submitted: BlastJobConfig) -> list[str]:
    """Names of guarded fields `submitted` would change, for a request not from this
    computer; empty for a loopback peer or when none changes (compared after strip)."""
    if packets.is_local_request(request):
        return []
    return [
        name
        for name, read in GUARDED_FIELDS.items()
        if (read(current) or "").strip() != (read(submitted) or "").strip()
    ]


def refusal_message(fields: list[str]) -> str:
    return f"{', '.join(fields)}: {REFUSAL}. Nothing was saved."

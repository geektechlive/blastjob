"""What `discovery.recruiter_label` may name (#83).

The label is the ONE mail folder the triage reader opens, chosen by the owner so the app sees
only mail they put there. So a name that would widen the read beyond one label is refused:
the inbox, Gmail's all-mail style system folders, and the folders the other mail features
already read. A name that is not plain printable ASCII is refused too, rather than encoded
(IMAP would need modified UTF-7). The message never echoes the offending value.
"""

from __future__ import annotations

MAX_LABEL_CHARS = 100
_FORBIDDEN_CHARS = frozenset('"\\*%')
_SYSTEM_PREFIXES = ("[gmail]", "[google mail]")


def recruiter_label_problem(
    label: str, personal_mailbox: str = "", alert_mailbox: str = ""
) -> str | None:
    """Why `label` cannot be the recruiter label, or None when it is fine (or empty = off).

    `personal_mailbox` and `alert_mailbox` are the configured values (blank is skipped); the
    effective inbox default is refused regardless."""
    if label == "":
        return None
    text = label.strip()
    if not text:
        return "The recruiter label cannot be only spaces. Leave it empty to turn it off."
    if len(text) > MAX_LABEL_CHARS:
        return f"The recruiter label can be at most {MAX_LABEL_CHARS} characters."
    if not all(32 <= ord(ch) < 127 for ch in text):
        return (
            "The recruiter label must be plain printable ASCII (no accents, tabs or line "
            "breaks); other characters need an encoding blastjob does not use."
        )
    if any(ch in _FORBIDDEN_CHARS for ch in text):
        return "The recruiter label may not contain a double quote, backslash, * or %."
    folded = text.casefold()
    if folded == "inbox" or folded.startswith(_SYSTEM_PREFIXES):
        return (
            "The recruiter label must be a label you made for this, not the inbox or a "
            "system folder; blastjob reads only mail you filed there."
        )
    configured = {m.strip().casefold() for m in (personal_mailbox, alert_mailbox) if m.strip()}
    if folded in configured:
        return "The recruiter label is already set as one of the mailboxes blastjob reads."
    return None

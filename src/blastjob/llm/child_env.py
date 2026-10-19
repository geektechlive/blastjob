"""Environment for child processes, minus blastjob's own secrets.

The app's process holds secrets loaded from the secrets file (API keys, Discord
and IMAP credentials). A child must not inherit them: the `claude -p`
subprocess reads attacker-controllable postings, and `open` needs nothing.

This is a denylist on purpose: an allowlist silently breaks other setups
(proxy vars, SSL_CERT_FILE, CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_BASE_URL,
Bedrock/Vertex vars, XDG_CONFIG_HOME) with a CLIError. So only two things go:
every key NAMED in the secrets file (read from the file on each call, so it is
removed even when the shell already had it before the file was loaded), and
ANTHROPIC_API_KEY always (it would switch the claude CLI from the subscription
to API billing).
"""

from __future__ import annotations

import os

from blastjob.secrets import secret_names

ALWAYS_REMOVED = frozenset({"ANTHROPIC_API_KEY"})


def child_env() -> dict[str, str]:
    """A fresh copy of os.environ without blastjob's secrets."""
    removed = ALWAYS_REMOVED | secret_names()
    return {k: v for k, v in os.environ.items() if k not in removed}

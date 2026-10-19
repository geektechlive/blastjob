"""Load API keys from the user's secrets file into the environment at runtime.

blastjob stores only env-var NAMES in its settings; the values live in a
KEY=VALUE file outside the repo (`paths.secrets_file`, default
`<data_dir>/secrets.env`, chmod 600). This reads that file so discovery
adapters find their keys without the shell having sourced it first. Existing
environment values always win (never override).

The path is installed by `config.apply_runtime_settings` on every config load;
before any load it is `<resolved data dir>/secrets.env`.
"""

from __future__ import annotations

import os
from pathlib import Path

_configured: Path | None = None


def set_secrets_path(path: Path) -> None:
    global _configured
    _configured = path


def secrets_path() -> Path:
    if _configured is not None:
        return _configured
    from blastjob.config import resolve_data_dir

    return resolve_data_dir() / "secrets.env"


def secrets_file_hint() -> str:
    """The configured secrets file, for user-facing messages: the path
    `paths.secrets_file` resolves to (installed on every config load), with
    the home directory shortened to `~`. The ONE place messages get it from."""
    path = secrets_path().expanduser()
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _parse(path: Path) -> list[tuple[str, str]]:
    """KEY=VALUE pairs from the file, in order. Missing file -> empty."""
    if not path.exists():
        return []
    pairs: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            pairs.append((key, value.strip().strip('"').strip("'")))
    return pairs


def secret_names(path: Path | None = None) -> frozenset[str]:
    """Every key NAMED in the secrets file, whether or not it was already in
    the environment when the file was loaded. Read fresh each call (the file is
    tiny), so it never goes stale. Unreadable file -> empty."""
    try:
        return frozenset(k for k, _ in _parse(secrets_path() if path is None else path))
    except (OSError, UnicodeDecodeError):
        return frozenset()


def load_secrets_env(path: Path | None = None) -> int:
    """Parse KEY=VALUE lines into os.environ (without overriding). Returns how
    many new keys were set. Missing file is fine (returns 0)."""
    added = 0
    for key, value in _parse(secrets_path() if path is None else path):
        if key not in os.environ:
            os.environ[key] = value
            added += 1
    return added

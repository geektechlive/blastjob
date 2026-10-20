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

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

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


LOOSE_MODE_BITS = 0o077  # any group or other permission bit


def secrets_file_problems(path: Path) -> list[str]:
    """Plain-language problems with the secrets file's mode or owner (empty when
    it is fine or missing). Looks at metadata only; never reads the contents."""
    try:
        st = path.stat()
    except OSError:
        return []
    problems: list[str] = []
    if st.st_mode & LOOSE_MODE_BITS:
        problems.append(f"readable or writable by other users (mode {st.st_mode & 0o777:03o})")
    if st.st_uid != os.geteuid():
        problems.append("owned by another user")
    return problems


_warned: set[tuple[Path, tuple[str, ...]]] = set()


def _warn_if_loose(path: Path) -> None:
    """Log (once per process per file and problem) that the secrets file is loose.
    Warn only: refusing to load could stop the unattended daily run."""
    problems = secrets_file_problems(path)
    key = (path, tuple(problems))
    if not problems or key in _warned:
        return
    _warned.add(key)
    log.warning(
        "secrets file %s is %s; run `chmod 600` on it (values are not logged)",
        path,
        " and ".join(problems),
    )


def load_secrets_env(path: Path | None = None) -> int:
    """Parse KEY=VALUE lines into os.environ (without overriding). Returns how
    many new keys were set. Missing file is fine (returns 0). A file with a loose
    mode or another owner is still loaded, with a logged warning."""
    added = 0
    target = secrets_path() if path is None else path
    _warn_if_loose(target)
    for key, value in _parse(target):
        if key not in os.environ:
            os.environ[key] = value
            added += 1
    return added

"""Where settings come from: the `settings` table of the user's own database.

Bootstrap (no config object needed until the end):
  1. `resolve_data_dir()`: `BLASTJOB_DATA_DIR`, else `paths.data_dir` from a
     legacy `config.toml` (read directly, no model, so a custom legacy data dir
     is still found), else `~/.blastjob`.
  2. connect -> migrate -> `settings_store.import_legacy_once` (first start
     only: an empty table plus a legacy TOML imports it once, under a write
     lock, and leaves the TOML in place as a backup).
  3. load + validate the rows.

`load_config()` runs that per call, so the cockpit and the daily run see an
edit without a restart (except `web.host`/`web.port`, read at launch).
"""

from __future__ import annotations

import os
import sqlite3
import tomllib
from pathlib import Path

from platformdirs import user_config_dir

from blastjob.models.config import DATA_DIR_ENV, DEFAULT_DATA_DIR, BlastJobConfig, PathsConfig

_CONFIG_DIR = Path(user_config_dir("blastjob"))
_CONFIG_FILE = _CONFIG_DIR / "config.toml"
DB_NAME = "blastjob.db"


def legacy_config_path() -> Path:
    """The pre-database config file (platformdirs), if one was ever written."""
    return _CONFIG_FILE


def _legacy_data_dir() -> str:
    path = legacy_config_path()
    if not path.is_file():
        return ""
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    value = (raw.get("paths") or {}).get("data_dir", "")
    return value.strip() if isinstance(value, str) else ""


def resolve_data_dir() -> Path:
    raw = os.environ.get(DATA_DIR_ENV, "").strip() or _legacy_data_dir() or DEFAULT_DATA_DIR
    return Path(os.path.expanduser(raw))


def apply_runtime_settings(cfg: BlastJobConfig) -> BlastJobConfig:
    """Install the settings that pure, config-free helpers read at call time
    (`company_key` aliases, agency names, network agency keywords, alert
    subject words, the secrets file, the model gateway's call ledger path and caps). Called by
    every config load, so each request or run sees the current values; returns `cfg` for
    chaining."""
    from blastjob import secrets
    from blastjob.core import agencies, company, employer_domains, network_views
    from blastjob.discovery import mailalerts
    from blastjob.llm import ledger

    d = cfg.discovery
    company.set_company_aliases(d.company_aliases)
    employer_domains.set_employer_domains(d.employer_domains)
    agencies.set_extra_agencies(d.agency_companies)
    network_views.set_extra_agency_keywords(d.network_agency_keywords)
    mailalerts.set_alert_subject_terms(d.mail_alert_subject_terms)
    secrets.set_secrets_path(secrets_path(cfg))
    # The model gateway's call ledger and budget (llm/ledger.py) find their database and caps here.
    ledger.install(data_dir(cfg) / DB_NAME, cfg.limits, cfg.pricing)
    return cfg


PRIVATE_DIR_MODE = 0o700


def ensure_private_dir(path: Path) -> Path:
    """mkdir -p, with the directory THIS call creates set owner-only (0700): the
    data dir holds a person's job search. An existing directory keeps the
    permissions its owner gave it."""
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, PRIVATE_DIR_MODE)
    return path


def _open(data_dir: Path) -> sqlite3.Connection:
    from blastjob.db import client as dbc

    return dbc.init_db_at(data_dir / DB_NAME)


def load_config_from_db(db_path: Path) -> BlastJobConfig:
    """Settings stored in one database file (read-only use: golden checks,
    regression scripts). The data dir is the file's directory."""
    from blastjob import settings_store

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
        ).fetchone()
        if not has_table:
            return BlastJobConfig(paths=PathsConfig(data_dir=str(db_path.parent)))
        return settings_store.load(conn, db_path.parent).cfg
    finally:
        conn.close()


def load_config() -> BlastJobConfig:
    from blastjob import settings_store

    data_dir = resolve_data_dir()
    conn = _open(data_dir)
    try:
        settings_store.import_legacy_once(conn, legacy_config_path())
        cfg = settings_store.load(conn, data_dir).cfg
    finally:
        conn.close()
    return apply_runtime_settings(cfg)


def save_config(cfg: BlastJobConfig) -> None:
    """Upsert the settings that changed. Raises `settings_store.StaleSettings`
    when `cfg` was loaded at a revision that is no longer current."""
    from blastjob import settings_store

    conn = _open(data_dir(cfg))
    try:
        settings_store.save(conn, cfg)
    finally:
        conn.close()


def data_dir(cfg: BlastJobConfig) -> Path:
    return Path(os.path.expanduser(cfg.paths.data_dir))


def output_dir(cfg: BlastJobConfig) -> Path:
    return Path(os.path.expanduser(cfg.paths.output_dir))


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})


def is_loopback_host(host: str) -> bool:
    h = host.strip().lower()
    return h in LOOPBACK_HOSTS or h.startswith("127.")


def cockpit_url(host: str, port: int) -> str:
    """The URL a browser on this machine uses: a wildcard bind (0.0.0.0) is
    not visitable, so it maps to loopback."""
    h = "127.0.0.1" if host.strip() in WILDCARD_HOSTS else host.strip()
    h = f"[{h}]" if ":" in h else h
    return f"http://{h}:{port}"


def secrets_path(cfg: BlastJobConfig) -> Path:
    raw = cfg.paths.secrets_file.strip()
    return Path(os.path.expanduser(raw)) if raw else data_dir(cfg) / "secrets.env"

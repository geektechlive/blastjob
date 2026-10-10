"""`blastjob setup doctor`: is this install ready to run? Every check says
what to do about a failure. Spends no tokens and sends nothing."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from blastjob import config as cfg_mod
from blastjob import settings_store as ss
from blastjob.db.schema import MIGRATIONS
from blastjob.models.config import BlastJobConfig

JINA_KEY_ENV = "JINA_API_KEY"
CLI_TIMEOUT_S = 15

# (argv) -> (returncode, output); injectable so tests never run a real binary.
Runner = Callable[[list[str]], tuple[int, str]]


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    required: bool = True


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=CLI_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return r.returncode, (r.stdout or r.stderr).strip()


def check_database(conn: sqlite3.Connection, path: Path) -> list[Check]:
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
        writable = Check("database writable", True, str(path))
    except sqlite3.Error as e:
        writable = Check("database writable", False, str(e), f"check permissions on {path.parent}")
    latest = max(v for v, _ in MIGRATIONS)
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    current = int(row[0])
    migrated = Check(
        "migrations current",
        current == latest,
        f"schema {current} of {latest}",
        "run any blastjob command once with this checkout (it migrates on open)",
    )
    return [writable, migrated]


def check_settings(conn: sqlite3.Connection, data_dir: Path) -> tuple[list[Check], BlastJobConfig]:
    try:
        loaded = ss.load(conn, data_dir)
    except ss.SettingsError as e:
        fix = "correct it with `blastjob setup set <key> <value>`"
        return [Check("settings valid", False, str(e), fix)], BlastJobConfig()
    checks = [Check("settings valid", True, f"{len(ss.read_rows(conn))} stored")]
    if loaded.unknown:
        checks.append(
            Check(
                "unknown settings",
                False,
                ", ".join(sorted(loaded.unknown)),
                "kept as-is; remove them only if you know they are obsolete",
                required=False,
            )
        )
    return checks, loaded.cfg


def check_profile(cfg: BlastJobConfig) -> list[Check]:
    wh = cfg_mod.data_dir(cfg) / "work_history.md"
    ingest = cfg_mod.cockpit_url(cfg.web.host, cfg.web.port) + "/work-history/ingest"
    return [
        Check(
            "work_history.md present",
            wh.is_file(),
            str(wh),
            f"start `blastjob-web` and ingest your resume at {ingest}",
        ),
        Check(
            "candidate.target_titles set",
            cfg.candidate.is_configured(),
            "; ".join(cfg.candidate.target_titles),
            "blastjob setup set candidate.target_titles '[\"Your Title\"]'",
        ),
    ]


def check_llm(cfg: BlastJobConfig, run: Runner | None = None) -> Check:
    from blastjob.llm.providers import ProviderNotConfiguredError, detect_provider

    try:
        provider = detect_provider(cfg.llm)
    except ProviderNotConfiguredError:
        return Check(
            "LLM provider",
            False,
            "none found",
            "install the claude CLI and log in (no key needed), or put ANTHROPIC_API_KEY or "
            f"OPENAI_API_KEY in your secrets file ({cfg_mod.secrets_path(cfg)})",
        )
    env = {"anthropic": cfg.llm.anthropic_api_key_env, "openai": cfg.llm.openai_api_key_env}
    if provider in env:
        name = env[provider]
        ok = bool(os.environ.get(name))
        return Check("LLM provider", ok, f"{provider} ({name})", f"set {name} in your secrets file")
    if not shutil.which("claude"):
        return Check("LLM provider", False, "claude CLI not on PATH", "install the claude CLI")
    code, out = (run or _run)(["claude", "--version"])
    return Check("LLM provider", code == 0, f"claude-cli {out[:60]}", "run `claude` once to log in")


def _secret_names(cfg: BlastJobConfig) -> list[tuple[str, str, bool]]:
    """(env var, what needs it, required) for every secret the settings use."""
    d, out = cfg.discovery, []
    if d.mail_alerts_enabled:
        out += [
            (d.mail_user_env, "alert mailbox", True),
            (d.mail_password_env, "alert mailbox", True),
        ]
    for name in (d.personal_mail_user_env, d.personal_mail_password_env):
        if name:
            out.append((name, "personal mailbox", True))
    if d.enable_aggregators:
        out += [(d.adzuna_app_id_env, "Adzuna", True), (d.adzuna_app_key_env, "Adzuna", True)]
    if cfg.notify.mode == "discord":
        n = cfg.notify
        out += [
            (n.discord_token_env, "Discord digest", True),
            (n.discord_channel_env, "Discord", True),
        ]
    if (d.search_firms_enabled and d.search_firms) or (d.apple_enabled and d.apple_queries):
        out.append((JINA_KEY_ENV, "search-firm / Apple discovery", False))
    return out


def check_secrets(cfg: BlastJobConfig) -> list[Check]:
    from blastjob.secrets import load_secrets_env

    path = cfg_mod.secrets_path(cfg)
    load_secrets_env(path)
    exists = Check(
        "secrets file",
        path.is_file(),
        str(path),
        f"create {path} (chmod 600) with KEY=VALUE lines, if you use any keys",
        required=False,
    )
    return [exists] + [
        Check(
            f"{name} set",
            bool(os.environ.get(name)),
            f"needed by {why}",
            f"add {name}=... to {path} (never to settings)",
            required,
        )
        for name, why, required in _secret_names(cfg)
    ]


def run_doctor(run: Runner | None = None) -> list[Check]:
    data_dir = cfg_mod.resolve_data_dir()
    path = data_dir / cfg_mod.DB_NAME
    from blastjob.db import client as dbc

    conn = dbc.init_db_at(path)
    try:
        ss.import_legacy_once(conn, cfg_mod.legacy_config_path())
        checks = check_database(conn, path)
        settings_checks, cfg = check_settings(conn, data_dir)
    finally:
        conn.close()
    checks += settings_checks
    checks += check_profile(cfg)
    checks.append(check_llm(cfg, run))
    checks += check_secrets(cfg)
    return checks


def format_checks(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        mark = "ok  " if c.ok else ("FAIL" if c.required else "warn")
        lines.append(f"[{mark}] {c.name}" + (f" -- {c.detail}" if c.detail else ""))
        if not c.ok and c.fix:
            lines.append(f"       fix: {c.fix}")
    return "\n".join(lines)


def failed(checks: list[Check]) -> bool:
    return any(c.required and not c.ok for c in checks)

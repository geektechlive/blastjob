"""Settings in the database: one `settings` row per dotted leaf key.

Row format (migration 44): `key` is the dotted path to a non-model field of
`BlastJobConfig` ("discovery.comp_floor_usd", "discovery.towns",
"candidate.target_titles"), `value` its JSON. A list of boards or a towns map
is ONE leaf: the model decides what a leaf is, not the JSON shape.

Contract:
- Loading validates every known row through Pydantic. A key the model does not
  know is kept in the table, carried untouched by every save, and reported
  (`LoadedSettings.unknown`), never dropped.
- Saves upsert only the keys whose value changed; nothing ever rewrites or
  deletes the whole table.
- A save can carry the revision (max `updated_at`) its edit was based on; a
  stale one raises `StaleSettings` instead of overwriting a newer edit.
- `paths.data_dir` is never stored: the database lives inside the data dir, so
  the data dir is found first (`config.resolve_data_dir`).
- Rows whose key starts with `_meta.` are bookkeeping (the legacy import
  marker), invisible to the model.
"""

from __future__ import annotations

import json
import sqlite3
import tomllib
import typing
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel, ValidationError

from blastjob.models.config import BlastJobConfig

META_PREFIX = "_meta."
IMPORT_MARKER = "_meta.imported_from"
BOOTSTRAP_KEYS = frozenset({"paths.data_dir"})

# Fields that hold the NAME of an environment variable, never a secret value.
# `set_value` / imports refuse anything not shaped like one. This is a shape
# check on these fields only -- not secret detection.
SECRET_REF_KEYS = frozenset(
    {
        "llm.anthropic_api_key_env",
        "llm.openai_api_key_env",
        "discovery.adzuna_app_id_env",
        "discovery.adzuna_app_key_env",
        "discovery.mail_user_env",
        "discovery.mail_password_env",
        "discovery.personal_mail_user_env",
        "discovery.personal_mail_password_env",
        "notify.discord_token_env",
        "notify.discord_channel_env",
    }
)
_BLANK_OK = frozenset({"discovery.personal_mail_user_env", "discovery.personal_mail_password_env"})
# A file path (the secrets file): absolute after `~` expansion, and never inside
# this checkout, where it could be committed. It may not exist yet (doctor warns).
PATH_KEYS = frozenset({"paths.secrets_file"})
REPO_ROOT = Path(__file__).resolve().parents[2]


class SettingsError(ValueError):
    """A setting failed validation; the message names the key."""


class StaleSettings(RuntimeError):
    """The settings changed since the edit being saved was loaded."""


@dataclass(frozen=True)
class LoadedSettings:
    cfg: BlastJobConfig
    unknown: dict[str, Any] = field(default_factory=dict)
    revision: str = ""


# --- the model's leaves ----------------------------------------------------


def _submodel(annotation: Any) -> type[BaseModel] | None:
    return (
        annotation if isinstance(annotation, type) and issubclass(annotation, BaseModel) else None
    )


def _walk(model: type[BaseModel], prefix: str = "") -> Iterator[tuple[str, Any]]:
    hints = typing.get_type_hints(model)
    for name in model.model_fields:
        sub = _submodel(hints.get(name))
        if sub is not None:
            yield from _walk(sub, f"{prefix}{name}.")
        else:
            yield f"{prefix}{name}", hints.get(name)


LEAVES: dict[str, Any] = dict(_walk(BlastJobConfig))
SECTIONS = frozenset(k.rsplit(".", 1)[0] for k in LEAVES if "." in k)


def is_string_leaf(key: str) -> bool:
    ann = LEAVES.get(key)
    if ann is str:
        return True
    args = typing.get_args(ann)
    return typing.get_origin(ann) is typing.Literal and all(isinstance(a, str) for a in args)


def is_list_leaf(key: str) -> bool:
    return typing.get_origin(LEAVES.get(key)) is list


def is_string_list_leaf(key: str) -> bool:
    return is_list_leaf(key) and typing.get_args(LEAVES.get(key)) == (str,)


def list_hint(key: str) -> str:
    """How to pass a list on the command line, quoted exactly."""
    return (
        f"{key} expects a JSON list, e.g. "
        f'blastjob setup set {key} \'["First item", "Second item"]\' '
        "(single quotes around the list, double quotes around each item)"
    )


# --- flatten / unflatten ---------------------------------------------------


def flatten(raw: Mapping[str, Any], prefix: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    """(known leaves, unknown leaves) of a nested mapping such as a TOML file."""
    known: dict[str, Any] = {}
    unknown: dict[str, Any] = {}
    for key, value in raw.items():
        full = f"{prefix}{key}"
        if full in LEAVES:
            known[full] = value
        elif isinstance(value, Mapping) and (full in SECTIONS or full not in LEAVES):
            k, u = flatten(value, f"{full}.")
            known.update(k)
            unknown.update(u)
        else:
            unknown[full] = value
    return known, unknown


def unflatten(flat: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in flat.items():
        node = out
        *parents, leaf = key.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return out


def effective(cfg: BlastJobConfig) -> dict[str, Any]:
    """Every known leaf of `cfg` as its JSON value (defaults included)."""
    known, _ = flatten(cfg.model_dump(mode="json", exclude={"anthropic"}))
    return {k: v for k, v in known.items() if k not in BOOTSTRAP_KEYS}


def validate(known: Mapping[str, Any]) -> BlastJobConfig:
    try:
        return BlastJobConfig.model_validate(unflatten(known))
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "settings"
        raise SettingsError(f"{where}: {first['msg']}") from e


# --- rows --------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def revision(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT COALESCE(MAX(updated_at), '') FROM settings").fetchone()
    return str(row[0])


def _next_stamp(conn: sqlite3.Connection) -> str:
    """A stamp strictly after the current revision, so every write moves it."""
    now, current = _now(), revision(conn)
    if current and now <= current:
        now = (datetime.fromisoformat(current) + timedelta(microseconds=1)).isoformat(
            timespec="microseconds"
        )
    return now


def read_rows(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    out: dict[str, Any] = {}
    for key, value in rows:
        if str(key).startswith(META_PREFIX):
            continue
        try:
            out[str(key)] = json.loads(value)
        except json.JSONDecodeError as e:
            raise SettingsError(f"{key}: stored value is not valid JSON") from e
    return out


def _upsert(conn: sqlite3.Connection, values: Mapping[str, Any], stamp: str) -> None:
    conn.executemany(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        [(k, json.dumps(v, sort_keys=True), stamp) for k, v in values.items()],
    )


def load(conn: sqlite3.Connection, data_dir: Path) -> LoadedSettings:
    rows = read_rows(conn)
    known = {k: v for k, v in rows.items() if k in LEAVES and k not in BOOTSTRAP_KEYS}
    unknown = {k: v for k, v in rows.items() if k not in known}
    cfg = validate(known)
    cfg.paths.data_dir = str(data_dir)
    rev = revision(conn)
    return LoadedSettings(cfg=cfg.with_revision(rev), unknown=unknown, revision=rev)


def save(
    conn: sqlite3.Connection, cfg: BlastJobConfig, *, expected_revision: str | None = None
) -> list[str]:
    """Upsert the leaves of `cfg` that differ from what is stored now. Returns
    the keys written. `expected_revision` (default: the revision `cfg` was
    loaded at, if any) must still be current, else `StaleSettings`."""
    expected = cfg.settings_revision if expected_revision is None else expected_revision
    new = effective(validate(effective(cfg)))
    # Same reference checks as set_value/import_mapping: a form field for an
    # *_env name or the secrets path must not be a way around them.
    _check_refs(new)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if expected is not None and revision(conn) != expected:
            raise StaleSettings("settings changed since this page was loaded")
        current = effective(load(conn, Path(cfg.paths.data_dir)).cfg)
        changed = {k: v for k, v in new.items() if current.get(k) != v}
        if changed:
            _upsert(conn, changed, _next_stamp(conn))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return sorted(changed)


# --- single keys (the setup CLI) --------------------------------------------


def _path_error(key: str, value: Any) -> str | None:
    if not isinstance(value, str):
        return f"{key} must be a string"
    if value == "":
        return None
    path = Path(value).expanduser()
    if "=" in value or value.strip() != value or not path.is_absolute():
        return f"{key} must be an absolute file path (e.g. ~/secrets.env)"
    if path.resolve().is_relative_to(REPO_ROOT):
        return f"{key} must be outside the blastjob checkout, where it could be committed"
    return None


def _ref_error(key: str, value: Any) -> str | None:
    from blastjob.web.mail_settings import valid_env_name

    if key in PATH_KEYS:
        return _path_error(key, value)
    if not isinstance(value, str):
        return f"{key} must be a string"
    if value == "" and key in _BLANK_OK:
        return None
    if not valid_env_name(value):
        return f"{key} must be the NAME of an environment variable (e.g. MY_APP_PASSWORD)"
    return None


def parse_value(key: str, text: str) -> Any:
    """CLI text -> a value: JSON when it parses (strings may be bare). For a
    list-of-strings key, a bare string is a one-item list ("Registered Nurse"
    -> ["Registered Nurse"]); any other list key needs real JSON."""
    if is_string_leaf(key) and not text.startswith('"'):
        return text
    if is_string_list_leaf(key) and not text.lstrip().startswith("["):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = text
        return [value] if isinstance(value, str) else value
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        if is_string_leaf(key):
            return text
        if is_list_leaf(key):
            raise SettingsError(list_hint(key)) from e
        raise SettingsError(f'{key}: value must be JSON (e.g. 200000, true, ["a"])') from e


def set_value(conn: sqlite3.Connection, key: str, value: Any) -> Any:
    """Validate one known key against the whole model and upsert it. Returns
    the stored (normalized) value."""
    if key not in LEAVES or key in BOOTSTRAP_KEYS:
        raise SettingsError(f"unknown setting: {key}")
    if key in SECRET_REF_KEYS | PATH_KEYS and (err := _ref_error(key, value)):
        raise SettingsError(err)
    if is_list_leaf(key) and not isinstance(value, list):
        raise SettingsError(list_hint(key))
    rows = read_rows(conn)
    known = {k: v for k, v in rows.items() if k in LEAVES and k not in BOOTSTRAP_KEYS}
    stored = effective(validate({**known, key: value}))[key]
    conn.execute("BEGIN IMMEDIATE")
    try:
        _upsert(conn, {key: stored}, _next_stamp(conn))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return stored


# --- TOML import / export ----------------------------------------------------


def _check_refs(known: Mapping[str, Any]) -> None:
    for key in (SECRET_REF_KEYS | PATH_KEYS) & known.keys():
        if err := _ref_error(key, known[key]):
            raise SettingsError(err)


def import_mapping(conn: sqlite3.Connection, raw: Mapping[str, Any], *, source: str = "") -> int:
    """Upsert every key present in `raw` (known ones validated together with
    what is already stored; unknown ones carried as-is). One transaction.
    Returns the number of rows written."""
    known, unknown = flatten(raw)
    known = {k: v for k, v in known.items() if k not in BOOTSTRAP_KEYS}
    _check_refs(known)
    rows = read_rows(conn)
    stored = {k: v for k, v in rows.items() if k in LEAVES and k not in BOOTSTRAP_KEYS}
    merged = effective(validate({**stored, **known}))
    values = {**{k: merged[k] for k in known}, **unknown}
    conn.execute("BEGIN IMMEDIATE")
    try:
        stamp = _next_stamp(conn)
        _upsert(conn, values, stamp)
        if source:
            _upsert(conn, {IMPORT_MARKER: {"path": source, "at": stamp}}, stamp)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return len(values)


def import_toml(conn: sqlite3.Connection, path: Path) -> int:
    return import_toml_report(conn, path)[0]


def import_toml_report(conn: sqlite3.Connection, path: Path) -> tuple[int, list[str]]:
    """Import a TOML file; (rows written, unknown keys kept as-is, sorted)."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    _, unknown = flatten(raw)
    return import_mapping(conn, raw), sorted(unknown)


def import_legacy_once(conn: sqlite3.Connection, legacy: Path) -> bool:
    """First start only: if the settings table is empty and a legacy
    config.toml exists, import it and record an `_meta.imported_from` marker,
    all under one write lock, so two processes starting together import once.
    The TOML is left in place as a backup.

    Called on every config load, so the common case (rows already there) is a
    plain read that never waits on another process's write; the write lock is
    taken only when the table looks empty, then re-checked under it."""
    if conn.execute("SELECT 1 FROM settings LIMIT 1").fetchone() or not legacy.is_file():
        return False
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM settings LIMIT 1").fetchone() or not legacy.is_file():
            conn.rollback()
            return False
        with open(legacy, "rb") as f:
            raw = tomllib.load(f)
        known, unknown = flatten(raw)
        known = {k: v for k, v in known.items() if k not in BOOTSTRAP_KEYS}
        merged = effective(validate(known))
        stamp = _now()
        _upsert(conn, {**{k: merged[k] for k in known}, **unknown}, stamp)
        _upsert(conn, {IMPORT_MARKER: {"path": str(legacy), "at": stamp}}, stamp)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return True


def export_toml(conn: sqlite3.Connection, cfg: BlastJobConfig | None = None) -> str:
    """The stored rows as TOML (or, with `cfg`, every effective value)."""
    flat = effective(cfg) if cfg is not None else read_rows(conn)
    return tomli_w.dumps(unflatten(flat))

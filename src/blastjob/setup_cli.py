"""`blastjob setup ...`: read and write settings from a terminal (or an AI
assistant setting the app up for its user).

    blastjob setup doctor                  # is this install ready? non-zero exit if not
    blastjob setup get [dotted.key]        # one value as JSON, or every value as TOML
    blastjob setup set <dotted.key> <v>    # v is JSON; strings may be bare
    blastjob setup import <file.toml>      # upsert every key in the file
    blastjob setup export [--all]          # stored rows (or every value) as TOML

Values for `set`:
    numbers and booleans    blastjob setup set discovery.comp_floor_usd 120000
                            blastjob setup set discovery.closure_auto true
    plain strings           blastjob setup set discovery.home_state OH
    lists (JSON)            blastjob setup set candidate.target_titles
                                '["Registered Nurse", "Charge Nurse"]'
                            (one shell argument: single quotes outside,
                            double quotes around each item)
    one-item string list    blastjob setup set candidate.target_titles "Registered Nurse"

Settings live in `<data dir>/blastjob.db`; the data dir is `BLASTJOB_DATA_DIR`
or `~/.blastjob`. Fields ending in `_env` take the NAME of an environment
variable, never its value; the values go in the secrets file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from blastjob import config as cfg_mod
from blastjob import settings_store as ss

LAN_WARNING = (
    "warning: web.host {host} is not loopback. The cockpit has NO login: after a "
    "restart, anyone who can reach this machine on the network can read your job "
    "search and trigger LLM calls. Use 127.0.0.1 unless you mean that."
)


def _conn():
    from blastjob.db import client as dbc

    conn = dbc.init_db_at(cfg_mod.resolve_data_dir() / cfg_mod.DB_NAME)
    ss.import_legacy_once(conn, cfg_mod.legacy_config_path())
    return conn


def _get(key: str | None) -> int:
    conn = _conn()
    try:
        loaded = ss.load(conn, cfg_mod.resolve_data_dir())
    finally:
        conn.close()
    values = {**ss.effective(loaded.cfg), **loaded.unknown}
    if key is None:
        print(ss.tomli_w.dumps(ss.unflatten(values)), end="")
        return 0
    if key not in values:
        print(f"unknown setting: {key}", file=sys.stderr)
        return 2
    print(json.dumps(values[key], indent=2))
    return 0


def _set(key: str, text: str) -> int:
    conn = _conn()
    try:
        stored = ss.set_value(conn, key, ss.parse_value(key, text))
    finally:
        conn.close()
    print(f"{key} = {json.dumps(stored)}")
    if ss.is_string_list_leaf(key) and not text.lstrip().startswith("["):
        print("note: stored as a one-item list. " + ss.list_hint(key), file=sys.stderr)
    if key == "web.host" and not cfg_mod.is_loopback_host(str(stored)):
        print(LAN_WARNING.format(host=stored), file=sys.stderr)
    return 0


def _import(path: str) -> int:
    conn = _conn()
    try:
        n, unknown = ss.import_toml_report(conn, Path(path))
    finally:
        conn.close()
    print(f"imported {n} setting(s) from {path}")
    if unknown:
        print(f"unknown settings (kept as-is, not used): {', '.join(unknown)}")
    return 0


def _export(include_all: bool) -> int:
    conn = _conn()
    try:
        cfg = ss.load(conn, cfg_mod.resolve_data_dir()).cfg if include_all else None
        print(ss.export_toml(conn, cfg), end="")
    finally:
        conn.close()
    return 0


def _doctor() -> int:
    from blastjob import setup_doctor

    checks = setup_doctor.run_doctor()
    print(setup_doctor.format_checks(checks))
    return 1 if setup_doctor.failed(checks) else 0


SET_EPILOG = """\
examples:
  blastjob setup set discovery.comp_floor_usd 120000
  blastjob setup set discovery.home_state OH
  blastjob setup set candidate.target_titles '["Registered Nurse", "Charge Nurse"]'
  blastjob setup set candidate.target_titles "Registered Nurse"   # a one-item list
"""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="blastjob", description="blastjob command line")
    sub = ap.add_subparsers(dest="command", required=True)
    setup = sub.add_parser(
        "setup",
        help="read and write settings",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ops = setup.add_subparsers(dest="op", required=True)
    ops.add_parser("doctor", help="check the install; non-zero exit on any failure")
    get = ops.add_parser("get", help="print one setting (JSON) or all (TOML)")
    get.add_argument("key", nargs="?")
    st = ops.add_parser(
        "set",
        help="validate and store one setting",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=SET_EPILOG,
    )
    st.add_argument("key")
    st.add_argument("value")
    imp = ops.add_parser("import", help="store every key in a TOML file")
    imp.add_argument("path")
    exp = ops.add_parser("export", help="print the stored settings as TOML")
    exp.add_argument("--all", action="store_true", help="include defaults (every value)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.op == "doctor":
            return _doctor()
        if args.op == "get":
            return _get(args.key)
        if args.op == "set":
            return _set(args.key, args.value)
        if args.op == "import":
            return _import(args.path)
        return _export(args.all)
    except ss.SettingsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

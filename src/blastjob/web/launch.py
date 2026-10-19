"""Launch the cockpit: uvicorn on `web.host` (default 127.0.0.1, loopback), open the
browser, recover any builds left mid-flight by a previous crash.

Runs standalone (`blastjob-web`, browser opens) or as a launchd service
(`blastjob-web --no-browser`, `RunAtLoad`/`KeepAlive`) so the Discord daily
digest's cockpit links resolve on this computer without starting it by hand."""

from __future__ import annotations

import argparse
import logging
import threading
import time
import webbrowser

import uvicorn

from blastjob import config as cfg_mod
from blastjob import logging as applog
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig
from blastjob.web.app import create_app

HOST = "127.0.0.1"
# Binding anywhere other than loopback exposes a tool with no login to whoever
# shares the network, so it is a flag -- opt in per launch, never the default.
PORT = 8765

log = logging.getLogger(__name__)


def _recover_on_start(cfg: BlastJobConfig) -> None:
    conn = dbc.init_db(cfg)
    try:
        n = dbc.recover_stale_builds(conn)
        if n:
            log.info("recovered %d stale build(s) from a prior run", n)
    finally:
        conn.close()


def _open_browser(port: int, host: str = HOST) -> None:
    time.sleep(1.0)
    # 0.0.0.0 is a bind address, not something a browser can visit.
    webbrowser.open(cfg_mod.cockpit_url(host, port) + "/")


def _start_browser_thread(port: int, host: str = HOST) -> None:
    threading.Thread(target=_open_browser, args=(port, host), daemon=True).start()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="blastjob-web", description=__doc__.split("\n")[0])
    ap.add_argument(
        "--no-browser",
        action="store_true",
        help="don't open a browser tab on start (for launchd/service runs)",
    )
    ap.add_argument(
        "--port", type=int, default=None, help="port to bind (overrides the web.port setting)"
    )
    ap.add_argument(
        "--host",
        default=None,
        help=(
            f"address to bind (overrides the web.host setting, default {HOST}, loopback "
            "only). 0.0.0.0 reaches the cockpit from another device on your LAN. There is "
            "no login: anyone on the network can then read your job search and spend your "
            "LLM budget."
        ),
    )
    args = ap.parse_args(argv)
    host, port = bind_address(cfg_mod.load_config(), args.host, args.port)

    # Give the web/discovery/matching path the same disk log as the rest of the app,
    # so a failed background run is debuggable after the fact (not just a transient
    # SSE line lost on restart).
    applog.setup()
    _recover_on_start(cfg_mod.load_config())
    if not args.no_browser:
        _start_browser_thread(port, host)
    if host != HOST:
        log.warning(
            "binding to %s — the cockpit has no login; anyone on this network can read "
            "your job search and trigger builds",
            host,
        )
    print(f"blastjob cockpit: {cfg_mod.cockpit_url(host, port)}", flush=True)
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


def bind_address(cfg: BlastJobConfig, host: str | None, port: int | None) -> tuple[str, int]:
    """`web.host`/`web.port` from settings, read once at startup; a CLI flag
    overrides. A settings change takes effect when the cockpit restarts."""
    return (host or cfg.web.host or HOST), (port or cfg.web.port or PORT)


if __name__ == "__main__":
    main()

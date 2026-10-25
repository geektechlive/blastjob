"""The model-call ledger and the atomic call budget (docs/how-it-works/llm.md).

Every model call the gateway lets through has a row in `model_calls`. The row is the
reservation: `reserve` runs `BEGIN IMMEDIATE`, counts today's chargeable rows for the call's
origin, inserts the `started` row and commits, and only then may the provider be contacted. Two
processes (the daily run and the cockpit) racing for the last slot are serialised by SQLite's
write lock, so exactly one of them starts. Over the cap the refusal itself is written as a
`refused_budget` row (which never counts toward the cap) and the gateway raises.

`finish` closes the row once. A NULL token count or cost is unknown, never zero (`cost_known`
0). A row whose owner process died before `finish` becomes `abandoned` with unknown cost, and
still counts against the cap because it may have spent.

Nothing is installed until `config.apply_runtime_settings` calls `install` (the way company
aliases reach `company_key`). With nothing installed, which is how tests and tools without a
config run, the ledger and the budget are a no-op: no row is written and nothing is refused.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from blastjob.models.config import LimitsConfig, PricingConfig

log = logging.getLogger(__name__)

PS_TIMEOUT_SECONDS = 3.0
ORIGINS = ("scheduled", "clicked")


@dataclass(frozen=True)
class Installed:
    db_path: Path
    limits: LimitsConfig
    pricing: PricingConfig


_installed: Installed | None = None
_reconciled: set[str] = set()
_own_start: str | None = None


def install(db_path: Path, limits: LimitsConfig, pricing: PricingConfig) -> None:
    """Point the gateway at one database with these limits. Called on every config load."""
    global _installed
    _installed = Installed(db_path=db_path, limits=limits, pricing=pricing)


def uninstall() -> None:
    """Back to the no-op state. For tests; production never needs it."""
    global _installed, _own_start
    _installed = None
    _own_start = None
    _reconciled.clear()


def installed() -> Installed | None:
    return _installed


def cap_for(origin: str, limits: LimitsConfig) -> int:
    return limits.scheduled_calls_per_day if origin == "scheduled" else limits.clicked_calls_per_day


# ---------------------------------------------------------------------------
# Process identity (a PID alone is reused by the OS)
# ---------------------------------------------------------------------------


def process_start_time(pid: int) -> str:
    """The OS's start time for `pid` ("" when it cannot be read). `ps -o lstart=` works on macOS
    and Linux without a new dependency; `LC_ALL=C` keeps the text stable across locales."""
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, an int PID
            ["ps", "-o", "lstart=", "-p", str(int(pid))],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_SECONDS,
            env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"},
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return " ".join(out.split())


def own_start_time() -> str:
    global _own_start
    if _own_start is None:
        _own_start = process_start_time(os.getpid())
    return _own_start


def owner_alive(pid: int, started: str, *, since: str = "") -> bool:
    """True while the process that owns a row may still be running. A PID that now has a
    different start time was reused by an unrelated process, so its owner is dead. When the
    start time cannot be read the owner is assumed alive: abandoning a live call is worse than
    leaving a dead one for the next check."""
    if pid == os.getpid():
        # Same PID is not enough: a restarted cockpit can be handed its old PID.
        own = own_start_time()
        return not (started and own and started != own)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists, owned by someone else
    except OSError:
        return False
    if not started:
        # No start time was recorded, so a reused PID cannot be told apart. Past a day, assume
        # the call is long dead so the row cannot look live forever (it still counts).
        return not since or since[:19] >= _a_day_ago()
    actual = process_start_time(pid)
    return actual == "" or actual == started


# ---------------------------------------------------------------------------
# Connection and time
# ---------------------------------------------------------------------------


def _connect(path: Path) -> sqlite3.Connection:
    from blastjob.db import client as dbc  # lazy: client imports config, config imports this

    conn = dbc.init_db_at(path)
    conn.isolation_level = None  # explicit BEGIN/COMMIT only
    return conn


def _a_day_ago() -> str:
    return (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def day_bounds(day: date) -> tuple[str, str]:
    """[start, end) of a LOCAL day as ISO prefixes that compare against `started_at` text."""
    return day.isoformat(), (day + timedelta(days=1)).isoformat()


# ---------------------------------------------------------------------------
# Reserve, finish, reconcile
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Admission:
    """`row_id` None with `refused` False means the ledger is off (nothing installed)."""

    row_id: int | None
    refused: bool = False
    used: int = 0
    cap: int = 0


def reserve(caller: str, origin: str) -> Admission:
    """Atomically count, then insert. Raises `sqlite3.Error` if the database is unusable (the
    gateway turns that into a refused call: an unmeasured call is never waved through)."""
    inst = _installed
    if inst is None:
        return Admission(row_id=None)
    if str(inst.db_path) not in _reconciled:
        reconcile_abandoned(inst.db_path)
    conn = _connect(inst.db_path)
    try:
        start, end = day_bounds(date.today())
        cap = cap_for(origin, inst.limits)
        conn.execute("BEGIN IMMEDIATE")
        try:
            used = int(
                conn.execute(
                    "SELECT COUNT(*) FROM model_calls WHERE origin=? AND outcome != "
                    "'refused_budget' AND started_at >= ? AND started_at < ?",
                    (origin, start, end),
                ).fetchone()[0]
            )
            refused = used >= cap
            now = now_iso()
            if refused and _already_refused_today(conn, origin, caller, start, end):
                # One refusal row per origin and caller per day: a retry loop cannot bloat the
                # table. The call is still refused; /runs counts distinct refused callers.
                conn.execute("COMMIT")
                return Admission(row_id=None, refused=True, used=used, cap=cap)
            cur = conn.execute(
                "INSERT INTO model_calls (started_at, ended_at, caller, origin, outcome, "
                "cost_known, owner_pid, owner_started) VALUES (?,?,?,?,?,0,?,?)",
                (
                    now,
                    now if refused else None,
                    caller,
                    origin,
                    "refused_budget" if refused else "started",
                    os.getpid(),
                    own_start_time(),
                ),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return Admission(row_id=cur.lastrowid, refused=refused, used=used, cap=cap)
    finally:
        conn.close()


def _already_refused_today(
    conn: sqlite3.Connection, origin: str, caller: str, start: str, end: str
) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM model_calls WHERE origin=? AND caller=? AND outcome='refused_budget' "
            "AND started_at >= ? AND started_at < ? LIMIT 1",
            (origin, caller, start, end),
        ).fetchone()
        is not None
    )


def finish(
    row_id: int | None,
    outcome: str,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
) -> None:
    """Close a `started` row. Idempotent (only a still-`started` row changes). Never raises:
    a ledger write that fails must not turn a finished call into a failed one."""
    inst = _installed
    if inst is None or row_id is None:
        return
    known = cost_usd is not None
    try:
        conn = _connect(inst.db_path)
        try:
            conn.execute(
                "UPDATE model_calls SET ended_at=?, outcome=?, input_tokens=?, output_tokens=?, "
                "cost_usd=?, cost_known=? WHERE id=? AND outcome='started'",
                (
                    now_iso(),
                    outcome,
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    1 if known else 0,
                    row_id,
                ),
            )
        finally:
            conn.close()
    except sqlite3.Error:
        log.exception("could not close model_calls row %s as %s", row_id, outcome)


def reconcile_abandoned(db_path: Path | None = None) -> int:
    """Mark unfinished rows whose owner process is gone as `abandoned` (cost unknown). Runs at
    the gateway's first use per database and at cockpit startup. Returns the rows changed."""
    path = db_path or (_installed.db_path if _installed else None)
    if path is None:
        return 0
    try:
        conn = _connect(path)
    except (sqlite3.Error, OSError):
        log.exception("could not open the call ledger to reconcile")
        return 0
    changed = 0
    try:
        owners = conn.execute(
            "SELECT owner_pid, owner_started, MIN(started_at) FROM model_calls "
            "WHERE outcome='started' GROUP BY owner_pid, owner_started"
        ).fetchall()
        for pid, started, first in owners:
            if owner_alive(int(pid), str(started or ""), since=str(first or "")):
                continue
            cur = conn.execute(
                "UPDATE model_calls SET outcome='abandoned', ended_at=?, cost_known=0, "
                "cost_usd=NULL, input_tokens=NULL, output_tokens=NULL "
                "WHERE outcome='started' AND owner_pid=? AND owner_started=?",
                (now_iso(), pid, started),
            )
            changed += cur.rowcount
        _reconciled.add(str(path))
    except sqlite3.Error:
        log.exception("could not reconcile abandoned model calls")
    finally:
        conn.close()
    return changed

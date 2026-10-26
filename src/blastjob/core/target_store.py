"""The target-company list and its news events: storage and selection (#85).

`core/target_watch.py` scans and decides what an event is; this module only stores
and selects. See docs/how-it-works/employer-data.md.

- A target is a row in `target_companies`, keyed by `company_key()`. Removing one
  deletes the row and keeps its events; every event query joins the table, so the
  events are hidden while it is gone and shown again when it is added back.
- A scan writes a company's events and its status in ONE transaction
  (`record_scan`), so a crash never leaves events without a status or the reverse.
- A failed scan keeps `last_scan_ok_at` and counts `scan_failures`, which drives the
  1, 3, then 7 day retry backoff. A lease (`scan_claimed_at`) keeps two runs off one
  target, and is ignored after `LEASE_MINUTES`.
- Timestamps are naive local ISO strings, as everywhere else, so they compare as text.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from blastjob.core.company import company_key
from blastjob.core.employer_domains import normalize_domain
from blastjob.discovery.textutil import us_state_code

EVENT_TYPES: tuple[str, ...] = (
    "new_ceo",
    "new_cfo",
    "new_cio_or_cto",
    "it_leader_departure",
    "acquisition",
    "private_equity",
    "merger",
    "funding",
    "erp_or_cloud_program",
    "restructuring",
)
EVENT_LABELS: dict[str, str] = {
    "new_ceo": "New CEO",
    "new_cfo": "New CFO",
    "new_cio_or_cto": "New CIO or CTO",
    "it_leader_departure": "IT leader leaving",
    "acquisition": "Acquisition",
    "private_equity": "Private equity",
    "merger": "Merger",
    "funding": "Funding",
    "erp_or_cloud_program": "ERP or cloud program",
    "restructuring": "Restructuring",
}
TIERS = ("core", "dream")
BASIS_LABELS = {"employer_site": "employer's own site", "news_report": "news report"}

MAX_BULK_LINES = 200
MAX_NAME_CHARS = 120
MAX_COMPANIES_PER_RUN = 8
RESCAN_DAYS = 7
BACKOFF_DAYS = (1, 3, 7)
LEASE_MINUTES = 30
SPAN_STORE_CHARS = 600

_TRACKING_PARAM = re.compile(
    r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|msclkid|igshid|ref|ref_src)$", re.I
)


def _stamp(when: dt.datetime) -> str:
    return when.isoformat(timespec="seconds")


@dataclass(frozen=True)
class TargetInput:
    name: str
    domain: str = ""
    state: str = ""


@dataclass(frozen=True)
class BulkParse:
    items: tuple[TargetInput, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class AddResult:
    added: tuple[str, ...] = ()
    existing: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()


@dataclass(frozen=True)
class NewEvent:
    event_type: str
    source_url: str
    page_title: str
    source_span: str
    published_on: str
    source_basis: str
    identity_basis: str


# ----------------------------------------------------------------------------- input


def _parse_line(line: str) -> TargetInput | str:
    """One `Name | domain | ST` line, or the reason it was refused."""
    parts = [p.strip() for p in line.split("|")]
    name = " ".join(parts[0].split())
    if not name or len(name) > MAX_NAME_CHARS:
        return f"{name[:40] or line[:40]!r}: the name must be 1 to {MAX_NAME_CHARS} characters"
    if len(parts) > 3:
        return f"{name!r}: expected `name | domain | state`, got more than three fields"
    domain = state = ""
    try:
        if len(parts) > 1 and parts[1]:
            domain = normalize_domain(parts[1])
    except ValueError as exc:
        return f"{name!r}: {exc}"
    if len(parts) > 2 and parts[2]:
        state = us_state_code(parts[2])
        if not state:
            return f"{name!r}: {parts[2]!r} is not a US state code"
    return TargetInput(name=name, domain=domain, state=state)


def parse_bulk(text: str) -> BulkParse:
    """A textarea, one company per line. Bad lines are reported, never dropped quietly."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    errors: list[str] = []
    if len(lines) > MAX_BULK_LINES:
        errors.append(f"Only the first {MAX_BULK_LINES} lines were read; add the rest separately.")
        lines = lines[:MAX_BULK_LINES]
    items: list[TargetInput] = []
    for line in lines:
        parsed = _parse_line(line)
        if isinstance(parsed, str):
            errors.append(parsed)
        else:
            items.append(parsed)
    return BulkParse(items=tuple(items), errors=tuple(errors))


# ------------------------------------------------------------------------- the list


def add_targets(
    conn: sqlite3.Connection,
    items: Iterable[TargetInput],
    tier: str = "core",
    *,
    now: dt.datetime | None = None,
) -> AddResult:
    """Insert new targets, deduped by `company_key`. An existing key is left as it is."""
    when = _stamp(now or dt.datetime.now())
    tier = tier if tier in TIERS else "core"
    added: list[str] = []
    existing: list[str] = []
    rejected: list[str] = []
    for item in items:
        key = company_key(item.name)
        if not key:
            rejected.append(item.name)
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO target_companies "
            "(company_key, company, tier, domain, state, added_at) VALUES (?,?,?,?,?,?)",
            (key, item.name, tier, item.domain, item.state, when),
        )
        (added if cur.rowcount == 1 else existing).append(item.name)
    conn.commit()
    return AddResult(tuple(added), tuple(existing), tuple(rejected))


def remove_target(conn: sqlite3.Connection, key: str) -> bool:
    cur = conn.execute("DELETE FROM target_companies WHERE company_key=?", (key,))
    conn.commit()
    return cur.rowcount == 1


def set_tier(conn: sqlite3.Connection, key: str, tier: str) -> bool:
    if tier not in TIERS:
        return False
    cur = conn.execute("UPDATE target_companies SET tier=? WHERE company_key=?", (tier, key))
    conn.commit()
    return cur.rowcount == 1


def get_target(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM target_companies WHERE company_key=?", (key,)).fetchone()


def dream_keys(conn: sqlite3.Connection) -> frozenset[str]:
    """The `company_key` of every company currently on the dream tier (#90). Read once per
    pass (matching, the funnel, a list render); membership is always current, never stored on
    a job."""
    rows = conn.execute("SELECT company_key FROM target_companies WHERE tier='dream'").fetchall()
    return frozenset(str(r["company_key"]) for r in rows)


def list_targets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM target_companies ORDER BY company COLLATE NOCASE").fetchall()


# ------------------------------------------------------------------ scan scheduling


def _backoff_days(failures: int) -> int:
    return BACKOFF_DAYS[min(max(failures, 1), len(BACKOFF_DAYS)) - 1]


def _is_due(row: sqlite3.Row, now: dt.datetime) -> bool:
    claimed = row["scan_claimed_at"] or ""
    if claimed and claimed > _stamp(now - dt.timedelta(minutes=LEASE_MINUTES)):
        return False
    ok_at = row["last_scan_ok_at"] or ""
    if ok_at and ok_at > _stamp(now - dt.timedelta(days=RESCAN_DAYS)):
        return False
    failures = int(row["scan_failures"] or 0)
    tried = row["last_scan_at"] or ""
    if failures and tried:
        return tried <= _stamp(now - dt.timedelta(days=_backoff_days(failures)))
    return True


def due_targets(
    conn: sqlite3.Connection,
    *,
    now: dt.datetime,
    limit: int = MAX_COMPANIES_PER_RUN,
    tier: str = "core",
) -> list[sqlite3.Row]:
    """Core targets whose last good scan is a week old (or absent), minus failures still in
    their backoff and targets another run holds. Never-scanned first, then the least recently
    attempted, so a list longer than `limit` rotates through all of it."""
    rows = conn.execute("SELECT * FROM target_companies WHERE tier=?", (tier,)).fetchall()
    due = [r for r in rows if _is_due(r, now)]
    due.sort(key=lambda r: (r["last_scan_at"] or "", r["added_at"], r["company_key"]))
    return due[:limit]


def claim_scan(conn: sqlite3.Connection, key: str, *, now: dt.datetime) -> bool:
    """Take the target's lease atomically. False when another run holds a live one."""
    cutoff = _stamp(now - dt.timedelta(minutes=LEASE_MINUTES))
    cur = conn.execute(
        "UPDATE target_companies SET scan_claimed_at=? WHERE company_key=? "
        "AND (COALESCE(scan_claimed_at,'')='' OR scan_claimed_at<=?)",
        (_stamp(now), key, cutoff),
    )
    conn.commit()
    return cur.rowcount == 1


def release_scan(conn: sqlite3.Connection, key: str) -> None:
    """Drop the lease without recording a scan: the target was not run (the daily deadline), so
    its status, failure count and last-scan time stay as they were and it stays due."""
    conn.execute("UPDATE target_companies SET scan_claimed_at='' WHERE company_key=?", (key,))
    conn.commit()


# --------------------------------------------------------------------- event rows


def canonical_url(url: str) -> str:
    """Lowercase host, no fragment, tracking parameters removed."""
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAM.match(k)])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))


def fingerprint(key: str, event_type: str, span: str) -> str:
    normalized = " ".join(span.lower().split())
    return hashlib.sha256(f"{key}\x1f{event_type}\x1f{normalized}".encode()).hexdigest()[:32]


def record_scan(
    conn: sqlite3.Connection,
    key: str,
    *,
    ok: bool,
    status: str,
    events: Sequence[NewEvent],
    now: dt.datetime,
) -> int:
    """One transaction: the company's new events, its scan status and the released lease.
    A failure keeps `last_scan_ok_at`. Returns how many events were new."""
    when = _stamp(now)
    inserted = 0
    try:
        for e in events:
            span = e.source_span[:SPAN_STORE_CHARS]  # fingerprint what is stored (same-role.md)
            cur = conn.execute(
                "INSERT OR IGNORE INTO company_events (company_key, event_type, source_url, "
                "page_title, source_span, published_on, source_basis, identity_basis, "
                "fingerprint, found_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    e.event_type,
                    canonical_url(e.source_url),
                    e.page_title,
                    span,
                    e.published_on,
                    e.source_basis,
                    e.identity_basis,
                    fingerprint(key, e.event_type, span),
                    when,
                ),
            )
            inserted += cur.rowcount
        if ok:
            conn.execute(
                "UPDATE target_companies SET last_scan_at=?, last_scan_status=?, "
                "last_scan_ok_at=?, scan_failures=0, scan_claimed_at='' WHERE company_key=?",
                (when, status, when, key),
            )
        else:
            conn.execute(
                "UPDATE target_companies SET last_scan_at=?, last_scan_status=?, "
                "scan_failures=COALESCE(scan_failures,0)+1, scan_claimed_at='' "
                "WHERE company_key=?",
                (when, status[:200], key),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return inserted


_EVENT_SELECT = (
    "SELECT e.*, t.company AS company, t.tier AS tier FROM company_events e "
    "JOIN target_companies t ON t.company_key = e.company_key "
)


def events_for(
    conn: sqlite3.Connection, key: str, *, include_dismissed: bool = False
) -> list[sqlite3.Row]:
    dismissed = "" if include_dismissed else "AND COALESCE(e.dismissed_at,'')='' "
    return conn.execute(
        f"{_EVENT_SELECT}WHERE e.company_key=? {dismissed}ORDER BY e.published_on DESC, e.id DESC",
        (key,),
    ).fetchall()


def all_events(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    """Every undismissed event of a current target, newest first, grouped by company_key."""
    out: dict[str, list[sqlite3.Row]] = {}
    rows = conn.execute(
        f"{_EVENT_SELECT}WHERE COALESCE(e.dismissed_at,'')='' "
        "ORDER BY e.published_on DESC, e.id DESC"
    ).fetchall()
    for row in rows:
        out.setdefault(row["company_key"], []).append(row)
    return out


def dismiss_event(
    conn: sqlite3.Connection, event_id: int, *, now: dt.datetime | None = None
) -> bool:
    cur = conn.execute(
        "UPDATE company_events SET dismissed_at=? WHERE id=? AND COALESCE(dismissed_at,'')=''",
        (_stamp(now or dt.datetime.now()), event_id),
    )
    conn.commit()
    return cur.rowcount == 1


def unnotified_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Events of current targets that no sent digest has carried and nobody dismissed."""
    return conn.execute(
        f"{_EVENT_SELECT}WHERE COALESCE(e.notified_at,'')='' AND COALESCE(e.dismissed_at,'')='' "
        "ORDER BY e.published_on DESC, e.id DESC"
    ).fetchall()


def mark_notified(
    conn: sqlite3.Connection, event_ids: Iterable[int], *, now: dt.datetime | None = None
) -> None:
    when = _stamp(now or dt.datetime.now())
    conn.executemany(
        "UPDATE company_events SET notified_at=? WHERE id=?", [(when, int(i)) for i in event_ids]
    )
    conn.commit()


# ------------------------------------------------------------------------- re-keying


@dataclass(frozen=True)
class TargetRekey:
    """What one target-table re-key pass did (`core/company.rekey_companies`)."""

    targets_updated: int = 0
    targets_conflicts: int = 0
    targets_skipped_leased: int = 0
    events_updated: int = 0
    events_merged: int = 0
    events_conflicts: int = 0


def _earliest(a: str | None, b: str | None) -> str:
    """The earlier non-empty of two timestamps; '' when both are empty."""
    return min((v for v in (a, b) if v), default="")


def _move_events(conn: sqlite3.Connection, old: str, new: str) -> tuple[int, int]:
    """Give every event of `old` the key `new` and the fingerprint that key implies. An event
    whose new fingerprint already exists is merged into that row (the earliest `notified_at`
    wins; the twin keeps its own `dismissed_at`) and its own row is deleted.
    Returns (moved, merged)."""
    moved = merged = 0
    for ev in conn.execute("SELECT * FROM company_events WHERE company_key=?", (old,)).fetchall():
        fp = fingerprint(new, ev["event_type"], ev["source_span"])
        twin = conn.execute(
            "SELECT id, notified_at, dismissed_at FROM company_events WHERE fingerprint=? "
            "AND id<>?",
            (fp, ev["id"]),
        ).fetchone()
        if twin is None:
            conn.execute(
                "UPDATE company_events SET company_key=?, fingerprint=? WHERE id=?",
                (new, fp, ev["id"]),
            )
            moved += 1
            continue
        conn.execute(
            "UPDATE company_events SET notified_at=? WHERE id=?",
            (_earliest(twin["notified_at"], ev["notified_at"]), twin["id"]),
        )
        conn.execute("DELETE FROM company_events WHERE id=?", (ev["id"],))
        merged += 1
    return moved, merged


def _lease_is_live(row: sqlite3.Row, now: dt.datetime) -> bool:
    claimed = row["scan_claimed_at"] or ""
    return bool(claimed) and claimed > _stamp(now - dt.timedelta(minutes=LEASE_MINUTES))


def rekey_targets(conn: sqlite3.Connection, *, now: dt.datetime | None = None) -> TargetRekey:
    """Re-key `target_companies` and `company_events` after the key rules or aliases changed.

    Never commits: `rekey_companies` runs it inside its one transaction. Each target moves
    under its own SAVEPOINT with its events, so a conflict (the new key is already a target)
    leaves the target and all its events where they were. A target with a live lease is
    skipped. Events whose target was removed are re-keyed from their old key alone.
    docs/how-it-works/same-role.md."""
    when = now or dt.datetime.now()
    out = {"tu": 0, "tc": 0, "tl": 0, "eu": 0, "em": 0, "ec": 0}
    for row in conn.execute("SELECT * FROM target_companies").fetchall():
        old, new = row["company_key"], company_key(row["company"])
        if new == old:
            continue
        if _lease_is_live(row, when):
            out["tl"] += 1
            continue
        conn.execute("SAVEPOINT rekey_target")
        try:
            conn.execute(
                "UPDATE target_companies SET company_key=? WHERE company_key=?", (new, old)
            )
            moved, merged = _move_events(conn, old, new)
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK TO rekey_target")
            out["tc"] += 1
        else:
            out["tu"] += 1
            out["eu"] += moved
            out["em"] += merged
        # RELEASE only here, never in a `finally`: after SQLite rolled the savepoint back on
        # its own, it would raise and mask the real error.
        conn.execute("RELEASE rekey_target")
    live = {r["company_key"] for r in conn.execute("SELECT company_key FROM target_companies")}
    orphans = conn.execute(
        "SELECT DISTINCT company_key FROM company_events "
        "WHERE company_key NOT IN (SELECT company_key FROM target_companies)"
    ).fetchall()
    for orphan in orphans:
        old = orphan["company_key"]
        new = company_key(
            old
        )  # old is a stored key, re-normalized (docs/how-it-works/same-role.md)
        if not new or new == old:
            continue
        if new in live:
            # Hidden by removing the target; moving them under a live target would show and
            # announce them again.
            out["ec"] += conn.execute(
                "SELECT COUNT(*) FROM company_events WHERE company_key=?", (old,)
            ).fetchone()[0]
            continue
        moved, merged = _move_events(conn, old, new)
        out["eu"] += moved
        out["em"] += merged
    return TargetRekey(out["tu"], out["tc"], out["tl"], out["eu"], out["em"], out["ec"])

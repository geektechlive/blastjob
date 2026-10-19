"""SQLite access for the job-discovery pipeline.

Connection policy (Codex #11): WAL + busy_timeout so concurrent discovery writes
and UI/status updates don't hard-fail with SQLITE_BUSY. Callers keep transactions
short; blocking calls run off the FastAPI event loop (via a threadpool) at the
web layer, not here.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from blastjob import config as cfg_mod
from blastjob.db.schema import MIGRATIONS
from blastjob.discovery.textutil import html_to_text
from blastjob.models.config import BlastJobConfig
from blastjob.models.job import BuildState

BUSY_TIMEOUT_MS = 5000
DEFAULT_LEASE_MINUTES = 20
# A revision is a model call plus several scoring calls (each up to the CLI timeout).
REFINE_LEASE_MINUTES = 90
# `build_runs.run_id` prefix marking a refine's claim row (no schema change needed).
REFINE_RUN_PREFIX = "refine:"

# Generated once per process. Stamped on every build_runs row this process
# claims so a later recovery pass can tell "a previous process's build" (dead
# the instant a new process starts, lease or not) from "this process's build,
# just running long" (only actually orphaned once its lease expires). See
# recover_stale_builds().
PROCESS_TOKEN = uuid.uuid4().hex


def db_path(cfg: BlastJobConfig | None = None) -> Path:
    """The database file: in `cfg`'s data dir, else the resolved one
    (`config.resolve_data_dir`), so opening it never needs a config."""
    base = cfg_mod.data_dir(cfg) if cfg is not None else cfg_mod.resolve_data_dir()
    return base / cfg_mod.DB_NAME


def _now() -> str:
    return datetime.now().isoformat()


def connect(cfg: BlastJobConfig | None = None) -> sqlite3.Connection:
    return connect_at(db_path(cfg))


PRIVATE_FILE_MODE = 0o600


def _create_private(path: Path) -> None:
    """The data dir and DB hold a person's job search: create them owner-only
    (0700 / 0600). Only what this call CREATES is tightened; an existing dir
    or file keeps the permissions its owner gave it."""
    cfg_mod.ensure_private_dir(path.parent)
    if not path.exists():
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE_MODE)
        except FileExistsError:
            return  # another process created it first; leave its mode alone
        os.close(fd)


def connect_at(path: Path) -> sqlite3.Connection:
    _create_private(path)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _apply_statement_by_statement(conn: sqlite3.Connection, sql: str) -> None:
    """Run a migration script one statement at a time, tolerating only
    "duplicate column".

    `executescript` stops at the first failure, so a script whose FIRST
    statement duplicates an existing column never applies its remaining ones.
    That is the half-applied case: the version gets recorded, the later column
    never exists, and every query naming it fails from then on. Replaying
    statement by statement lets the genuinely-already-applied parts be skipped
    while the rest still land. Any other OperationalError still raises.
    """
    for statement in sql.split(";"):
        if not statement.strip():
            continue
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any pending migrations. Returns the resulting schema version."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations").fetchone()
    current = row["v"]
    applied = current
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        try:
            conn.executescript(sql)
        except sqlite3.OperationalError as e:
            # Two processes (cockpit + daily job) can race the first launch after
            # a schema bump; the loser sees "duplicate column name". The column
            # exists, so that statement is a no-op.
            if "duplicate column" not in str(e).lower():
                raise
            # But `executescript` ABORTS at the failing statement, so the rest of
            # the script never ran. Recording the version here would mark the
            # migration done with its later statements missing, and every query
            # naming the missing column would fail forever. So replay it
            # statement by statement, skipping only the duplicate-column ones.
            _apply_statement_by_statement(conn, sql)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, _now()),
        )
        applied = version
    # Ad hoc, self-contained (not a numbered entry in schema.MIGRATIONS — see
    # client.py's ownership note): tracks which process claimed a build_runs
    # row, for recover_stale_builds()'s process-owner check. Idempotent, same
    # duplicate-column race guard as the loop above.
    try:
        conn.execute("ALTER TABLE build_runs ADD COLUMN owner_token TEXT DEFAULT ''")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise
    conn.commit()
    return applied


# Databases this process has already migrated. The schema cannot change under a
# running process, and `web/app.py` calls `init_db` on every request (twice for
# most full pages), so without this every page view re-ran the whole migration
# loop plus an ALTER TABLE that raises and is caught each time. Keyed by path,
# so a second database -- a test's temp dir, say -- is still migrated.
_MIGRATED: set[str] = set()


def reset_migration_memo() -> None:
    """Forget what this process has migrated. For tests that rebuild a database
    at the same path; production never needs it."""
    _MIGRATED.clear()


def init_db(cfg: BlastJobConfig | None = None) -> sqlite3.Connection:
    """Open a connection and ensure the schema is current. With no `cfg`, the
    data dir is resolved without one (the bootstrap path).

    Migration runs once per database per process (see `_MIGRATED`). Two
    processes -- the cockpit and the daily job -- still each migrate once, which
    is the race the duplicate-column tolerance in `migrate` exists to handle.
    """
    return init_db_at(db_path(cfg))


def init_db_at(path: Path) -> sqlite3.Connection:
    conn = connect_at(path)
    key = str(path)
    if key not in _MIGRATED:
        migrate(conn)
        _MIGRATED.add(key)
    return conn


def upsert_job(
    conn: sqlite3.Connection,
    *,
    source: str,
    external_id: str,
    commit: bool = True,
    refresh_title: bool = True,
    **fields,
) -> int:
    """Insert a discovered posting or refresh a known one by (source, external_id).

    A refresh touches only source-owned fields (JD, url, last_seen, source_state)
    — never review_state / build_state / application_state, so user decisions and
    in-flight builds survive rediscovery. Three more refresh guards, each from a
    real overwrite:
    - a JD only moves to the same or a higher quality class (`jd_class`), and an
      undecided row's score goes with the old text (`_clear_score_sql`);
    - an empty company never replaces a stored one (a re-sent whole-card alert
      carries none, and the stored name may have been repaired since);
    - `refresh_title=False` (whole-card alert rows, whose "title" is the entire
      card text) keeps the stored title; first sight still stores what it has.

    `commit=False` leaves the write in the caller's transaction, so a batch of
    upserts and the bookkeeping that depends on them can land or roll back as one.
    """
    now = _now()
    existing = conn.execute(
        "SELECT id, jd_pasted_at, raw_jd, review_state, jd_source FROM jobs "
        "WHERE source=? AND external_id=?",
        (source, external_id),
    ).fetchone()

    if existing is None:
        cols = {
            "source": source,
            "external_id": external_id,
            "first_seen": now,
            "last_seen": now,
            "source_state": "active",
            **fields,
        }
        placeholders = ", ".join("?" for _ in cols)
        col_names = ", ".join(cols)
        cur = conn.execute(
            f"INSERT INTO jobs ({col_names}) VALUES ({placeholders})", tuple(cols.values())
        )
        if commit:
            conn.commit()
        return int(cur.lastrowid or 0)

    # Refresh only source-owned columns. A pasted description is the user's, not
    # the source's: the snippet the source keeps sending must not replace it.
    source_owned = {k: v for k, v in fields.items() if k in _SOURCE_OWNED}
    if not source_owned.get("company"):
        source_owned.pop("company", None)
    if not refresh_title:
        source_owned.pop("title", None)
    clear_score = _guard_jd(source_owned, existing)
    source_owned["last_seen"] = now
    source_owned["source_state"] = "active"
    assignments = ", ".join(f"{k}=?" for k in source_owned)
    conn.execute(
        f"UPDATE jobs SET {assignments}{', ' + _CLEAR_SCORE_SQL if clear_score else ''} WHERE id=?",
        (*source_owned.values(), existing["id"]),
    )
    if commit:
        conn.commit()
    return int(existing["id"])


# The score belongs to the JD it was computed from (see `set_pasted_jd`).
_CLEAR_SCORE_SQL = "alignment_score=NULL, rationale='', match_strengths='', match_gaps=''"

# Provenance of an employer-sourced JD; a paste or an undo wipes it.
_CLEAR_EMPLOYER_SQL = "jd_source='', jd_source_ref='', jd_fetched_at='', jd_prior=''"

EMPLOYER_JD_PREFIX = "employer:"
JD_STUB, JD_SNIPPET, JD_FULL, JD_EMPLOYER, JD_PASTED = range(5)


def jd_class(
    raw_jd: str | None, jd_pasted_at: str | None = None, jd_source: str | None = None
) -> int:
    """Quality class of a description: stub (empty) < snippet < full < employer
    (adopted from the employer's own posting, `jd_source` 'employer:...') < pasted."""
    if jd_pasted_at:
        return JD_PASTED
    if (jd_source or "").startswith(EMPLOYER_JD_PREFIX):
        return JD_EMPLOYER
    n = len((raw_jd or "").strip())
    if n == 0:
        return JD_STUB
    return JD_SNIPPET if n < MIN_FULL_JD_CHARS else JD_FULL


def _guard_jd(source_owned: dict, existing: sqlite3.Row) -> bool:
    """Decide whether this refresh may replace the stored JD, editing
    `source_owned` in place. Returns True when the JD text changed on an
    undecided row, i.e. its score must be cleared.

    A lower class never replaces a higher one (a snippet must not clobber a full
    JD, and nothing the source sends replaces a pasted one); the same class does,
    so a revised full posting still lands. Compared on stripped text, so a
    whitespace-only difference is no change and costs no re-score."""
    if "raw_jd" not in source_owned:
        return False
    incoming = source_owned["raw_jd"] or ""
    stored_class = jd_class(existing["raw_jd"], existing["jd_pasted_at"], existing["jd_source"])
    if jd_class(incoming) < stored_class or stored_class == JD_PASTED:
        source_owned.pop("raw_jd")
        if stored_class == JD_EMPLOYER and incoming.strip():
            # Keep the latest aggregator text so an undo restores it, not a stale one.
            source_owned["jd_prior"] = incoming
        return False
    if incoming.strip() == (existing["raw_jd"] or "").strip():
        source_owned.pop("raw_jd")  # unchanged: leave the stored text byte-for-byte
        return False
    return existing["review_state"] in UNDECIDED_STATES


# Columns a rediscovery is allowed to refresh — facts the source owns. Comp
# columns are here so POSTED pay refreshes, but normalize omits them when the
# source gave no pay, so inferred comp is never clobbered. User/matcher-owned
# columns (review_state, build_state, application_state, alignment_score) are
# deliberately excluded.
_SOURCE_OWNED = {
    "company",
    "title",
    "url",
    "raw_jd",
    "geo_location_raw",
    "geo_city",
    "geo_region",
    "geo_remote",
    "comp_min",
    "comp_max",
    "comp_total_est",
    "comp_currency",
    "comp_confidence",
    "comp_provenance",
    "cluster_id",
}


# Below this length a stored description is a snippet, not the posting: Adzuna's
# API cuts at 500 characters and an alert email can carry one line.
MIN_FULL_JD_CHARS = 600
MAX_PASTED_JD_CHARS = 60_000
# The machine-owned review states (matching.pipeline.MACHINE_STATES); a row in
# any other state carries a user decision.
UNDECIDED_STATES = ("new", "surfaced", "queued")


def is_jd_snippet(raw_jd: str | None, jd_pasted_at: str | None) -> bool:
    """True when the description we hold is too short to be the real posting.
    Anything the user pasted is trusted as the whole description, however short."""
    if jd_pasted_at:
        return False
    return len((raw_jd or "").strip()) < MIN_FULL_JD_CHARS


def _pasted_body(text: str | None) -> str:
    return (text or "").strip()[:MAX_PASTED_JD_CHARS]


def pasted_jd_changes(conn: sqlite3.Connection, job_id: int, text: str) -> bool:
    """Would pasting `text` change this job's stored description? (Compared as
    `set_pasted_jd` would store it, so trailing whitespace is no change.)"""
    body = _pasted_body(text)
    prev = conn.execute("SELECT raw_jd FROM jobs WHERE id=?", (job_id,)).fetchone()
    return bool(body) and prev is not None and _pasted_body(prev[0]) != body


def set_pasted_jd(conn: sqlite3.Connection, job_id: int, text: str) -> bool:
    """Store a hand-pasted full description and stamp it so rediscovery keeps
    it. A blank paste is rejected rather than wiping the snippet.

    New text on an UNDECIDED row (new/surfaced/queued) clears the score fields:
    a score belongs to the JD it was computed from, and score reuse keys on
    employer + title + JD, so a snippet's score left beside the full text would
    be replayed as the full text's (issue #4). The caller re-scores
    (`matching.rescore`), and a NULL score is also what the daily
    `only_unscored` pass picks up. A KEPT row keeps its score: no pass ever
    retries a kept row, so clearing it would lose the number for good if the
    re-score failed; `rescore` replaces it and ignores its stale reuse entry."""
    body = _pasted_body(text)
    if not body:
        return False
    prev = conn.execute("SELECT raw_jd, review_state FROM jobs WHERE id=?", (job_id,)).fetchone()
    if prev is None:
        return False
    if _pasted_body(prev[0]) == body:
        conn.execute(
            f"UPDATE jobs SET jd_pasted_at=?, {_CLEAR_EMPLOYER_SQL} WHERE id=?", (_now(), job_id)
        )
    elif prev[1] in UNDECIDED_STATES:
        conn.execute(
            f"UPDATE jobs SET raw_jd=?, jd_pasted_at=?, {_CLEAR_EMPLOYER_SQL}, "
            f"{_CLEAR_SCORE_SQL} WHERE id=?",
            (body, _now(), job_id),
        )
    else:
        conn.execute(
            f"UPDATE jobs SET raw_jd=?, jd_pasted_at=?, {_CLEAR_EMPLOYER_SQL} WHERE id=?",
            (body, _now(), job_id),
        )
    conn.commit()
    return True


def normalize_employer_jd(text: str) -> str | None:
    """Plain-text form of an employer posting as `set_employer_jd` stores it, or
    None when it is too short to be the real description (an error or blocked
    page, a stub). Capped like a paste."""
    body = html_to_text(text or "").strip()
    if len(body) < MIN_FULL_JD_CHARS:
        return None
    return body[:MAX_PASTED_JD_CHARS]


@dataclass(frozen=True)
class JdSnapshot:
    """Everything `set_employer_jd` proved its match against, read BEFORE the
    network await; the write only lands if none of it moved."""

    job_id: int
    raw_jd: str | None
    company: str | None
    title: str | None
    review_state: str | None
    source_state: str | None
    employer_link_source: str | None
    employer_link_external_id: str | None


def jd_snapshot(conn: sqlite3.Connection, job_id: int) -> JdSnapshot | None:
    row = conn.execute(
        "SELECT raw_jd, company, title, review_state, source_state, employer_link_source, "
        "employer_link_external_id FROM jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return JdSnapshot(job_id, *tuple(row))


def _score_follows_jd_sql(review_state: str | None) -> str:
    """How a changed JD treats the score: an undecided row's goes with the old
    text (cleared); a kept row keeps it but is flagged for a durable re-score."""
    if review_state in UNDECIDED_STATES:
        return _CLEAR_SCORE_SQL
    return "score_stale=1" if review_state == "kept" else "score_stale=score_stale"


def set_employer_jd(
    conn: sqlite3.Connection,
    snap: JdSnapshot,
    *,
    text: str,
    jd_source: str,
    jd_source_ref: str,
    now: str,
) -> bool:
    """Adopt the employer's own posting text as this job's description.

    NEVER commits: the caller owns the transaction. One UPDATE compare-and-sets
    over every field of `snap` (the state the match was proven against), no
    paste, and either no employer text yet (first adoption: the replaced text
    moves to `jd_prior` for undo) or the same `jd_source`/`jd_source_ref` (a
    revision: the original `jd_prior` stays). False = refused text or lost race;
    nothing written."""
    body = normalize_employer_jd(text)
    if body is None or not jd_source.startswith(EMPLOYER_JD_PREFIX):
        return False
    cur = conn.execute(
        "UPDATE jobs SET raw_jd=?, jd_source=?, jd_source_ref=?, jd_fetched_at=?, "
        "jd_prior=CASE WHEN COALESCE(jd_source,'')='' THEN COALESCE(raw_jd,'') ELSE jd_prior END, "
        f"{_score_follows_jd_sql(snap.review_state)} "
        "WHERE id=? AND raw_jd IS ? AND company IS ? AND title IS ? AND review_state IS ? "
        "AND source_state IS ? AND employer_link_source IS ? AND employer_link_external_id IS ? "
        "AND COALESCE(jd_pasted_at,'')='' "
        "AND (COALESCE(jd_source,'')='' OR (jd_source=? AND jd_source_ref=?))",
        (
            body,
            jd_source,
            jd_source_ref,
            now,
            snap.job_id,
            snap.raw_jd,
            snap.company,
            snap.title,
            snap.review_state,
            snap.source_state,
            snap.employer_link_source,
            snap.employer_link_external_id,
            jd_source,
            jd_source_ref,
        ),
    )
    return cur.rowcount == 1


def revert_employer_jd(
    conn: sqlite3.Connection, job_id: int, *, ref: tuple[str, str] | None = None
) -> bool:
    """Undo an employer-text adoption: restore `jd_prior`, wipe the provenance.

    NEVER commits. `ref` = (jd_source, jd_source_ref) limits it to that exact
    adoption (the link being dropped), so another link's text is left alone.
    A pasted row is never touched: the paste already cleared the provenance."""
    row = conn.execute("SELECT review_state FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return False
    where = "id=? AND jd_source LIKE ? AND COALESCE(jd_pasted_at,'')=''"
    params: list[object] = [job_id, EMPLOYER_JD_PREFIX + "%"]
    if ref is not None:
        where += " AND jd_source=? AND jd_source_ref=?"
        params += [ref[0], ref[1]]
    cur = conn.execute(
        f"UPDATE jobs SET raw_jd=COALESCE(jd_prior,''), {_CLEAR_EMPLOYER_SQL}, "
        f"{_score_follows_jd_sql(row[0])} WHERE {where}",
        params,
    )
    return cur.rowcount == 1


JD_LOOKUP_STATUSES = ("found", "no_match", "transient")
NO_MATCH_RETRY_DAYS = 14
TRANSIENT_RETRY_BASE_DAYS = 1
RETRY_CAP_DAYS = 14


def record_jd_lookup(conn: sqlite3.Connection, job_id: int, status: str, now: str) -> None:
    """Record the outcome of an employer-JD lookup (NEVER commits). A transient
    failure counts toward the backoff; any other outcome resets the count."""
    if status not in JD_LOOKUP_STATUSES:
        raise ValueError(f"unknown jd lookup status: {status!r}")
    failures = "COALESCE(jd_lookup_failures,0)+1" if status == "transient" else "0"
    conn.execute(
        f"UPDATE jobs SET jd_lookup_at=?, jd_lookup_status=?, jd_lookup_failures={failures} "
        "WHERE id=?",
        (now, status, job_id),
    )


def jd_lookup_due(row: sqlite3.Row, now: str) -> bool:
    """May this job be looked up again? found: never; no_match: after 14 days;
    transient: 1, 2, 4, 8 days (doubling per failure), capped at 14."""
    status = row["jd_lookup_status"] or ""
    if status == "found":
        return False
    last = row["jd_lookup_at"] or ""
    if not status or not last:
        return True
    if status == "no_match":
        wait = NO_MATCH_RETRY_DAYS
    else:
        failures = max(int(row["jd_lookup_failures"] or 0), 1)
        wait = min(TRANSIENT_RETRY_BASE_DAYS * 2 ** (failures - 1), RETRY_CAP_DAYS)
    try:
        return datetime.fromisoformat(now) >= datetime.fromisoformat(last) + timedelta(days=wait)
    except ValueError:
        return True


def claim_for_build(conn: sqlite3.Connection, job_id: int, run_id: str) -> bool:
    """CAS: move build_state none|failed -> tailoring and record a leased run.

    Returns True only if THIS caller won the claim (rowcount == 1), so a job is
    never double-tailored.
    """
    now = datetime.now()
    lease = (now + timedelta(minutes=DEFAULT_LEASE_MINUTES)).isoformat()
    # One statement, so a refine's claim and this one cannot both succeed.
    cur = conn.execute(
        "UPDATE jobs SET build_state='tailoring' WHERE id=? AND build_state IN ('none','failed') "
        "AND NOT EXISTS (SELECT 1 FROM build_runs WHERE job_id=jobs.id AND status='running' "
        "AND run_id LIKE ? AND lease_expires_at >= ?)",
        (job_id, REFINE_RUN_PREFIX + "%", now.isoformat()),
    )
    if cur.rowcount != 1:
        conn.rollback()
        return False
    try:
        conn.execute(
            "INSERT INTO build_runs "
            "(job_id, run_id, status, lease_expires_at, created_at, updated_at, owner_token) "
            "VALUES (?, ?, 'running', ?, ?, ?, ?)",
            (job_id, run_id, lease, now.isoformat(), now.isoformat(), PROCESS_TOKEN),
        )
        conn.commit()
    except Exception:
        # Never leave the job stuck in 'tailoring' if the run row can't be written.
        conn.rollback()
        return False
    return True


def claim_for_refine(conn: sqlite3.Connection, job_id: int, run_id: str) -> bool:
    """Claim the job's packet folder for a resume revision.

    A refine and a tailor both rewrite the same files, so they exclude each other
    here, durably: this refuses while a build holds the job (`tailoring`) or another
    refine runs, and `claim_for_build` refuses while a refine's row is live. The
    claim is a `build_runs` row whose run id carries `REFINE_RUN_PREFIX`; it is
    deleted by `release_refine`, never left behind as a 'failed' build that
    would pose as the job's latest attempt. One INSERT...SELECT, so it is atomic."""
    now = datetime.now()
    lease = (now + timedelta(minutes=REFINE_LEASE_MINUTES)).isoformat()
    cur = conn.execute(
        "INSERT INTO build_runs "
        "(job_id, run_id, status, lease_expires_at, created_at, updated_at, owner_token) "
        "SELECT j.id, ?, 'running', ?, ?, ?, ? FROM jobs j WHERE j.id=? "
        "AND COALESCE(j.build_state, 'none') != 'tailoring' "
        "AND NOT EXISTS (SELECT 1 FROM build_runs b WHERE b.job_id=j.id AND b.status='running' "
        "AND b.run_id LIKE ? AND b.lease_expires_at >= ?)",
        (
            REFINE_RUN_PREFIX + run_id,
            lease,
            now.isoformat(),
            now.isoformat(),
            PROCESS_TOKEN,
            job_id,
            REFINE_RUN_PREFIX + "%",
            now.isoformat(),
        ),
    )
    conn.commit()
    return cur.rowcount == 1


def release_refine(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute("DELETE FROM build_runs WHERE run_id=?", (REFINE_RUN_PREFIX + run_id,))
    conn.commit()


def finish_build(conn: sqlite3.Connection, run_id: str, *, artifact_dir: str, ok: bool) -> None:
    now = _now()
    row = conn.execute("SELECT job_id FROM build_runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return
    conn.execute(
        "UPDATE build_runs SET status=?, artifact_dir=?, updated_at=? WHERE run_id=?",
        ("done" if ok else "failed", artifact_dir, now, run_id),
    )
    conn.execute(
        "UPDATE jobs SET build_state=? WHERE id=?",
        (BuildState.TAILORED.value if ok else BuildState.FAILED.value, row["job_id"]),
    )
    conn.commit()


def backup_db(
    conn: sqlite3.Connection,
    dest_dir: Path,
    *,
    keep: int = 14,
    now: datetime | None = None,
) -> Path:
    """Copy the live database to `dest_dir` using SQLite's online backup API.

    The DB now holds applications, contacts, comp signals, and profile
    data alongside discovery state — a daily copy is cheap insurance against
    corruption or a bad migration. `conn.backup()` is safe to run against a
    WAL-mode connection while the cockpit is open (readers/writers keep
    going; the backup just takes longer than usual for concurrent writers).

    This lives beside the `work_history-<stamp>-*.md` snapshots taken before a
    work-history overwrite (same `backups/` folder) but never touches them: pruning only matches the
    `blastjob-*.db` glob this function writes, so those snapshots are left alone.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = now or datetime.now()
    target = dest_dir / f"blastjob-{ts:%Y%m%d-%H%M%S}.db"
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()

    backups = sorted(p for p in dest_dir.glob("blastjob-*.db") if p.is_file())
    for stale in backups[:-keep] if keep > 0 else backups:
        stale.unlink(missing_ok=True)

    return target


def recover_stale_builds(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Reset builds abandoned by this process or a prior one, back to 'failed'.

    A 'running' build_runs row is recovered when EITHER:
    - its owner_token doesn't match this process's PROCESS_TOKEN — a restart
      means no in-process worker survives to finish it, so it's dead
      immediately regardless of its lease (Defect 2: a KeepAlive restart
      mid-build used to strand the job until the lease *also* expired); or
    - it belongs to THIS process but its lease has expired — a worker that
      crashed without reaching finish_build, within one process.

    Run at startup so a crash/restart mid-build never leaves a job stuck in
    'tailoring'. Returns the number recovered.
    """
    ts = (now or datetime.now()).isoformat()
    stale = conn.execute(
        "SELECT run_id, job_id FROM build_runs "
        "WHERE status='running' AND (owner_token != ? OR lease_expires_at < ?)",
        (PROCESS_TOKEN, ts),
    ).fetchall()
    for r in stale:
        if r["run_id"].startswith(REFINE_RUN_PREFIX):
            # A dead refine's claim is simply dropped (see `claim_for_refine`).
            conn.execute("DELETE FROM build_runs WHERE run_id=?", (r["run_id"],))
            continue
        conn.execute(
            "UPDATE build_runs SET status='failed', updated_at=? WHERE run_id=?",
            (ts, r["run_id"]),
        )
        conn.execute(
            "UPDATE jobs SET build_state='failed' WHERE id=? AND build_state='tailoring'",
            (r["job_id"],),
        )
    conn.commit()
    return len(stale)


def reset_build_state(
    conn: sqlite3.Connection, job_id: int, *, now: datetime | None = None
) -> bool:
    """CAS: move build_state 'tailored'|'failed' -> 'none' so a job can be re-tailored.

    A 'tailoring' row is also eligible, but ONLY if its lease has expired —
    otherwise a build genuinely in flight would be yanked out from under
    itself. Refuses: a job whose build is still live, a job already at
    'none', and a job that doesn't exist. This is the sole writer of
    jobs.build_state outside claim_for_build/finish_build — callers reset the
    column through here rather than writing it directly.
    """
    ts = (now or datetime.now()).isoformat()
    row = conn.execute("SELECT build_state FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        conn.rollback()
        return False
    state = row["build_state"]

    if state in (BuildState.TAILORED.value, BuildState.FAILED.value):
        return _cas_build_state_to_none(conn, job_id, from_state=state)

    if state != BuildState.TAILORING.value:
        conn.rollback()
        return False

    run = conn.execute(
        "SELECT run_id FROM build_runs WHERE job_id=? AND status='running' "
        "ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if run is not None:
        live = conn.execute(
            "SELECT 1 FROM build_runs WHERE run_id=? AND lease_expires_at >= ?",
            (run["run_id"], ts),
        ).fetchone()
        if live is not None:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE build_runs SET status='failed', updated_at=? WHERE run_id=?",
            (ts, run["run_id"]),
        )
    return _cas_build_state_to_none(conn, job_id, from_state=BuildState.TAILORING.value)


def _cas_build_state_to_none(conn: sqlite3.Connection, job_id: int, *, from_state: str) -> bool:
    cur = conn.execute(
        "UPDATE jobs SET build_state='none' WHERE id=? AND build_state=?",
        (job_id, from_state),
    )
    if cur.rowcount != 1:
        conn.rollback()
        return False
    conn.commit()
    return True

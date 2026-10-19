"""Posting-closure lifecycle: what happens to a job when its posting closes.

Only proof moves state. A closed verdict comes from a source that can prove
absence (its own authoritative board, or the matched posting on the employer's
board); Adzuna ageing out of its query window is only `likely_closed` and never
moves anything. Rules (2026-09-13):

- kept, not yet applied (incl. tailored) + closed -> `posting_closed`, prior
  state saved so a reopen can restore it
- kept + applied + closed -> badge now; `filled` once closed for 21 days
- screen / interview / offer -> badge only, never moved
- not kept -> `posting_closed_at` stamped, nothing else (active surfaced and
  queued jobs are searched and probed like kept ones, so the user sees a dead
  posting before keeping it; nothing here ever archives them)
- a manual verdict always beats automation, and a manual state change beats a
  restore (moving a job out of posting_closed/filled pins its verdict)
- `[discovery] closure_auto = false` (the default) is shadow mode: verdicts
  and links are written, application_state is not

Every automated state write is a CAS UPDATE guarded on posting_check_manual=0.

Locking: the cockpit writes to the same WAL database with a 5 s busy timeout.
The pass therefore NEVER awaits network I/O while a write is pending -- the
synchronous sweep commits before the first probe, and every network-derived
write is committed before the next await.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from blastjob.core import closure_links, page_links
from blastjob.core import events as app_events
from blastjob.core.closure_links import Link, LinkDeps
from blastjob.core.company import company_key
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig, DiscoveryConfig
from blastjob.web.apply_link import MANUAL_LINK
from blastjob.web.compare import is_agency

log = logging.getLogger(__name__)

POSTING_CLOSED = "posting_closed"
FILLED = "filled"
# The two states only closure automation (or the manual "Posting closed"
# button) may set; the generic status route refuses them.
AUTOMATION_STATES: tuple[str, ...] = (POSTING_CLOSED, FILLED)
# Application states a closed posting may move straight to posting_closed.
AUTO_CLOSABLE: tuple[str, ...] = ("", "drafted")
NO_MOVE = "—"
# MANUAL_LINK (web/apply_link) is the user's pasted employer posting URL and is
# DISPLAY-ONLY here: nothing validated it, so a typo or a dead page would
# otherwise "prove" a live job closed. The pass never probes it, never links
# over it, and never changes that row's verdict (an authoritative own source
# still decides its own posting).

FILLED_AFTER_DAYS = 21
# Bounds on network work per run. Anything skipped is simply reconsidered on a
# later run (oldest check first), which is the conservative direction.
MAX_LINK_SEARCHES_PER_RUN = 15
MAX_LINK_PROBES_PER_RUN = 40
# Network order: oldest check first (never-checked first), then by stage, then
# best fit. Stage and fit only break ties, so no stage starves another.
_STAGE_RANK: dict[str, int] = {"kept": 0, "queued": 1, "surfaced": 2}

_NOT_MANUAL = " AND posting_check_manual=0"

_ROW_COLS = (
    "id, source, external_id, company, title, geo_region, review_state, source_state, "
    "cluster_id, first_seen, posting_check, posting_checked_at, employer_link_source, "
    "employer_link_external_id, employer_link_url, alignment_score"
)


@dataclass
class ClosureResult:
    checked: int = 0
    linked: int = 0
    moved: int = 0
    filled: int = 0
    # Kept jobs whose posting became closed THIS run (for the digest line).
    closed_kept: list[dict] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    # Rows whose link SEARCH closure ran this run (so the employer-JD step does
    # not search them again) and rows whose adopted description was reverted
    # because the link behind it was dropped (they need a re-score).
    searched: set[int] = field(default_factory=set)
    reverted_jd: list[int] = field(default_factory=list)
    # A shadow run (dry run, --closure-report) moves nothing, so a description it
    # WOULD have reverted is listed here instead and left in place.
    would_revert_jd: list[int] = field(default_factory=list)


def _stamp(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


def own_source_verdict(source_state: str | None) -> str:
    """An authoritative source already did the proving: its pipeline only
    stale-marks after a full listing or a confirming 404."""
    return "verified_open" if (source_state or "active") == "active" else "closed"


# --- transitions -------------------------------------------------------------

# Every state move below writes an application event with this source.
_EVENT_SOURCE = "closure"


def _record_outcome(conn: sqlite3.Connection, job_id: int, outcome: str) -> None:
    """Attach a closure outcome to the documents that were sent, if any.

    Best-effort by design: the closure lifecycle is about the posting, and a
    missing or unrecordable send must never stop a job being closed.
    """
    from blastjob.core import packet_sends as ps

    try:
        ps.record_outcome_for_job(conn, job_id, outcome)
    except Exception:  # noqa: BLE001 — bookkeeping never blocks a state change
        return


def close_transition(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    now: datetime,
    auto: bool,
    respect_manual: bool = True,
) -> bool:
    """Stamp the closure (always) and, when `auto`, move an unapplied kept job
    to posting_closed. Automated calls never touch a manually pinned row.
    Returns True if application_state moved."""
    guard = _NOT_MANUAL if respect_manual else ""
    conn.execute(
        "UPDATE jobs SET posting_closed_at=? WHERE id=? "
        f"AND COALESCE(posting_closed_at,'')=''{guard}",
        (_stamp(now), job_id),
    )
    if not auto:
        return False
    placeholders = ",".join("?" for _ in AUTO_CLOSABLE)
    cur = conn.execute(
        "UPDATE jobs SET closed_prior_state=COALESCE(application_state,''), application_state=?, "
        "stage_changed_at=? WHERE id=? AND review_state='kept' "
        f"AND COALESCE(application_state,'') IN ({placeholders}){guard}",
        (POSTING_CLOSED, _stamp(now), job_id, *AUTO_CLOSABLE),
    )
    moved = cur.rowcount == 1
    if moved:
        # The event joins the move's transaction, ahead of the outcome
        # bookkeeping (which commits): both land together.
        app_events.insert_event(conn, job_id, POSTING_CLOSED, _stamp(now), source=_EVENT_SOURCE)
        _record_outcome(conn, job_id, POSTING_CLOSED)
    return moved


def reopen_transition(
    conn: sqlite3.Connection, job_id: int, *, auto: bool, respect_manual: bool = True
) -> bool:
    """The posting is open again. When `auto`, restore the state automation
    replaced -- only if the job still holds the value automation set, so a
    manual change in between wins. Returns True if application_state moved."""
    guard = _NOT_MANUAL if respect_manual else ""
    moved = False
    if auto:
        stamp = _stamp(datetime.now())
        cur = conn.execute(
            "UPDATE jobs SET application_state=closed_prior_state, closed_prior_state='', "
            "posting_closed_at='', stage_changed_at=? WHERE id=? AND application_state IN (?, ?) "
            f"AND COALESCE(posting_closed_at,'')<>''{guard}",
            (stamp, job_id, POSTING_CLOSED, FILLED),
        )
        moved = cur.rowcount == 1
        if moved:
            _record_reopen_event(conn, job_id, stamp)
    # Still parked in an automation state (shadow mode): keep the stamp so the
    # row stays explainable; otherwise the closure is simply over.
    conn.execute(
        "UPDATE jobs SET posting_closed_at='' WHERE id=? AND application_state NOT IN (?, ?) "
        f"AND COALESCE(posting_closed_at,'')<>''{guard}",
        (job_id, POSTING_CLOSED, FILLED),
    )
    return moved


def sweep_filled(conn: sqlite3.Connection, *, now: datetime, auto: bool) -> int:
    """Applied jobs whose posting has been closed for FILLED_AFTER_DAYS move
    to `filled`. A posting can close BECAUSE they are interviewing the user, which is
    why this waits and why screen/interview/offer are never touched."""
    if not auto:
        return 0
    cutoff = _stamp(now - timedelta(days=FILLED_AFTER_DAYS))
    due = (
        "review_state='kept' AND application_state='applied' AND posting_check='closed' "
        "AND COALESCE(posting_closed_at,'')<>'' AND posting_closed_at <= ?"
    )
    ids = [r["id"] for r in conn.execute(f"SELECT id FROM jobs WHERE {due}", (cutoff,))]
    moved = 0
    for job_id in ids:
        # Per-row CAS on the same predicate, so each move gets its own event.
        cur = conn.execute(
            "UPDATE jobs SET closed_prior_state='applied', application_state=?, "
            f"stage_changed_at=? WHERE id=? AND {due}",
            (FILLED, _stamp(now), job_id, cutoff),
        )
        if cur.rowcount == 1:
            app_events.insert_event(conn, job_id, FILLED, _stamp(now), source=_EVENT_SOURCE)
            _record_outcome(conn, job_id, FILLED)
            moved += 1
    conn.commit()
    return moved


def _record_reopen_event(conn: sqlite3.Connection, job_id: int, stamp: str) -> None:
    """A note, never the restored state as its own event: a restored 'applied'
    dated today would claim an application that happened weeks ago."""
    row = conn.execute(
        "SELECT COALESCE(application_state,'') AS state FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    restored = (row["state"] if row else "") or "shortlist"
    note = f"Posting reopened; back to {restored}"
    app_events.insert_event(conn, job_id, "note", stamp, note=note, source=_EVENT_SOURCE)


def would_move_to(row: Mapping[str, Any], *, now: datetime) -> str:
    """What automation would do to this row if closure_auto were on right now.
    Pure: mirrors close_transition and sweep_filled without writing."""
    if row["review_state"] != "kept" or row["posting_check"] != "closed":
        return NO_MOVE
    state = row["application_state"] or ""
    if state in AUTO_CLOSABLE and not row["posting_check_manual"]:
        return POSTING_CLOSED
    closed_at = row["posting_closed_at"] or ""
    cutoff = _stamp(now - timedelta(days=FILLED_AFTER_DAYS))
    if state == "applied" and closed_at and closed_at <= cutoff:
        return FILLED
    return NO_MOVE


def set_manual_verdict(
    conn: sqlite3.Connection, job_id: int, *, is_open: bool, now: datetime
) -> bool:
    """The user's own verdict. Recorded as manual so automation never overturns
    it, and applied regardless of closure_auto."""
    cur = conn.execute(
        "UPDATE jobs SET posting_check=?, posting_checked_at=?, posting_check_manual=1 WHERE id=?",
        ("verified_open" if is_open else "closed", _stamp(now), job_id),
    )
    if cur.rowcount != 1:
        conn.rollback()
        return False
    if is_open:
        reopen_transition(conn, job_id, auto=True, respect_manual=False)
    else:
        close_transition(conn, job_id, now=now, auto=True, respect_manual=False)
    conn.commit()
    return True


# --- recording verdicts --------------------------------------------------------


@dataclass
class _Pass:
    conn: sqlite3.Connection
    deps: LinkDeps
    now: datetime
    auto: bool
    result: ClosureResult
    index: dict
    radius_miles: float = closure_links.DEFAULT_RADIUS_MILES
    geo: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    shadow: bool = False  # distinct from `not auto`: closure_auto off still reverts
    searches: int = 0
    probes: int = 0


def _apply(ctx: _Pass, row: sqlite3.Row, verdict: str, *, checked: bool = False) -> None:
    """Record an automated verdict and run the transitions it implies.

    The verdict row is only rewritten when it changed or a network check really
    happened (`checked`), so "last checked" means a real check. If that UPDATE
    matches nothing, a manual verdict landed since `row` was read and nothing
    else runs. Unchanged kept rows still get the (idempotent, manual-guarded)
    transitions, which is how shadow-mode verdicts take effect once
    closure_auto is switched on."""
    prev = row["posting_check"] or ""
    changed = verdict != prev
    if changed or checked:
        cur = ctx.conn.execute(
            f"UPDATE jobs SET posting_check=?, posting_checked_at=? WHERE id=?{_NOT_MANUAL}",
            (verdict, _stamp(ctx.now), row["id"]),
        )
        if cur.rowcount != 1:
            return
        ctx.result.checked += 1
    elif row["review_state"] != "kept":
        return
    moved = False
    if verdict == "closed":
        moved = close_transition(ctx.conn, row["id"], now=ctx.now, auto=ctx.auto)
        # `moved` too: a shadow/dry run already stamped the verdict, so the real
        # run that finally moves the job sees changed=False but must announce it.
        if (changed or moved) and row["review_state"] == "kept":
            ctx.result.closed_kept.append(
                {"id": row["id"], "company": row["company"], "title": row["title"]}
            )
    elif verdict == "verified_open":
        moved = reopen_transition(ctx.conn, row["id"], auto=ctx.auto)
    elif prev == "closed":
        # The proof that closed it is gone (a dropped link): undo what that close
        # did. reopen_transition only restores a state automation set, and never
        # a manual verdict or a user move.
        moved = reopen_transition(ctx.conn, row["id"], auto=ctx.auto)
    ctx.result.moved += int(moved)


def _save_link(ctx: _Pass, job_id: int, link: Link) -> None:
    """Never overwrites the user's manual link (see MANUAL_LINK)."""
    ctx.result.linked += closure_links.save_link(ctx.conn, job_id, link)


def _networked(row: sqlite3.Row) -> bool:
    """Rows worth network work: kept, or an active surfaced/queued job."""
    if row["review_state"] == "kept":
        return True
    return row["review_state"] in _STAGE_RANK and (row["source_state"] or "active") == "active"


def _network_order(row: sqlite3.Row) -> tuple[str, int, float, int]:
    return (
        row["posting_checked_at"] or "",
        _STAGE_RANK.get(row["review_state"], len(_STAGE_RANK)),
        -(row["alignment_score"] or 0),
        row["id"],
    )


def _fallback_verdict(row: sqlite3.Row) -> str:
    """No proof available. Adzuna ageing out is evidence, never proof."""
    if row["source"] == "adzuna" and (row["source_state"] or "active") != "active":
        return "likely_closed"
    return ""


async def _check_existing_link(ctx: _Pass, row: sqlite3.Row) -> None:
    if row["employer_link_source"] == MANUAL_LINK:
        return  # display-only: never probed, never evidence
    link = Link(
        row["employer_link_source"], row["employer_link_external_id"], row["employer_link_url"]
    )
    held = ctx.conn.execute(
        "SELECT id, cluster_id, source_state FROM jobs WHERE source=? AND external_id=?",
        (link.source, link.external_id),
    ).fetchone()
    if held is not None and not _still_proof(ctx, row, held):
        _drop_link(ctx, row)
        return
    if held is not None:
        _apply(ctx, row, own_source_verdict(held["source_state"]))
        return
    if not _networked(row) or ctx.probes >= MAX_LINK_PROBES_PER_RUN:
        return
    ctx.probes += 1
    try:
        is_open, root = await _probe(ctx, row, link)
    except Exception as e:  # noqa: BLE001 -- a failed probe is not evidence of closure
        log.warning("closure: probe failed for job %s (%s)", row["id"], type(e).__name__)
        return
    if link.source.startswith("page:"):
        _sync_page_url(ctx, row, link, root)
        if page_links.is_limited(link, row["company"] or ""):
            # No evidence either way (#23): not closed, and not "verified open"
            # either; a close this page once caused is undone by _apply.
            _apply(ctx, row, "unverifiable", checked=True)
            return
    if is_open is not None:
        _apply(ctx, row, "verified_open" if is_open else "closed", checked=True)


def _still_proof(ctx: _Pass, row: sqlite3.Row, held: sqlite3.Row) -> bool:
    return closure_links.link_still_proof(ctx.conn, row, held["id"], ctx.radius_miles, ctx.geo)


def _rejected(ctx: _Pass, job_id: int, link: Link) -> bool:
    return closure_links.rejected_link(ctx.conn, job_id, link)


def _drop_link(ctx: _Pass, row: sqlite3.Row) -> None:
    """The proof behind the link is gone: forget the link and fall back to
    unverifiable. Never a state move; a manual verdict still wins. A description
    adopted from that very link goes with it (restored to what it replaced)."""
    cur = ctx.conn.execute(
        "UPDATE jobs SET employer_link_source='', employer_link_external_id='', "
        f"employer_link_url='' WHERE id=?{_NOT_MANUAL}",
        (row["id"],),
    )
    if cur.rowcount == 1:
        _revert_adopted_text(ctx, row)
    _apply(ctx, row, "unverifiable", checked=True)


def _revert_adopted_text(ctx: _Pass, row: sqlite3.Row) -> None:
    """Undo a description adopted over the link just dropped. A shadow run only
    reports it (`would_revert_jd`): the employer-JD step's own revalidation reverts
    it in a real run, once the link no longer proves it."""
    ref = closure_links.employer_jd_ref(
        Link(row["employer_link_source"], row["employer_link_external_id"], "")
    )
    if ctx.shadow:
        adopted = ctx.conn.execute(
            "SELECT 1 FROM jobs WHERE id=? AND jd_source=? AND jd_source_ref=? "
            "AND COALESCE(jd_pasted_at,'')=''",
            (row["id"], ref[0], ref[1]),
        ).fetchone()
        if adopted is not None:
            ctx.result.would_revert_jd.append(row["id"])
        return
    if dbc.revert_employer_jd(ctx.conn, row["id"], ref=ref):
        ctx.result.reverted_jd.append(row["id"])


async def _probe(ctx: _Pass, row: sqlite3.Row, link: Link) -> tuple[bool | None, str]:
    """(verdict, tenant root to store). A page link re-verifies the employer,
    title and region on every check; only a page probe can report a root."""
    if not link.source.startswith("page:"):
        return await ctx.deps.check_link(link), ""
    args = (link, row["company"] or "", row["title"] or "", row["geo_region"] or "")
    if ctx.deps.probe_page is None:
        return await ctx.deps.check_page(*args), ""
    checked = await ctx.deps.probe_page(*args)
    return checked.verdict, checked.tenant_root


def _sync_page_url(ctx: _Pass, row: sqlite3.Row, link: Link, root: str) -> None:
    """Keep a page link's shown URL in step with its trust level (#23): the page
    for full trust, the ATS tenant root for limited. A row stored before #23
    holds the page itself; its root is `root`, read off the page the probe just
    fetched, so no search is spent. Never drops or re-keys the link."""
    want = page_links.apply_url(link, row["company"] or "") or root
    if not want or want == link.url:
        return
    ctx.conn.execute(
        "UPDATE jobs SET employer_link_url=? WHERE id=? AND employer_link_source=? "
        f"AND employer_link_external_id=?{_NOT_MANUAL}",
        (want, row["id"], link.source, link.external_id),
    )
    ctx.conn.commit()


async def _link_and_check(ctx: _Pass, row: sqlite3.Row) -> None:
    held = closure_links.db_link(ctx.conn, row, ctx.index, ctx.radius_miles, ctx.geo)
    if held is not None:
        sibling, state = held
        _save_link(ctx, row["id"], sibling)
        _apply(ctx, row, own_source_verdict(state))
        return
    if is_agency(row["company"] or ""):
        _apply(ctx, row, "unverifiable")  # no network call, by design
        return
    if not company_key(row["company"] or ""):
        return  # nothing to search for; verdict untouched, cap not spent
    if ctx.searches >= MAX_LINK_SEARCHES_PER_RUN:
        return  # not stamped, so it sorts first next run
    ctx.searches += 1
    ctx.result.searched.add(row["id"])
    try:
        link = await closure_links.resolve_link(
            row["company"] or "", row["title"] or "", row["geo_region"] or "", ctx.deps
        )
    except Exception as e:  # noqa: BLE001 -- a failed search proves nothing either way
        log.warning("closure: link search failed for job %s (%s)", row["id"], type(e).__name__)
        return
    if link is not None and _rejected(ctx, row["id"], link):
        # The user said this posting is not this role; a search that keeps
        # finding it must not re-link it every run.
        _apply(ctx, row, _fallback_verdict(row) or "unverifiable", checked=True)
        return
    if link is None:
        _apply(ctx, row, _fallback_verdict(row), checked=True)
        return
    _save_link(ctx, row["id"], link)
    if page_links.is_limited(link, row["company"] or ""):
        _apply(ctx, row, "unverifiable", checked=True)  # linked, but no evidence (#23)
        return
    _apply(ctx, row, "verified_open", checked=True)  # just listed on the live board


def _sweep_local(ctx: _Pass, rows: list[sqlite3.Row]) -> tuple[list, list]:
    """Everything decidable without the network. Returns (linked, unlinked)
    for the network phase, unlinked being kept + active surfaced/queued."""
    linked: list[sqlite3.Row] = []
    unlinked: list[sqlite3.Row] = []
    for row in rows:
        if closure_links.is_authoritative(row["source"]):
            _apply(ctx, row, own_source_verdict(row["source_state"]))
        elif row["employer_link_source"] == MANUAL_LINK:
            continue  # display-only: no probe, no linking, no verdict change
        elif row["employer_link_source"]:
            linked.append(row)
        elif _networked(row):
            unlinked.append(row)
        elif _fallback_verdict(row) or row["posting_check"]:
            _apply(ctx, row, _fallback_verdict(row))
    return linked, unlinked


async def run_closure(
    cfg: BlastJobConfig,
    conn: sqlite3.Connection,
    *,
    deps: LinkDeps | None = None,
    shadow: bool = False,
    now: datetime | None = None,
) -> ClosureResult:
    """One closure pass over every non-manual job, then the 21-day sweep.
    `shadow=True` forces no transitions regardless of config."""
    ctx = _Pass(
        conn=conn,
        deps=deps or closure_links.default_deps(),
        now=now or datetime.now(),
        auto=cfg.discovery.closure_auto and not shadow,
        shadow=shadow,
        result=ClosureResult(),
        index=closure_links.authoritative_index(conn),
        radius_miles=cfg.discovery.commute_radius_miles,
        geo=cfg.discovery,
    )
    rows = conn.execute(f"SELECT {_ROW_COLS} FROM jobs WHERE posting_check_manual=0").fetchall()
    linked, unlinked = _sweep_local(ctx, rows)
    conn.commit()  # release the write lock before the first network await
    for row in sorted(linked, key=_network_order):
        await _check_existing_link(ctx, row)
        conn.commit()
    for row in sorted(unlinked, key=_network_order):
        await _link_and_check(ctx, row)
        conn.commit()
    ctx.result.filled = sweep_filled(conn, now=ctx.now, auto=ctx.auto)
    return ctx.result

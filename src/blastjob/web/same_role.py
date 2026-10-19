"""Same role, different hats: the cockpit side of core.same_role and
core.role_clusters.

Detail page: a warning when an agency/aggregator posting may be an employer's
own opening (or the reverse), an "employer not named" note for an agency
posting with no match, a "you applied to this role on <date>" note when the
same employer re-posts a role the user already applied to (PREVIOUS), and "Also
listed as" for every other member of the job's cluster, each with a "Not the
same role" undo. List rows: chips and the Mark applied / Advance confirmation,
computed in one batch per page.
"""

from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse

from blastjob.core import page_links
from blastjob.core import role_clusters as rc
from blastjob.core import same_role as sr
from blastjob.core.agencies import is_agency
from blastjob.models.config import BlastJobConfig, DiscoveryConfig
from blastjob.web import stages as st
from blastjob.web.apply_link import MANUAL_LINK, best_apply_link
from blastjob.web.security import require_csrf
from blastjob.web.service import stage_stamp
from blastjob.web.stage_lists import representative_key

# The stage pages that carry same-role chips.
CHIP_VIEWS: frozenset[str] = frozenset({st.SURFACED, st.QUEUED, st.SHORTLIST, st.APPLIED})
PARTIAL = "_same_role.html"


@dataclass(frozen=True)
class Member:
    """Another posting of the same role, as shown under "Also listed as"."""

    job_id: int
    company: str
    title: str
    city: str
    source: str
    stage: str
    url: str = ""
    employer_link_source: str = ""
    employer_link_url: str = ""


def stage_label(review_state: str, application_state: str, source_state: str) -> str:
    stage = st.stage_of(
        {
            "review_state": review_state,
            "application_state": application_state,
            "source_state": source_state,
        }
    )
    if stage is None:
        return review_state or "not reviewed"
    return st.LABELS[stage]


def candidate_label(c: sr.Candidate) -> str:
    """'Applied, Sep 13' / 'Shortlist' / 'archived'."""
    label = stage_label(c.review_state, c.application_state, c.source_state)
    date = sr.label_date(c.applied_at) if c.already_applied else ""
    return f"{label}, {date}" if date else label


def previous_label(c: sr.Candidate) -> str:
    """'You applied to this role on Sep 13 (rejected)'."""
    date = sr.label_date(c.applied_at)
    text = f"You applied to this role on {date}" if date else "You applied to this role"
    return f"{text} ({c.application_state})" if c.application_state else text


def _split(cands: list[sr.Candidate]) -> tuple[list[sr.Candidate], list[sr.Candidate]]:
    """(other-route candidates, previous applications)."""
    previous = [c for c in cands if c.kind == sr.PREVIOUS]
    return [c for c in cands if c.kind != sr.PREVIOUS], previous


def _previous_chip(previous: list[sr.Candidate]) -> tuple[str, bool]:
    """Red only when that application is still in progress: applying again into
    a live process is the costly error; after a closed one it may be a new req."""
    labels = (sr.label_date(c.applied_at) for c in previous)
    dates = ", ".join(dict.fromkeys(d for d in labels if d))
    chip = f"applied before: {dates}" if dates else "applied before"
    return chip, any(rc.is_active_application(_state_row(c)) for c in previous)


def _state_row(c: sr.Candidate) -> dict[str, str]:
    return {"review_state": c.review_state, "application_state": c.application_state}


def apply_confirm(cands: list[sr.Candidate]) -> str:
    """hx-confirm text for Mark applied / Advance, or "" when nothing the user
    has already applied to may be this role."""
    applied = [c for c in cands if c.already_applied]
    if not applied:
        return ""
    names = "; ".join(f"{c.company} · {c.title}" for c in applied)
    return f"You already applied to {names}, which may be this same role. Apply here too?"


def row_annotations(job: dict[str, Any], cands: list[sr.Candidate]) -> dict[str, Any]:
    """Chip + confirm fields for one list row (a new dict)."""
    agency = is_agency(job.get("company") or "")
    routes, previous = _split(cands)
    applied = [c for c in routes if c.already_applied]
    chip, red = "", False
    if agency and applied:
        chip, red = "already applied: " + _companies(applied), True
    elif agency and routes:
        chip = "possible duplicate of " + _companies(routes)
    elif previous:
        chip, red = _previous_chip(previous)
    return {
        **job,
        "same_role_chip": chip,
        "same_role_red": red,
        "employer_hidden": agency and not cands,
        "apply_confirm": apply_confirm(cands),
    }


def _companies(cands: list[sr.Candidate]) -> str:
    return ", ".join(dict.fromkeys(c.company for c in cands))


def annotate_rows(
    conn: sqlite3.Connection, rows: list[dict[str, Any]], radius_miles: float, geo: DiscoveryConfig
) -> list[dict[str, Any]]:
    """Every row with its same-role fields, from ONE batch lookup."""
    batch = sr.possible_same_role_batch(conn, [int(r["id"]) for r in rows], radius_miles, geo)
    return [row_annotations(r, batch.get(int(r["id"]), [])) for r in rows]


def _cluster_rows(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    """The job itself plus every other member of its cluster, in id order."""
    return conn.execute(
        "SELECT id, company, title, geo_city, geo_location_raw, source, url, cluster_id, "
        "employer_link_source, employer_link_external_id, employer_link_url, review_state, "
        "source_state, "
        "COALESCE(application_state,'') AS application_state "
        "FROM jobs WHERE id = ? OR cluster_id = (SELECT cluster_id FROM jobs WHERE id = ?) "
        "ORDER BY id",
        (job_id, job_id),
    ).fetchall()


def _members(rows: list[sqlite3.Row], job_id: int) -> list[Member]:
    return [
        Member(
            job_id=int(r["id"]),
            company=r["company"] or "",
            title=r["title"] or "",
            city=r["geo_city"] or r["geo_location_raw"] or "",
            source=r["source"] or "",
            stage=stage_label(r["review_state"], r["application_state"], r["source_state"]),
            url=r["url"] or "",
            employer_link_source=r["employer_link_source"] or "",
            employer_link_url=r["employer_link_url"] or "",
        )
        for r in rows
        if int(r["id"]) != job_id
    ]


def detail_context(
    conn: sqlite3.Connection, job: Any, radius_miles: float, geo: DiscoveryConfig
) -> dict[str, Any]:
    """Template context for the same-role block on one job's page."""
    job_id = int(job["id"])
    cands = sr.possible_same_role(conn, job_id, radius_miles, geo)
    rows = _cluster_rows(conn, job_id)
    members = _members(rows, job_id)
    own = next((r for r in rows if int(r["id"]) == job_id), None)
    link_source = (own["employer_link_source"] or "") if own is not None else ""
    linked = {m.job_id for m in members}
    open_cands = [c for c in cands if c.decision != rc.SAME and c.job_id not in linked]
    routes, previous = _split(open_cands)
    groups = sr.group_by_company(routes)
    agency = is_agency(job["company"] or "")
    return {
        "job_id": job_id,
        "sr_agency": agency,
        "sr_candidates": routes,
        "sr_previous": previous,
        "sr_previous_labels": {c.job_id: previous_label(c) for c in previous},
        "sr_labels": {c.job_id: candidate_label(c) for c in open_cands},
        "sr_applied": any(c.already_applied for c in routes),
        "sr_ambiguous": len(groups) > 1,
        "sr_employer_hidden": agency and not cands,
        "sr_members": members,
        "sr_error": "",
        "apply_confirm": apply_confirm(cands),
        "apply": best_apply_link(rows),
        "employer_link_manual": (
            (own["employer_link_url"] or "")
            if own is not None and link_source == MANUAL_LINK
            else ""
        ),
        # A limited careers page never prints its intermediary host (#23).
        "employer_link_auto": (
            page_links.employer_label(own) if own is not None and link_source != MANUAL_LINK else ""
        ),
        "employer_link_error": "",
    }


Render = Callable[..., HTMLResponse]


def register(
    app: FastAPI,
    conn: Callable[[], sqlite3.Connection],
    radius: Callable[[], float],
    render: Render,
    geo: Callable[[], DiscoveryConfig] = DiscoveryConfig,
    load_config: Callable[[], BlastJobConfig] | None = None,
) -> None:
    """POST /jobs/{id}/same-role/{other}/{same|different}: the user's verdict.
    Answers the re-rendered block for job `id`."""

    @app.post("/jobs/{job_id}/same-role/{other_id}/{decision}", response_class=HTMLResponse)
    async def same_role_decision(
        request: Request,
        job_id: int,
        other_id: int,
        decision: str,
        _: None = Depends(require_csrf),
    ) -> HTMLResponse:
        if decision not in rc.DECISIONS or job_id == other_id:
            return HTMLResponse("Invalid decision", status_code=400)
        c = conn()
        try:
            job = c.execute("SELECT id, company FROM jobs WHERE id=?", (job_id,)).fetchone()
            other = c.execute("SELECT id FROM jobs WHERE id=?", (other_id,)).fetchone()
            if job is None or other is None:
                return HTMLResponse("Not found", status_code=404)
            started: list[int] = []
            error = decide(
                c, job_id, other_id, decision, radius(), geo(),
                rescore=started.extend if load_config is not None else None,
            )  # fmt: skip
            if started and load_config is not None:
                from blastjob.web.employer_jd import start_rescore_jobs

                start_rescore_jobs(load_config, started)
            ctx = detail_context(c, job, radius(), geo())
            resp = render(request, PARTIAL, **{**ctx, "sr_error": error})
            if started:
                # base.html's #run-status re-reads /runs/active on this.
                resp.headers["HX-Trigger"] = "refresh-run-status"
            return resp
        finally:
            c.close()


def decide(
    conn: sqlite3.Connection,
    job_id: int,
    other_id: int,
    decision: str,
    radius: float,
    geo: DiscoveryConfig,
    rescore: Callable[[list[int]], object] | None = None,
) -> str:
    """Apply one verdict. 'same' joins the two clusters (refused, with the
    reason, if it would cross a 'different' pair or put two reqs from one
    board together) and reconciles their applications into one; 'different'
    records it and splits them apart. A split reverts any description adopted
    from the other posting; `rescore(ids)` is then called (after the split has
    committed) so those rows are scored against the snippet they hold again."""
    if decision == rc.DIFFERENT:
        reverted = rc.split_pair(conn, job_id, other_id, radius, geo)
        if reverted and rescore is not None:
            rescore(reverted)
        return ""
    return _decide_same(conn, job_id, other_id)


def _decide_same(conn: sqlite3.Connection, job_id: int, other_id: int) -> str:
    """The merge, the decision record and the application reconcile, as ONE
    `BEGIN IMMEDIATE` transaction: all of it lands or none of it does."""
    if conn.in_transaction:
        raise RuntimeError("same-role merge needs a connection with no open transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        reason = rc.merge_jobs(conn, job_id, other_id)
        if not reason:
            rc.record_decision(conn, job_id, other_id, rc.SAME)
            reason = reconcile_applications(conn, job_id)
    except BaseException:
        conn.rollback()
        raise
    if reason:
        conn.rollback()
        return f"Not linked: {reason}."
    conn.commit()
    return ""


# --- one role, one application ------------------------------------------------

_APP_COLS = (
    "id, source, review_state, COALESCE(application_state,'') AS application_state, "
    "COALESCE(applied_at,'') AS applied_at, alignment_score, COALESCE(app_notes,'') AS app_notes"
)
_NOTE_CAP = 300


def reconcile_applications(conn: sqlite3.Connection, job_id: int) -> str:
    """After a 'same' merge: when the merged cluster holds more than one
    application (rc.has_application), keep the one furthest along (the row the
    lists already show) and retire the rest as duplicates, noting each on the
    survivor. Does not commit. Returns "" or why it refused (the caller rolls
    back)."""
    members = conn.execute(
        f"SELECT {_APP_COLS} FROM jobs WHERE cluster_id = "
        "(SELECT cluster_id FROM jobs WHERE id=?) ORDER BY id",
        (job_id,),
    ).fetchall()
    apps = [m for m in members if rc.has_application(m)]
    if len(apps) < 2:
        return ""
    survivor = max(apps, key=representative_key)
    today = datetime.date.today().isoformat()
    lines: list[str] = []
    for dup in (m for m in apps if m["id"] != survivor["id"]):
        if not retire_duplicate(conn, dup, int(survivor["id"])):
            return f"#{dup['id']} changed while linking, so nothing was linked"
        lines.append(
            f"Merged #{dup['id']} ({dup['source']}, state {dup['application_state'] or '-'}, "
            f"applied_at {dup['applied_at'] or '-'}) as a duplicate on {today}"
        )
    old = survivor["app_notes"]
    new = "\n".join([old, *lines]) if old else "\n".join(lines)
    cur = conn.execute(
        "UPDATE jobs SET app_notes=? WHERE id=? AND COALESCE(app_notes,'')=?",
        (new, survivor["id"], old),
    )
    return "" if cur.rowcount == 1 else f"#{survivor['id']} changed while linking"


def retire_duplicate(conn: sqlite3.Connection, row: Any, survivor_id: int) -> bool:
    """Clear one duplicate's application and archive it, as one CAS on the
    state AND applied_at it was read with. Does not commit. Same effect as
    `service.archive_job(reason='other')`, without its commit."""
    cur = conn.execute(
        "UPDATE jobs SET review_state='archived', archive_reason='other', archive_note=?, "
        "application_state='', applied_at='', stage_changed_at=? "
        "WHERE id=? AND review_state='kept' AND COALESCE(application_state,'')=? "
        "AND COALESCE(applied_at,'')=?",
        (
            f"Duplicate of #{survivor_id}"[:_NOTE_CAP],
            stage_stamp(),
            row["id"],
            row["application_state"] or "",
            row["applied_at"] or "",
        ),
    )
    return cur.rowcount == 1

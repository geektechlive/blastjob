"""Where a job stands in the user's own process, for list chips and filters.

One status per row, most-advanced first: a closed or finished application, then
an application in progress, then a tailored packet, then not tailored yet.
Filtering never reorders a list: every list stays best fit first.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

# Filter keys in the order the filter bar shows them, with their labels.
FILTERS: tuple[tuple[str, str], ...] = (
    ("all", "All"),
    ("not_tailored", "Not tailored"),
    ("tailored", "Tailored"),
    ("applied", "Applied"),
    ("closed", "Closed"),
)
_IN_PROCESS = ("applied", "screen", "interview", "offer")
_FINISHED = ("rejected", "ghosted", "withdrawn", "posting_closed", "filled")


def work_status(job: Mapping[str, Any]) -> tuple[str, str]:
    """(filter key, chip label) for one job row."""
    app_state = job.get("application_state") or ""
    build = job.get("build_state") or "none"
    if app_state in _FINISHED:
        return "closed", app_state.replace("_", " ")
    # An application in progress outranks a closed posting: a posting can close
    # because they are interviewing the user.
    if app_state in _IN_PROCESS:
        return "applied", app_state
    if job.get("posting_check") == "closed":
        return "closed", "posting closed"
    if build == "tailored":
        return "tailored", "tailored"
    if build == "tailoring":
        return "not_tailored", "tailoring"
    if build == "failed":
        return "not_tailored", "tailor failed"
    return "not_tailored", "not tailored"


def filter_jobs(jobs: list[dict], status: str) -> tuple[list[dict], dict[str, int]]:
    """Annotate each job with `work_key`/`work_label`, then return the jobs
    matching `status` (order preserved) and a count per filter key. An unknown
    status shows everything rather than an empty page."""
    counts = {key: 0 for key, _ in FILTERS}
    for job in jobs:
        job["work_key"], job["work_label"] = work_status(job)
        counts[job["work_key"]] += 1
    counts["all"] = len(jobs)
    if status not in counts or status == "all":
        return jobs, counts
    return [j for j in jobs if j["work_key"] == status], counts


# Per-stage filter chips. Undecided stages (Surfaced, Queue, Matches) have none:
# nothing there is tailored or applied yet. A job may match several chips (a
# tailored posting can also be likely closed), so these are predicates.
_Pred = Callable[[Mapping[str, Any]], bool]
STAGE_FILTERS: dict[str, tuple[tuple[str, str, _Pred], ...]] = {
    "shortlist": (
        ("not_tailored", "Not tailored", lambda j: (j.get("build_state") or "none") != "tailored"),
        ("tailored", "Tailored", lambda j: j.get("build_state") == "tailored"),
        ("likely_closed", "Likely closed", lambda j: j.get("posting_check") == "likely_closed"),
    ),
    "applied": (
        ("applied", "Applied", lambda j: j.get("application_state") == "applied"),
        ("screen", "Screen", lambda j: j.get("application_state") == "screen"),
        ("interview", "Interview", lambda j: j.get("application_state") == "interview"),
        ("offer", "Offer", lambda j: j.get("application_state") == "offer"),
        ("due", "Follow-up due", lambda j: bool(j.get("followup_reason"))),
    ),
}


def filter_stage(
    jobs: list[dict], stage: str, status: str
) -> tuple[list[dict], dict[str, int], tuple[tuple[str, str], ...]]:
    """Annotate each job's row chip, then return (the jobs matching `status`,
    order preserved; a count per chip; the chips to show). A stage with no
    chips, or an unknown status, shows everything."""
    for job in jobs:
        job["work_key"], job["work_label"] = work_status(job)
    preds = STAGE_FILTERS.get(stage, ())
    counts = {"all": len(jobs)}
    for key, _label, pred in preds:
        counts[key] = sum(1 for j in jobs if pred(j))
    chips = (("all", "All"), *((key, label) for key, label, _ in preds)) if preds else ()
    match = next((pred for key, _label, pred in preds if key == status), None)
    if match is None:
        return jobs, counts, chips
    return [j for j in jobs if match(j)], counts, chips

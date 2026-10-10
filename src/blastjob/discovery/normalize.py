"""Map a RawPosting to job-table columns + a cross-source cluster fingerprint.

Discovery does NOT decide comp provenance heuristically here beyond "did the
source publish a range": a present range from Ashby/Adzuna is POSTED (or
ESTIMATED when the source flagged it predicted). JD-inferred comp is set later
by the matcher, never here. Geo distance/status is also deferred to the matcher;
Phase-1 only records the raw location + remote hint.
"""

from __future__ import annotations

import re

from blastjob.core.agencies import is_agency
from blastjob.discovery.base import RawPosting
from blastjob.models.job import CompProvenance

_NORM = re.compile(r"[^a-z0-9]+")

# "5 Locations" / "2 locations" — Workday's multi-site placeholder. It is a
# COUNT, not a place: writing it destroys a city the detail fetch already knew.
_PLACEHOLDER_LOCATION = re.compile(r"^\d+\s+locations?$", re.I)

# Remote/hybrid intent in free-form location text. Hybrid is checked first and
# wins: a hybrid role still has to be commutable, so it is NOT remote.
_HYBRID = re.compile(r"\bhybrid\b", re.I)
_REMOTE = re.compile(
    r"\bremote\b|\bwork from home\b|\bwfh\b|\btelecommut\w*\b|\bvirtual office\b", re.I
)

_WORKPLACE_REMOTE = {
    "remote": True,
    "hybrid": False,
    "onsite": False,
    "on-site": False,
    "on site": False,
    "in-office": False,
}


def resolve_remote(p: RawPosting) -> bool | None:
    """True / False / None ("the source didn't say").

    Order matters: a structured flag (Ashby's `isRemote`) is authoritative and
    is never second-guessed from text; then the structured workplace type; then
    the location string, which is the only signal Workday gives for the roles
    literally located at "Remote-OH".
    """
    if p.remote is not None:
        return p.remote
    wt = (p.workplace_type or "").strip().lower()
    if wt in _WORKPLACE_REMOTE:
        return _WORKPLACE_REMOTE[wt]
    text = p.location_raw or ""
    if _HYBRID.search(text):
        return False
    if _REMOTE.search(text):
        return True
    return None


def _norm(s: str) -> str:
    return _NORM.sub(" ", (s or "").lower()).strip()


def cluster_fingerprint(p: RawPosting) -> str:
    """Stable key for grouping the SAME role seen across sources (company board +
    Adzuna). Deliberately coarse; clustering is reversible and display-only, so a
    false group is harmless — unlike a destructive identity merge."""
    from blastjob.core.company import company_key
    from blastjob.core.titles import normalize_role_title

    # company_key strips Inc/LLC/Corp and punctuation, so "Fabrikam Services" and
    # "Fabrikam Services Inc" (Adzuna vs a board) fold into one cluster. The title
    # uses the same normalizer as the same-role rules (VP == Vice President).
    where = _norm(p.city) or _norm(p.region)
    return f"{company_key(p.company)}|{normalize_role_title(p.title)}|{where}"


def to_job_columns(p: RawPosting) -> dict:
    """RawPosting -> kwargs for db.client.upsert_job (source/external_id passed
    separately by the pipeline).

    Comp columns are emitted ONLY when the source published pay. When it didn't,
    they're omitted so a rediscovery refresh can't overwrite comp the matcher
    later inferred from the JD with zeros.
    """
    cols: dict[str, object] = {
        "company": p.company,
        "title": p.title,
        "url": p.url,
    }
    # Geo is emitted ONLY where the adapter actually knows something. A cheap
    # refresh (Workday's presence stub) carries no location and no remote flag,
    # and upsert_job only UPDATEs the keys it is handed — so absent here means
    # "unchanged in the DB" rather than "overwrite the detail-derived value with
    # a placeholder", which is what degraded 120 live rows to "N Locations".
    raw = (p.location_raw or "").strip()
    if raw and not _PLACEHOLDER_LOCATION.match(raw):
        cols["geo_location_raw"] = raw
        if p.city.strip():
            cols["geo_city"] = p.city.strip()
        if p.region.strip():
            cols["geo_region"] = p.region.strip()
    elif not raw:
        # Structured city/region with no free-form string (Ashby's postalAddress).
        if p.city.strip():
            cols["geo_city"] = p.city.strip()
        if p.region.strip():
            cols["geo_region"] = p.region.strip()
    remote = resolve_remote(p)
    if remote is not None:
        cols["geo_remote"] = 1 if remote else 0
    # An empty description means the adapter skipped the detail fetch for a
    # posting we already hold (Workday does this to keep daily runs cheap). Omit
    # the column so the refresh never blanks a JD we already scored against.
    if p.description:
        cols["raw_jd"] = p.description
    if p.comp_max or p.comp_min:
        # An intermediary's range is never the employer's published figure. A
        # LinkedIn posting says "This range is provided by <X>" where X is
        # whoever listed it: for the employer itself that IS the employer, but
        # for a staffing reseller it is a reseller quoting a number the employer
        # never published. Only POSTED comp may hard-reject a role at the gate
        # (matching/gate.py), so an intermediary's figure is ESTIMATED — it
        # still surfaces the job, it just cannot bury it.
        estimated = p.comp_is_estimated or is_agency(p.company)
        provenance = CompProvenance.ESTIMATED if estimated else CompProvenance.POSTED
        cols.update(
            comp_min=p.comp_min,
            comp_max=p.comp_max,
            comp_total_est=p.comp_max or p.comp_min,
            comp_currency=p.comp_currency,
            comp_confidence=0.5 if estimated else 0.9,
            comp_provenance=provenance.value,
        )
    return cols

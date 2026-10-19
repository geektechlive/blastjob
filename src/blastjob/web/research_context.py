"""The job context packet research anchors on (issue #59): where the job is and
which links are the employer's own, so pages about a same-named company
elsewhere are not used as this employer's facts (`core/research_match.py`).
"""

from __future__ import annotations

import re
import sqlite3

from blastjob.core.agencies import is_agency
from blastjob.core.closure_links import is_authoritative
from blastjob.core.company import company_key
from blastjob.core.research_match import JobContext
from blastjob.discovery.textutil import us_state_code
from blastjob.web.apply_link import APPLY_COLS, apply_links_for

_COLS = f"{APPLY_COLS}, title, raw_jd, geo_city, geo_region, geo_location_raw"
# "WI 53202" -> "WI": a trailing ZIP (or ZIP+4) must not hide the state.
_ZIP = re.compile(r"\s+\d{5}(?:-\d{4})?$")


def _city_region(row: sqlite3.Row) -> tuple[str, str]:
    """The row's city/region, else read off "City, ST" in the raw location (the
    first segment of a multi-location "A; B" string)."""
    city = (row["geo_city"] or "").strip()
    region = _ZIP.sub("", (row["geo_region"] or "").strip())
    if city and region:
        return city, region
    first = (row["geo_location_raw"] or "").split(";")[0]
    parts = [_ZIP.sub("", p.strip()) for p in first.split(",") if p.strip()]
    if not region and len(parts) >= 2 and us_state_code(parts[1]):
        region = parts[1]
    if not city and len(parts) >= 2 and us_state_code(parts[1]):
        city = parts[0]
    return city, region


def _employer_urls(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[str, ...]:
    """Links that are the employer's own: an authoritative board posting, a link
    closure found or the user pasted, and the role's best apply link unless it is
    an agency's. Aggregator and shared-ATS hosts are filtered by research_match."""
    if is_agency(row["company"] or ""):
        return ()
    urls: list[str] = []
    if row["employer_link_source"] and row["employer_link_url"]:
        urls.append(row["employer_link_url"])
    if is_authoritative(row["source"] or "") and row["url"]:
        urls.append(row["url"])
    link = apply_links_for(conn, [row]).get(int(row["id"]))
    if link is not None and not link.label.endswith("(agency)"):
        urls.append(link.url)
    return tuple(dict.fromkeys(urls))


def job_research_context(conn: sqlite3.Connection, job_id: int) -> JobContext | None:
    row = conn.execute(f"SELECT {_COLS} FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    return _context_from_row(conn, row)


# Cap on how many of a company's postings anchor one Research-button run.
MAX_COMPANY_CONTEXTS = 25


def _context_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> JobContext:
    city, region = _city_region(row)
    return JobContext(
        company=row["company"] or "",
        title=row["title"] or "",
        city=city,
        region=region,
        urls=_employer_urls(conn, row),
        jd=row["raw_jd"] or "",
    )


def company_job_contexts(
    conn: sqlite3.Connection, company: str, *, first_job_id: int | None = None
) -> list[JobContext]:
    """One context per live posting of `company` (issue #60), the Research
    button's anchors: where each job is and which links are the employer's own.

    "Live" means not archived and still listed by its source: an archived posting
    may be the very same-name company the owner dismissed. Company identity is
    `company_key`, so spelling variants of the name share anchors. `first_job_id`
    (the job whose page the button was pressed on) leads, so its description is
    the one the identity judgment reads; kept postings come before the rest."""
    key = company_key(company)
    # Names only first: raw_jd is large and most jobs are other companies'.
    named = conn.execute(
        "SELECT id, company FROM jobs "
        "WHERE (COALESCE(review_state, 'new') != 'archived' OR id = ?) "
        "AND (COALESCE(source_state, 'active') = 'active' OR id = ?) "
        "ORDER BY (id = ?) DESC, (review_state = 'kept') DESC, id DESC",
        (first_job_id, first_job_id, first_job_id),
    ).fetchall()
    ids = [r["id"] for r in named if company_key(r["company"] or "") == key]
    ids = ids[:MAX_COMPANY_CONTEXTS]
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    rows = {
        r["id"]: r
        for r in conn.execute(f"SELECT {_COLS} FROM jobs WHERE id IN ({marks})", ids).fetchall()
    }
    return [_context_from_row(conn, rows[i]) for i in ids]

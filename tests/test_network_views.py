"""Turns the rich-but-inert `network_contacts` data into three actionable
views: warm paths for a specific job, a ranked recruiter desk, and a ranked
list of dormant senior ties. Deterministic, read-only, no LLM."""

from __future__ import annotations

import tempfile
from datetime import date

import pytest

from blastjob.core import network_views as nv
from blastjob.core.company import company_key
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig

TODAY = "2026-09-12"


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    yield c
    c.close()


class _CountingConnection:
    """Wraps a real sqlite3.Connection and counts calls to `.execute()`, so a
    test can assert a view is one batched query rather than N+1. Everything
    else delegates straight to the wrapped connection."""

    def __init__(self, real):
        self._real = real
        self.execute_count = 0

    def execute(self, *args, **kwargs):
        self.execute_count += 1
        return self._real.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _add_contact(
    conn,
    *,
    full_name: str,
    company: str,
    position: str = "",
    seniority: str = "ic",
    is_recruiter: int = 0,
    recommended_me: int = 0,
    last_message_at: str = "",
    message_count: int = 0,
    url: str = "",
) -> int:
    """`company_key` is deliberately NOT an argument -- it's always derived
    from `company` via the real `company_key()`, the same as a real import
    would do. Hand-typing a key here previously drifted from what
    `company_key()` actually normalizes to (e.g. "Acme Corp" -> "acme", not
    "acme corp"), which silently broke every company-matching assertion."""
    now = "2026-01-01T00:00:00"
    cur = conn.execute(
        "INSERT INTO network_contacts "
        "(full_name, url, company, company_key, position, seniority, is_recruiter, "
        " recommended_me, last_message_at, message_count, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            full_name,
            url or f"https://linkedin.com/in/example-{full_name.lower().replace(' ', '')}",
            company,
            company_key(company),
            position,
            seniority,
            is_recruiter,
            recommended_me,
            last_message_at,
            message_count,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _add_job(conn, *, company: str, review_state: str = "surfaced", source_state: str = "active"):
    return dbc.upsert_job(
        conn,
        source="test",
        external_id=company + "-" + review_state + "-" + str(id(company)),
        company=company,
        title="Some Role",
        review_state=review_state,
        source_state=source_state,
    )


# --------------------------------------------------------------------------
# warm_paths_for_job
# --------------------------------------------------------------------------


# 1. A recommender outranks a more senior non-recommender.
def test_warm_paths_for_job_recommender_beats_higher_seniority(conn):
    job_id = _add_job(conn, company="Acme Corp")
    _add_contact(
        conn,
        full_name="Vera VP",
        company="Acme Corp",
        position="VP Engineering",
        seniority="exec",
        recommended_me=0,
    )
    _add_contact(
        conn,
        full_name="Mandy Manager",
        company="Acme Corp",
        position="Engineering Manager",
        seniority="manager",
        recommended_me=1,
    )

    result = nv.warm_paths_for_job(conn, job_id)

    assert [r["name"] for r in result] == ["Mandy Manager", "Vera VP"]
    assert result[0]["recommended_me"] is True
    assert result[1]["recommended_me"] is False


# 2. Every field the deliverable promises is present and traces to the stored row.
def test_warm_paths_for_job_returns_expected_fields(conn):
    job_id = _add_job(conn, company="Acme Corp")
    _add_contact(
        conn,
        full_name="Vera VP",
        company="Acme Corp",
        position="VP Engineering",
        seniority="exec",
        last_message_at="2026-05-01",
        url="https://linkedin.com/in/example-verav",
    )

    result = nv.warm_paths_for_job(conn, job_id)

    assert result == [
        {
            "name": "Vera VP",
            "position": "VP Engineering",
            "seniority": "exec",
            "url": "https://linkedin.com/in/example-verav",
            "last_message_at": "2026-05-01",
            "recommended_me": False,
        }
    ]


# 3. A job at a company with no connections returns an empty list, not an error.
def test_warm_paths_for_job_no_connections_returns_empty(conn):
    job_id = _add_job(conn, company="Nobody Knows Inc")

    assert nv.warm_paths_for_job(conn, job_id) == []


# 4. An unknown job id returns an empty list.
def test_warm_paths_for_job_unknown_job_returns_empty(conn):
    assert nv.warm_paths_for_job(conn, 999999) == []


# --------------------------------------------------------------------------
# recruiter_desk
# --------------------------------------------------------------------------


# 5. An agency recruiter (works at a staffing/search firm) is classified
# separately from an in-house recruiter (works at the hiring company).
def test_recruiter_desk_splits_agency_from_in_house(conn):
    _add_contact(
        conn,
        full_name="Rae Recruiter",
        company="Acme Executive Search",
        position="Executive Recruiter",
        seniority="ic",
        is_recruiter=1,
        message_count=5,
        last_message_at=TODAY,
    )
    _add_contact(
        conn,
        full_name="Ian Housetalent",
        company="Fabrikam's",
        position="Talent Acquisition Partner",
        seniority="ic",
        is_recruiter=1,
        message_count=5,
        last_message_at=TODAY,
    )

    result = nv.recruiter_desk(conn)

    by_name = {r["name"]: r for r in result}
    assert by_name["Rae Recruiter"]["group"] == "agency"
    assert by_name["Ian Housetalent"]["group"] == "in_house"


# 6. Only contacts flagged is_recruiter=1 appear on the desk.
def test_recruiter_desk_excludes_non_recruiters(conn):
    _add_contact(
        conn,
        full_name="Norm Nonrecruiter",
        company="Acme Corp",
        position="Software Engineer",
        is_recruiter=0,
    )

    assert nv.recruiter_desk(conn) == []


# 7. A recruiter whose company has a live (non-archived) posting is flagged.
def test_recruiter_desk_flags_company_with_live_jobs(conn):
    _add_job(conn, company="Acme Corp", review_state="surfaced")
    _add_contact(
        conn,
        full_name="Rae Recruiter",
        company="Acme Corp",
        position="Recruiter",
        is_recruiter=1,
    )
    _add_contact(
        conn,
        full_name="Nia Norole",
        company="No Jobs Here LLC",
        position="Recruiter",
        is_recruiter=1,
    )

    result = nv.recruiter_desk(conn)

    by_name = {r["name"]: r for r in result}
    assert by_name["Rae Recruiter"]["has_live_jobs"] is True
    assert by_name["Nia Norole"]["has_live_jobs"] is False


# 8. An archived posting does not count as a "live" job.
def test_recruiter_desk_archived_job_does_not_count_as_live(conn):
    _add_job(conn, company="Acme Corp", review_state="archived")
    _add_contact(
        conn,
        full_name="Rae Recruiter",
        company="Acme Corp",
        position="Recruiter",
        is_recruiter=1,
    )

    result = nv.recruiter_desk(conn)

    assert result[0]["has_live_jobs"] is False


# 9. Recency is visible in the ranking, not swamped by raw volume: a recruiter
# with fewer messages but a very recent one outranks a much higher-volume
# recruiter who has gone cold for a long time.
def test_recruiter_desk_recency_is_not_hidden_by_volume(conn):
    _add_contact(
        conn,
        full_name="Cold Highvolume",
        company="Acme Corp",
        position="Recruiter",
        is_recruiter=1,
        message_count=54,
        last_message_at="2024-01-01",
    )
    _add_contact(
        conn,
        full_name="Warm Lowvolume",
        company="Beta LLC",
        position="Recruiter",
        is_recruiter=1,
        message_count=2,
        last_message_at="2026-09-10",
    )

    result = nv.recruiter_desk(conn)

    by_name = {r["name"]: r for r in result}
    assert by_name["Warm Lowvolume"]["warmth_score"] > by_name["Cold Highvolume"]["warmth_score"]


# 10. recruiter_desk issues exactly one query to network_contacts and one
# batched query for live-job company_keys -- never one query per recruiter.
def test_recruiter_desk_is_batched_not_n_plus_one(conn):
    for i in range(10):
        _add_contact(
            conn,
            full_name=f"Recruiter {i}",
            company=f"Company {i}",
            position="Recruiter",
            is_recruiter=1,
        )
    counting = _CountingConnection(conn)

    nv.recruiter_desk(counting)

    assert counting.execute_count <= 2


# --------------------------------------------------------------------------
# dormant_ties
# --------------------------------------------------------------------------


# 11. Only exec/director contacts with message_count > 0 are dormant ties.
def test_dormant_ties_filters_to_exec_director_with_messages(conn):
    _add_contact(
        conn,
        full_name="Exec No Messages",
        company="Acme Corp",
        seniority="exec",
        message_count=0,
    )
    _add_contact(
        conn,
        full_name="Ic With Messages",
        company="Acme Corp",
        seniority="ic",
        message_count=3,
    )
    _add_contact(
        conn,
        full_name="Director With Messages",
        company="Acme Corp",
        seniority="director",
        message_count=3,
        last_message_at="2026-01-01",
    )

    result = nv.dormant_ties(conn)

    assert [r["name"] for r in result] == ["Director With Messages"]


# 12. A dormant tie at a company with live roles outranks one without, even
# when the one without has a slightly higher raw warmth score.
def test_dormant_ties_live_company_outranks_no_live_company(conn):
    _add_job(conn, company="Has Jobs Inc", review_state="queued")
    _add_contact(
        conn,
        full_name="At Live Company",
        company="Has Jobs Inc",
        seniority="director",
        message_count=2,
        last_message_at="2025-01-01",
    )
    _add_contact(
        conn,
        full_name="At Dead Company",
        company="No Jobs Corp",
        seniority="exec",
        message_count=10,
        last_message_at="2026-09-01",
    )

    result = nv.dormant_ties(conn)

    assert [r["name"] for r in result] == ["At Live Company", "At Dead Company"]


# 13. `limit` caps the result.
def test_dormant_ties_respects_limit(conn):
    for i in range(5):
        _add_contact(
            conn,
            full_name=f"Exec {i}",
            company=f"Company {i}",
            seniority="exec",
            message_count=1,
        )

    assert len(nv.dormant_ties(conn, limit=2)) == 2


# 14. dormant_ties is batched, not N+1.
def test_dormant_ties_is_batched_not_n_plus_one(conn):
    for i in range(10):
        _add_contact(
            conn,
            full_name=f"Exec {i}",
            company=f"Company {i}",
            seniority="exec",
            message_count=1,
        )
    counting = _CountingConnection(conn)

    nv.dormant_ties(counting)

    assert counting.execute_count <= 2


# --------------------------------------------------------------------------
# warmth_score
# --------------------------------------------------------------------------


# 15. Zero messages and no seniority weight floors at (or near) zero.
def test_warmth_score_zero_messages_and_ic_is_lowest():
    score = nv.warmth_score(0, "", "ic", TODAY)
    assert score == 0.0


# 16. More messages, all else equal, scores higher.
def test_warmth_score_more_messages_scores_higher():
    low = nv.warmth_score(1, TODAY, "ic", TODAY)
    high = nv.warmth_score(10, TODAY, "ic", TODAY)
    assert high > low


# 17. More recent contact, all else equal, scores higher.
def test_warmth_score_more_recent_scores_higher():
    stale = nv.warmth_score(5, "2020-01-01", "ic", TODAY)
    fresh = nv.warmth_score(5, TODAY, "ic", TODAY)
    assert fresh > stale


# 18. Higher seniority, all else equal, scores higher.
def test_warmth_score_higher_seniority_scores_higher():
    ic = nv.warmth_score(5, TODAY, "ic", TODAY)
    exec_ = nv.warmth_score(5, TODAY, "exec", TODAY)
    assert exec_ > ic


# 19. A blank last_message_at is treated as "never" -- lowest recency, not
# an error, not treated as "today".
def test_warmth_score_blank_last_message_at_is_never():
    blank = nv.warmth_score(5, "", "ic", TODAY)
    ancient = nv.warmth_score(5, "2000-01-01", "ic", TODAY)
    fresh = nv.warmth_score(5, TODAY, "ic", TODAY)
    assert blank <= ancient < fresh


# 20. An unparseable date string does not raise -- treated as "never".
def test_warmth_score_unparseable_date_does_not_raise():
    score = nv.warmth_score(5, "not-a-date", "ic", TODAY)
    assert score >= 0.0


# 21. Same day contact (days_since == 0) is the maximum recency case.
def test_warmth_score_same_day_is_max_recency():
    same_day = nv.warmth_score(5, TODAY, "ic", TODAY)
    one_year_ago = nv.warmth_score(5, "2025-09-12", "ic", TODAY)
    assert same_day > one_year_ago


# 22. Never negative, regardless of input.
def test_warmth_score_never_negative():
    assert nv.warmth_score(0, "", "ic", TODAY) >= 0.0
    assert nv.warmth_score(0, "1999-01-01", "unknown", TODAY) >= 0.0


# 23. An unrecognized seniority label degrades gracefully rather than raising.
def test_warmth_score_unknown_seniority_does_not_raise():
    score = nv.warmth_score(3, TODAY, "something-weird", TODAY)
    assert score >= 0.0


def test_warmth_score_is_deterministic():
    a = nv.warmth_score(7, "2026-03-01", "director", TODAY)
    b = nv.warmth_score(7, "2026-03-01", "director", TODAY)
    assert a == b


def test_today_constant_is_a_real_date():
    # sanity check on the fixture constant itself
    assert date.fromisoformat(TODAY)

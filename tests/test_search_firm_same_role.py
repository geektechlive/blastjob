"""Search-firm listings count as agencies and are paired with the employer's posting (#112).

Decision: ADR 0072. Matching stays suggest-only; nothing here links two jobs.
"""

from __future__ import annotations

import tempfile

import pytest

import tests.agency_fixture as agency_fixture
import tests.geo_fixture as geo_fixture
from blastjob.core import same_role as sr
from blastjob.core.agencies import RETAINED_SUFFIX, is_agency
from blastjob.core.titles import normalize_role_title, role_segment
from blastjob.db import client as dbc
from blastjob.models.config import BlastJobConfig

FIRM = "Acme Executive Search (retained search)"
GEO = geo_fixture.region_cfg()
RADIUS = BlastJobConfig().discovery.commute_radius_miles
_seq = iter(range(1, 1_000_000))


@pytest.fixture(autouse=True)
def _fixture_agencies() -> None:
    agency_fixture.install_agencies()


@pytest.fixture
def conn():
    cfg = BlastJobConfig()
    geo_fixture.apply_region(cfg)
    cfg.paths.data_dir = tempfile.mkdtemp()
    c = dbc.init_db(cfg)
    yield c
    c.close()


def _job(conn, *, company: str, title: str, city: str = "Fairhaven", **cols) -> int:
    n = next(_seq)
    jid = dbc.upsert_job(
        conn,
        source=cols.pop("source", "adzuna"),
        external_id=f"s{n}",
        company=company,
        title=title,
        geo_city=city,
        geo_location_raw=cols.pop("raw", f"{city}, OH" if city else ""),
    )
    cols.setdefault("first_seen", "2026-11-01T10:00:00")
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), jid))
    conn.commit()
    return jid


# -- is_agency -----------------------------------------------------------------


def test_the_retained_search_suffix_makes_an_agency() -> None:
    assert RETAINED_SUFFIX == " (retained search)"
    assert is_agency(FIRM)
    assert is_agency("Globex Retained Search (retained search)")


def test_a_company_with_search_in_its_name_is_still_an_employer() -> None:
    assert not is_agency("Contoso Dining Search")
    assert not is_agency("Contoso Search")
    assert not is_agency("Contoso (retained search) Holdings")


# -- role_segment --------------------------------------------------------------


def test_role_segment_picks_the_one_title_shaped_segment() -> None:
    assert role_segment("Fabrikam Credit Union - Chief Information Officer") == (
        "Chief Information Officer"
    )
    assert role_segment("Chief Information Officer | Fabrikam Credit Union") == (
        "Chief Information Officer"
    )
    assert role_segment("Fabrikam Credit Union – VP of Technology") == "VP of Technology"


def test_role_segment_is_none_when_zero_or_several_segments_match() -> None:
    assert role_segment("Fabrikam Credit Union - Operations Lead") is None
    assert role_segment("Chief Information Officer - Director of Security") is None
    assert role_segment("Fabrikam Credit Union") is None


def test_role_segment_of_a_bare_title_is_the_title() -> None:
    assert role_segment("Chief Information Officer") == "Chief Information Officer"


# -- suggestions ---------------------------------------------------------------


def test_employer_posting_is_suggested_for_a_firm_row_with_a_prefixed_title(conn) -> None:
    employer = _job(conn, company="Fabrikam Credit Union", title="Chief Information Officer")
    firm = _job(
        conn,
        company=FIRM,
        title="Fabrikam Credit Union - Chief Information Officer",
        source="searchfirm:acme",
    )

    assert [(c.job_id, c.kind) for c in sr.possible_same_role(conn, firm, RADIUS, GEO)] == [
        (employer, sr.EMPLOYER)
    ]
    assert [(c.job_id, c.kind) for c in sr.possible_same_role(conn, employer, RADIUS, GEO)] == [
        (firm, sr.AGENCY)
    ]


def test_no_suggestion_when_the_metro_differs(conn) -> None:
    firm = _job(
        conn,
        company=FIRM,
        title="Fabrikam Credit Union - Chief Information Officer",
        source="searchfirm:acme",
    )
    _job(
        conn,
        company="Fabrikam Credit Union",
        title="Chief Information Officer",
        city="Stockton",
        raw="Stockton, CA",
    )

    assert sr.possible_same_role(conn, firm, RADIUS, GEO) == []


def test_a_firm_title_that_is_already_the_bare_role_gives_one_candidate(conn) -> None:
    employer = _job(conn, company="Fabrikam Credit Union", title="Chief Information Officer")
    firm = _job(conn, company=FIRM, title="Chief Information Officer", source="searchfirm:acme")

    assert [c.job_id for c in sr.possible_same_role(conn, firm, RADIUS, GEO)] == [employer]
    assert [c.job_id for c in sr.possible_same_role(conn, employer, RADIUS, GEO)] == [firm]
    assert sr.SameRoleIndex(conn, RADIUS, GEO).peer_ids(firm) == [employer]


def test_an_employer_titled_with_a_separator_is_not_given_extra_keys(conn) -> None:
    # Only agency rows are indexed under their role segment.
    employer = _job(
        conn, company="Fabrikam Credit Union", title="Fabrikam - Chief Information Officer"
    )
    other = _job(conn, company="Globex", title="Chief Information Officer")

    assert sr.possible_same_role(conn, employer, RADIUS, GEO) == []
    assert sr.possible_same_role(conn, other, RADIUS, GEO) == []


def test_normalized_titles_still_differ_so_only_agency_rows_use_the_segment() -> None:
    assert normalize_role_title("Fabrikam Credit Union - Chief Information Officer") != (
        normalize_role_title("Chief Information Officer")
    )

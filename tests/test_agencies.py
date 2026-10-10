"""Tests for core.agencies.is_agency -- pure (no DB, no web imports), so this
file imports `blastjob.core.agencies` directly rather than reaching it through
`web.compare`'s re-export (that path only pins the re-export, not purity).
"""

from blastjob.core import agencies
from blastjob.core import company as co
from blastjob.core.agencies import is_agency


def test_configured_agencies_and_built_in_platforms() -> None:
    agencies.set_extra_agencies(["Acme Talent Partners"])
    assert is_agency("Acme Talent Partners")
    assert is_agency("Acme Talent Partners Inc")
    assert is_agency("Acme Talent Partners Government Solutions")
    assert is_agency("LinkedIn")


def test_explicit_2026_09_15_additions() -> None:
    assert is_agency("Lakeview Hire Staff")
    assert is_agency("Bridgetown Talent")


def test_trailing_noun_heuristic_catches_unlisted_staffing_shops() -> None:
    assert is_agency("Acme Staffing")
    assert is_agency("Acme Recruiters")
    assert is_agency("Northshore Recruiting")
    assert is_agency("Beacon Personnel")
    assert is_agency("Lakeside Placement")
    assert is_agency("Lakeside Placements")


def test_trailing_noun_must_be_the_whole_last_token() -> None:
    # "talent" leads, not trails -- not caught by the heuristic.
    assert not is_agency("Talent Systems Inc")
    # merged word, not the standalone token "talent".
    assert not is_agency("Ian Housetalent")


def test_negatives_stay_false() -> None:
    assert not is_agency("Lakeshore Credit Union")
    assert not is_agency("Fabrikam Industries")
    assert not is_agency("Northshore Tools")
    assert not is_agency("Fabrikam Research Associates, Inc.")


def test_search_firms_deliberately_not_flagged() -> None:
    # company_key does not strip parentheticals, so these already end in
    # "search" -- see the deliberate exclusion of "search" from
    # _TRAILING_AGENCY_NOUNS in agencies.py for why this stays False.
    assert co.company_key("Acme Executive Search (retained search)") == (
        "acme executive search retained search"
    )
    assert not is_agency("Acme Executive Search (retained search)")
    assert not is_agency("Globex Retained Search (retained search)")
    assert not is_agency("Contoso Dining Search")


def test_empty_and_blank_company_do_not_crash() -> None:
    assert not is_agency("")
    assert not is_agency("   ")


def test_single_token_agency_noun_alone_stays_false() -> None:
    # A bare "Staffing" (no leading token) shouldn't match -- too thin a
    # signal, and company_key("") edge cases should never be the trigger.
    assert not is_agency("Staffing")


def test_a_configured_agency_is_not_an_agency_until_configured() -> None:
    assert not is_agency("Acme Talent Partners")
    agencies.set_extra_agencies(["ACME Talent Partners, Inc."])
    assert is_agency("Acme Talent Partners Government Solutions")
    agencies.set_extra_agencies([])
    assert not is_agency("Acme Talent Partners")

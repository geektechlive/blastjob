"""Who is not the employer: job aggregators and staffing/recruiting agencies.

Pure (no database, no web imports) so discovery, core and the web layer can
all ask the same question.
"""

from __future__ import annotations

from collections.abc import Iterable

from blastjob.core import company as co

# Companies that are recruiting platforms / job aggregators, not the employer.
# Keyed the same way as company_key() so name variants ("Dice", "DICE.com")
# still match.
#
# Only the platforms themselves are built in. The agencies, staffing shops and
# job-board resellers that show up in YOUR feed are a setting
# (`discovery.agency_companies`), installed by `set_extra_agencies` whenever the
# config is loaded. Resellers matter most: they repost other companies'
# openings under their own name, so the posting looks like an employer listing
# and scores like one.
AGGREGATOR_COMPANIES: frozenset[str] = frozenset(
    {
        "lensa",
        "jobot",
        "talentify",
        "dice",
        "ziprecruiter",
        "indeed",
        "linkedin",
        "talentify io",
    }
)

_EXTRA_AGENCIES: frozenset[str] = frozenset()


def set_extra_agencies(names: Iterable[str]) -> None:
    """Replace the configured agency names (keyed like `company_key`)."""
    global _EXTRA_AGENCIES
    _EXTRA_AGENCIES = frozenset(k for k in (co.company_key(n) for n in names) if k)


def known_agencies() -> frozenset[str]:
    return AGGREGATOR_COMPANIES | _EXTRA_AGENCIES


# Trailing-noun heuristic: if a company_key's LAST token is one of these, the
# name almost certainly names a staffing/recruiting function rather than an
# employer ("XYZ Staffing", "Acme Recruiters", "Acme Talent"). Deliberately
# conservative — matched as a whole token, never a substring, so "Fabrikam
# Research Associates" (ends in "associates") and "Talent Systems Inc" (ends
# in "systems", "talent" leads rather than trails) both stay False.
#
# "search" is deliberately excluded even though it would catch the
# search-firm adapter's own output ("Acme Executive Search (retained
# search)", "Globex Retained Search (retained search)", and a pre-existing
# DB row like "Contoso Dining Search"). Those company_keys already end in
# "search" (company_key does not strip parentheticals), so adding it would
# flip is_agency for those rows too — and closure.py short-circuits an
# is_agency row straight to "unverifiable" with no employer-link search,
# which is a real behavior change for rows already in the DB. That change
# may well be correct (a retained-search listing has no named employer
# either), but it wants a deliberate decision by the user, not a side effect
# of widening this set. Revisit explicitly if they want search firms treated
# as agencies for closure/clustering purposes.
_TRAILING_AGENCY_NOUNS: frozenset[str] = frozenset(
    {
        "staffing",
        "staff",
        "recruiting",
        "recruiters",
        "recruitment",
        "personnel",
        "placement",
        "placements",
        "talent",
    }
)


def is_agency(company: str) -> bool:
    """True if `company` is a job aggregator or a staffing/recruiting agency —
    not the actual employer. Matches the bare name ("Acme Talent Partners") as
    well as a longer variant that still starts with it ("Acme Talent Partners
    Government Solutions"), so a subsidiary/division naming doesn't dodge the
    flag. Also catches an unlisted staffing shop whose name ends in an agency
    noun (see `_TRAILING_AGENCY_NOUNS`)."""
    key = co.company_key(company)
    if not key:
        return False
    agencies = known_agencies()
    if key in agencies:
        return True
    if any(key.startswith(f"{agency} ") for agency in agencies):
        return True
    tokens = key.split()
    return len(tokens) >= 2 and tokens[-1] in _TRAILING_AGENCY_NOUNS

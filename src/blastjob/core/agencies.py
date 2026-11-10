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

# What the search-firm adapter appends to a firm's name (`discovery/searchfirms.py`).
RETAINED_SUFFIX = " (retained search)"

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
# "search" is deliberately NOT a trailing noun: an employer named "Contoso Dining Search"
# stays an employer. The search-firm adapter's own rows are caught by `RETAINED_SUFFIX`
# instead, the literal suffix the adapter writes (ADR 0072), which also makes closure give
# them "unverifiable" without an employer-link search.
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
    noun (see `_TRAILING_AGENCY_NOUNS`) and a search-firm listing (`RETAINED_SUFFIX`)."""
    if (company or "").strip().lower().endswith(RETAINED_SUFFIX):
        return True
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

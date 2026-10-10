"""Shared fictional staffing-agency / aggregator fixture.

Agency and reseller names are a setting (`discovery.agency_companies`, see
`core/agencies.py`); only the big job platforms are built in. Tests that need
"this posting is from an agency" configure these invented names.

None of them ends in a trailing agency noun ("Staffing", "Talent", ...), so
each one is an agency ONLY because it is configured here, never through the
`is_agency` name heuristic.

Set both: `install_agencies()` for code that calls `is_agency` without loading
a config, and `apply_agencies(cfg)` for anything that goes through
`create_app` / a config load (which re-installs the configured list).
"""

from __future__ import annotations

from blastjob.core import agencies
from blastjob.models.config import BlastJobConfig

STAFFING_AGENCY = "Talonridge Global"
SECOND_AGENCY = "Corvin Hale"
AGGREGATOR = "Swiftlane"
COUNTRY_ONLY_AGGREGATOR = "Jobwharf"
AGENCIES = (STAFFING_AGENCY, SECOND_AGENCY, AGGREGATOR, COUNTRY_ONLY_AGGREGATOR)


def install_agencies() -> None:
    agencies.set_extra_agencies(AGENCIES)


def apply_agencies(cfg: BlastJobConfig) -> BlastJobConfig:
    """Set `cfg.discovery.agency_companies` in place; returns `cfg`."""
    cfg.discovery.agency_companies = list(AGENCIES)
    return cfg

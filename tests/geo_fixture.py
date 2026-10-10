"""Shared fictional-region fixture for geo-aware tests.

Real town/centroid data is now 100% user-configured (see
``models/config.py::DiscoveryConfig``) -- no production town table ships with
the app anymore. Every test that exercises the geo filter, same-role
clustering, or closure's same-metro proof therefore needs SOME towns
configured. Using a fictional region keeps that configuration out of the
owner's real geography (which no longer exists in the codebase at all) while
still exercising real US-state logic: ``OH`` is a real state, only the towns
in it are invented.

Never use ``DiscoveryConfig()``'s bare defaults (``home_state=""``,
``towns={}``) in a test that expects a real pass/fail/queue geo verdict --
that combination means "geo not configured" by design (see matching/geo.py),
so every non-remote job comes back UNKNOWN.
"""

from __future__ import annotations

from blastjob.models.config import BlastJobConfig, DiscoveryConfig

HOME_STATE = "OH"
HOME_LOCATION = "Harrowgate, OH"
HOME_LAT = 39.92
HOME_LON = -83.00

# name -> [lat, lon]. Approximate distances from HOME_LAT/HOME_LON, against
# DiscoveryConfig's shipped defaults (commute_radius_miles=35,
# extended_radius_miles=55):
#   harrowgate  ~ 0 mi   the home town itself
#   fairhaven   ~ 3 mi   in commute radius
#   oakbrook    ~ 3 mi   in commute radius; ~6 mi from fairhaven -- same
#                        metro by centroid PROXIMITY, not by matching city
#                        string (the two tests that need that distinction)
#   elmcrest    ~45 mi   beyond commute, inside extended -> conditional pass
#   rivermont   ~69 mi   beyond extended -> too far
#   jackson     ~ 3 mi   deliberately shares a name with a real town in
#                        another state (Jackson, MS), for the "an explicit
#                        other-state token beats a bare town-name match"
#                        precedence case
#   west fairhaven ~4 mi just outside fairhaven itself -- a different city
#                        string, same metro by proximity (same-role
#                        clustering tests)
TOWNS: dict[str, list[float]] = {
    "harrowgate": [39.92, -83.00],
    "fairhaven": [39.95, -83.05],
    "west fairhaven": [39.94, -83.06],
    "oakbrook": [39.90, -82.95],
    "elmcrest": [40.572, -83.00],
    "rivermont": [40.92, -83.00],
    "jackson": [39.93, -83.02],
}

# A home-state town the owner never got around to listing: exercises the
# "home state but unlisted town -> UNKNOWN" path. Deliberately absent from
# TOWNS above.
UNLISTED_HOME_TOWN = "cedarbrook"


def region_cfg() -> DiscoveryConfig:
    """A DiscoveryConfig with the fictional region fully configured."""
    return DiscoveryConfig(
        home_location=HOME_LOCATION,
        home_state=HOME_STATE,
        home_lat=HOME_LAT,
        home_lon=HOME_LON,
        towns=dict(TOWNS),
    )


def apply_region(cfg: BlastJobConfig) -> BlastJobConfig:
    """Mutate `cfg.discovery`'s geo fields in place, for fixtures that build a
    whole `BlastJobConfig` and need the region layered on rather than
    replaced. Returns `cfg` for chaining."""
    d = cfg.discovery
    d.home_location = HOME_LOCATION
    d.home_state = HOME_STATE
    d.home_lat = HOME_LAT
    d.home_lon = HOME_LON
    d.towns = dict(TOWNS)
    return cfg

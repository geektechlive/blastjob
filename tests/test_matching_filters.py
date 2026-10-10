"""Tests for the keyless geo filter and the deterministic scope prefilter."""

from __future__ import annotations

import pytest

import tests.geo_fixture as geo_fixture
from blastjob.matching.geo import assess_geo, haversine_miles
from blastjob.matching.prefilter import classify_scope
from blastjob.models.config import DiscoveryConfig
from blastjob.models.job import GeoStatus

# ---------- geo ----------


def test_haversine_known_distance():
    # two points ~30 miles apart (same figure the fixture's home/elmcrest pair
    # is built from) -- this test is pure trigonometry, no config involved.
    d = haversine_miles(39.92, -83.00, 40.35, -83.00)
    assert 28 <= d <= 34


def test_remote_passes_verified():
    cfg = DiscoveryConfig()
    status, dist, passes, cond = assess_geo(
        remote=True, city="", region="", location_raw="Remote - US", cfg=cfg
    )
    assert passes and status == GeoStatus.VERIFIED


def test_geo_unconfigured_non_remote_is_unknown_not_rejected():
    """home_state == "" and towns == {} -- the shipped default -- means the
    geo filter has nothing to measure against. A non-remote job must go to
    the review queue, never be hard-rejected on an empty config."""
    cfg = DiscoveryConfig()
    assert cfg.home_state == "" and not cfg.towns
    status, dist, passes, cond = assess_geo(
        remote=False, city="Springfield", region="OH", location_raw="Springfield, OH", cfg=cfg
    )
    assert status == GeoStatus.UNKNOWN and passes and not cond


def test_geo_unconfigured_remote_still_passes():
    cfg = DiscoveryConfig()
    status, dist, passes, cond = assess_geo(
        remote=True, city="", region="", location_raw="Remote", cfg=cfg
    )
    assert passes and status == GeoStatus.VERIFIED


def test_other_state_onsite_is_far():
    cfg = geo_fixture.region_cfg()
    status, dist, passes, cond = assess_geo(
        remote=False, city="New York", region="NY", location_raw="New York, NY", cfg=cfg
    )
    assert not passes and status == GeoStatus.VERIFIED


def test_configured_town_within_radius_passes():
    cfg = geo_fixture.region_cfg()
    status, dist, passes, cond = assess_geo(
        remote=False, city="Fairhaven", region="OH", location_raw="Fairhaven, OH", cfg=cfg
    )
    assert passes and status == GeoStatus.APPROXIMATE and dist is not None and dist < 35


def test_extended_zone_is_conditional_pass():
    cfg = geo_fixture.region_cfg()  # elmcrest is ~45mi: beyond commute, inside extended
    status, dist, passes, cond = assess_geo(
        remote=False, city="Elmcrest", region="OH", location_raw="Elmcrest, OH", cfg=cfg
    )
    # beyond normal radius but within extended -> passes but conditional
    assert passes and cond and status == GeoStatus.APPROXIMATE


def test_beyond_extended_radius_fails():
    cfg = geo_fixture.region_cfg()
    cfg.commute_radius_miles = 1.0
    cfg.extended_radius_miles = 2.0  # Fairhaven (~3mi) is now beyond both
    status, dist, passes, cond = assess_geo(
        remote=False, city="Fairhaven", region="OH", location_raw="Fairhaven, OH", cfg=cfg
    )
    assert not passes and not cond


def test_home_state_but_unlisted_town_goes_to_review_not_dropped():
    """A home-state town the owner never configured -- not the same as geo
    being unconfigured -- also lands in UNKNOWN, never a guess."""
    cfg = geo_fixture.region_cfg()
    town = geo_fixture.UNLISTED_HOME_TOWN
    assert town not in cfg.towns
    status, dist, passes, cond = assess_geo(
        remote=False, city=town.title(), region="OH", location_raw=f"{town.title()}, OH", cfg=cfg
    )
    assert status == GeoStatus.UNKNOWN and passes  # provisional pass -> review queue


# ---------- scope prefilter ----------
#
# These exercise the config-driven prefilter: `title_keep_terms`/`title_drop_terms`
# are whatever a candidate fills into `[discovery]`, not a hard-coded persona. The
# fixture terms below are a stand-in for one hypothetical configuration, not a
# default the app ships with.

_KEEP_TERMS = ["director", "head of"]
_DROP_TERMS = ["engineer", "nurse", "accountant", "representative"]


@pytest.mark.parametrize(
    "title",
    [
        "Director of Operations",
        "Head of Data Platform",
        "Regional Director",
        "Director, Customer Success",
    ],
)
def test_configured_keep_titles_are_kept(title):
    keep, reason = classify_scope(title, "", _KEEP_TERMS, _DROP_TERMS)
    assert keep, reason


@pytest.mark.parametrize(
    "title",
    ["Field Engineer", "Registered Nurse", "Staff Accountant", "Sales Representative"],
)
def test_configured_drop_titles_are_dropped_without_scope(title):
    keep, reason = classify_scope(title, "Build features and write code.", _KEEP_TERMS, _DROP_TERMS)
    assert not keep, reason


def test_configured_drop_title_with_management_scope_is_rescued():
    keep, reason = classify_scope(
        "Field Engineer",  # a configured drop-family term...
        "You will lead a team of 8 engineers and own the budget and roadmap.",
        _KEEP_TERMS,
        _DROP_TERMS,
    )
    assert keep  # ...but the JD shows the target management scope


def test_title_in_neither_list_is_ambiguous_kept():
    # No configured keep term, no configured drop term -> ambiguous -> kept for the LLM.
    keep, reason = classify_scope(
        "Business Transformation Lead", "Own the transformation roadmap.", _KEEP_TERMS, _DROP_TERMS
    )
    assert keep and "ambiguous" in reason


def test_plain_manager_is_ambiguous_kept():
    # 'manager' is deliberately in neither configured list here -> ambiguous -> kept.
    keep, reason = classify_scope(
        "Operations Manager", "Manage daily operations.", _KEEP_TERMS, _DROP_TERMS
    )
    assert keep


def test_empty_term_lists_keep_everything():
    # An unconfigured install (both lists empty, the default) must never drop a
    # role -- it just defers every title to the LLM.
    keep, reason = classify_scope("Field Engineer", "Build features and write code.")
    assert keep and "ambiguous" in reason


def test_empty_term_lists_keep_a_drop_shaped_title_even_with_no_scope_signal():
    keep, _reason = classify_scope("Staff Accountant", "No people-management language here.")
    assert keep

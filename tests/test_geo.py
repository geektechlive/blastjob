"""Three-way geo classification: known-home / known-other / can't-tell.

Regression cover for the bug that hid in-radius roles for the life of the
project: the old home-state check split `location_raw` on whitespace, so a
board writing a state+city as one dash-joined token (e.g.
``USA-OH-Fairhaven``) read as "not home" and was hard-rejected before the
centroid lookup -- permanently, since an unscored job is re-selected and
re-rejected on every run.

Every case here deliberately passes ``region=""``. The pre-existing geo tests
in tests/test_matching_filters.py all pass a real ``region``, which
short-circuits the state check on the first line and is exactly why the bug
survived.

The predicate under test is the one `matching/pipeline.py` actually branches
on:
    drop = (not passes) and status != GeoStatus.UNKNOWN
so each case asserts pass/drop, not just the status label.
"""

from __future__ import annotations

import pytest

import tests.geo_fixture as geo_fixture
from blastjob.matching.geo import assess_geo
from blastjob.models.config import DiscoveryConfig
from blastjob.models.job import GeoStatus


def _assess(location_raw: str, *, city: str = "", region: str = "", remote: bool = False, cfg=None):
    cfg = cfg if cfg is not None else geo_fixture.region_cfg()
    return assess_geo(remote=remote, city=city, region=region, location_raw=location_raw, cfg=cfg)


def _is_dropped(status: GeoStatus, passes: bool) -> bool:
    """Exactly the pipeline's pre-LLM drop test (pipeline.py `if not passes and ...`)."""
    return (not passes) and status != GeoStatus.UNKNOWN


# --------------------------------------------------------------------------
# 1. known-home -> must survive to scoring (never dropped)
# --------------------------------------------------------------------------

# (location_raw, city_field) — city mirrors location_raw the way the ATS
# adapters actually store it for these boards.
KNOWN_HOME = [
    ("USA-OH-Fairhaven", "USA-OH-Fairhaven"),
    ("USA-OH-Corporate Headquarters", "USA-OH-Corporate Headquarters"),
    ("Greater Fairhaven area", "Greater Fairhaven area"),
    ("AAO Fairhaven - 123 W Virginia St", "AAO Fairhaven - 123 W Virginia St"),
    ("Fairhaven, OH", "Fairhaven"),
    ("Oakbrook, OH", "Oakbrook"),
    ("Fairhaven, OH Corporate", "Fairhaven"),
    ("Oakbrook, OH - USA", "Oakbrook"),
    ("Fairhaven-Ohio-United States of America", "Fairhaven-Ohio-United States"),
    ("Elmcrest, Ohio", "Elmcrest"),
]


@pytest.mark.parametrize("raw,city", KNOWN_HOME, ids=[c[0] for c in KNOWN_HOME])
def test_known_home_is_never_dropped(raw, city):
    status, dist, passes, cond = _assess(raw, city=city)
    assert passes, f"{raw!r} must reach scoring"
    assert not _is_dropped(status, passes), f"{raw!r} was hard-rejected by the geo gate"


PLACEABLE_HOME = [
    ("USA-OH-Fairhaven", "USA-OH-Fairhaven"),
    ("Greater Fairhaven area", "Greater Fairhaven area"),
    ("AAO Fairhaven - 123 W Virginia St", "AAO Fairhaven - 123 W Virginia St"),
    ("Fairhaven, OH", "Fairhaven"),
    ("Oakbrook, OH", "Oakbrook"),
]


@pytest.mark.parametrize("raw,city", PLACEABLE_HOME, ids=[c[0] for c in PLACEABLE_HOME])
def test_known_home_city_is_placed_and_in_radius(raw, city):
    """Not just "not dropped" — these carry a real configured town name, so
    the centroid lookup must resolve them and report an in-commute distance."""
    status, dist, passes, cond = _assess(raw, city=city)
    assert status == GeoStatus.APPROXIMATE and dist is not None
    assert dist < 35 and passes and not cond


# --------------------------------------------------------------------------
# 2. known-other -> hard reject (an affirmative other-place signal)
# --------------------------------------------------------------------------

KNOWN_OTHER = [
    "Neuhausen-Schaffhausen-Switzerland",
    "Katowice, Poland",
    "Wilmington-North Carolina-United States",
    "Remote, Germany",
    "Remote-California",
    "Charlotte, NC 27409",
    "Gilbert-Arizona-United States of America",
    "Toronto, Canada",
    "MN - Minneapolis",
    "Example Illinois Medical Center - 200 W Wellington Ave",
]


@pytest.mark.parametrize("raw", KNOWN_OTHER)
def test_known_other_is_hard_rejected(raw):
    status, dist, passes, cond = _assess(raw, city=raw)
    assert _is_dropped(status, passes), f"{raw!r} should be a hard geo reject"
    assert status == GeoStatus.VERIFIED


# --------------------------------------------------------------------------
# 3. can't tell -> UNKNOWN -> review queue (geo.py's own docstring invariant)
# --------------------------------------------------------------------------

CANT_TELL = [
    "5 Locations",  # Workday multi-location placeholder
    "2 Locations",
    "Multiple Locations",
    "Fabrikam Retail Corporate Offices (0900)",
    "Example Regional Medical Center - 1200 W Oklahoma Ave",  # street name, not a state
    "Example Medical Center Washington County - 1400 E Sumner St",  # county, not a state
    "Example Medical Center - 500 S Fairhaven Ave",  # street name, not the city
    "United States Virtual",
    "Airside Center of Excellence",
    "Heanor",
]


@pytest.mark.parametrize("raw", CANT_TELL)
def test_unparseable_location_goes_to_review_not_dropped(raw):
    status, dist, passes, cond = _assess(raw, city=raw)
    assert status == GeoStatus.UNKNOWN, f"{raw!r} is not placeable — must not be asserted on"
    assert passes and not _is_dropped(status, passes)


# --------------------------------------------------------------------------
# precedence + guards
# --------------------------------------------------------------------------


def test_other_state_token_beats_a_town_name_that_is_also_elsewhere():
    """'Jackson' is in the configured town table (a home-area Jackson) and is
    also a real city in Mississippi. An explicit other-state token must win
    over the bare town-name scan."""
    status, _d, passes, _c = _assess("Jackson, MS", city="Jackson")
    assert _is_dropped(status, passes)


def test_explicit_home_state_token_wins_over_a_state_word_in_a_street_name():
    status, _d, passes, _c = _assess("USA-OH-123 W Virginia St", city="USA-OH-123 W Virginia St")
    assert passes and not _is_dropped(status, passes)


def test_lowercase_two_letter_words_are_not_state_abbreviations():
    """'in', 'or', 'me' are English words. Only an UPPERCASE 2-letter token is a
    state abbreviation — otherwise 'Positions in Technology' reads as Indiana."""
    status, _d, passes, _c = _assess("Openings in Technology", city="Openings in Technology")
    assert status == GeoStatus.UNKNOWN and passes


def test_remote_still_short_circuits():
    status, dist, passes, cond = _assess("Remote - US", remote=True)
    assert passes and status == GeoStatus.VERIFIED and dist is None


def test_empty_location_is_unknown_not_rejected():
    status, dist, passes, cond = _assess("")
    assert status == GeoStatus.UNKNOWN and passes


def test_geo_unconfigured_is_unknown_never_rejected():
    """home_state == "" and towns == {} (the shipped default): nothing to
    reject against, so even an explicit other-state mention must not be
    turned into a hard reject -- there is no "home" to be far from."""
    cfg = DiscoveryConfig()
    status, dist, passes, cond = _assess("New York, NY", city="New York", region="NY", cfg=cfg)
    assert status == GeoStatus.UNKNOWN and passes and not _is_dropped(status, passes)


# --------------------------------------------------------------------------
# pipeline integration: the drop happens in run_matching, so prove it there too
# --------------------------------------------------------------------------


async def test_workday_home_job_reaches_the_scorer(tmp_path, monkeypatch):
    """End-to-end guard on the actual casualty: a role whose only location
    string is Workday's dash-joined ``USA-OH-Fairhaven`` must be scored, not
    reset to 'new' unscored (which would re-select and re-reject it every
    run)."""
    import tempfile

    import tests.profile_fixture as profile_fixture
    from blastjob import config as cfg_mod
    from blastjob.db import client as dbc
    from blastjob.llm.cost import CostTracker
    from blastjob.matching import pipeline as mp
    from blastjob.matching.scorer import ScoringResult
    from blastjob.models.config import BlastJobConfig

    cfg = BlastJobConfig()
    cfg.paths.data_dir = tempfile.mkdtemp()
    geo_fixture.apply_region(cfg)
    profile_fixture.apply_profile(cfg)
    cfg_mod.data_dir(cfg).mkdir(parents=True, exist_ok=True)
    (cfg_mod.data_dir(cfg) / "work_history.md").write_text(
        "Senior leadership, 25y.", encoding="utf-8"
    )
    conn = dbc.init_db(cfg)

    jd = "VP of Operations. Lead a team of 20+, own enterprise strategy, report to CEO."
    home_id = dbc.upsert_job(
        conn,
        source="workday:northfieldretail",
        external_id="434",
        title="VP Regional Services - Central",
        company="Northfield Retail",
        raw_jd=jd,
        geo_city="USA-OH-Fairhaven",
        geo_location_raw="USA-OH-Fairhaven",
    )
    unsure_id = dbc.upsert_job(
        conn,
        source="workday:northfieldretail",
        external_id="999",
        title="VP, Operations",
        company="Northfield Retail",
        raw_jd=jd,
        geo_city="5 Locations",
        geo_location_raw="5 Locations",
    )
    far_id = dbc.upsert_job(
        conn,
        source="workday:acme",
        external_id="998",
        title="VP, Operations",
        company="Acme",
        raw_jd=jd,
        geo_city="Katowice",
        geo_location_raw="Katowice, Poland",
    )

    scored: list[int] = []

    async def fake_score(cfg_, wh, candidates, ct, **kw):
        scored.extend(c["job_id"] for c in candidates)
        return ScoringResult(
            scores={
                c["job_id"]: {
                    "alignment": 90,
                    "rationale": "stub",
                    "seniority_note": "",
                    "comp_min": 0,
                    "comp_max": 0,
                }
                for c in candidates
            }
        )

    monkeypatch.setattr(mp, "score_candidates", fake_score)
    res = await mp.run_matching(cfg, conn, CostTracker())

    assert home_id in scored, "USA-OH-Fairhaven was dropped before scoring"
    assert unsure_id in scored, "an unplaceable location must still be scored"
    assert far_id not in scored
    assert res.geo_rejected == 1 and res.geo_unknown == 1

    def state(jid):
        return conn.execute("SELECT review_state FROM jobs WHERE id=?", (jid,)).fetchone()[
            "review_state"
        ]

    assert state(home_id) == "surfaced"  # in radius, high alignment
    assert state(unsure_id) == "queued"  # location needs confirmation
    assert state(far_id) == "new"

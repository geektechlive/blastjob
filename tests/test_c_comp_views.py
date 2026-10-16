"""Compare's red flags and the funnel's comp bucket follow the gate's comp rule.

Only a USD (or unlabelled $) posted MAXIMUM is comparable to the floor. A
"From $150,000" posting stores max 0 (unknown) and a GBP range is not dollars,
so neither is "below floor" anywhere: the gate surfaces them, and the views
must not contradict it.
"""

from __future__ import annotations

import pytest

from blastjob.matching.gate import below_floor
from blastjob.models.config import BlastJobConfig
from blastjob.web import compare, funnel

FLOOR = 200_000


def _row(**over):
    base = {
        "company": "Example Widgets",
        "comp_provenance": "posted",
        "comp_max": 0,
        "comp_currency": "USD",
        "geo_location_raw": "Milwaukee, WI",
        "geo_remote": 0,
    }
    return {**base, **over}


@pytest.mark.parametrize(
    "comp_max,currency,expected",
    [
        (150_000, "USD", True),
        (150_000, "", True),
        (0, "USD", False),
        (150_000, "GBP", False),
        (250_000, "USD", False),
    ],
)
def test_below_floor_is_the_gates_rule(comp_max, currency, expected):
    assert below_floor(comp_max, currency, FLOOR) is expected


def test_compare_does_not_flag_a_min_only_posting():
    flags = compare.red_flags(_row(comp_max=0), None, FLOOR)
    assert "posted max below floor" not in flags


def test_compare_does_not_flag_a_non_usd_posting():
    flags = compare.red_flags(_row(comp_max=120_000, comp_currency="GBP"), None, FLOOR)
    assert "posted max below floor" not in flags


def test_compare_still_flags_a_usd_max_under_the_floor():
    assert "posted max below floor" in compare.red_flags(_row(comp_max=150_000), None, FLOOR)


def test_funnel_does_not_count_non_usd_posted_comp_as_a_comp_reject():
    cfg = BlastJobConfig()
    cfg.discovery.comp_floor_usd = FLOOR
    row = {
        "title": "Director of IT",
        "review_state": "new",
        "alignment_score": 80,
        "comp_provenance": "posted",
        "comp_max": 120_000,
        "comp_currency": "GBP",
    }
    assert funnel._post_scope_stage(row, cfg, "full jd " * 100) != "comp_rejected"

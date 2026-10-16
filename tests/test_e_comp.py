"""E1/E2: pay-string parsing must not invent magnitudes, mirrored bounds or currencies."""

from __future__ import annotations

import pytest

from blastjob.comp import parse_salary_range
from blastjob.matching.gate import decide
from blastjob.models.config import DiscoveryConfig
from blastjob.models.job import GeoStatus, ReviewState


def test_suffix_letter_of_the_next_word_is_not_a_multiplier():
    # "Max" used to be read as the M (million) suffix: 180000 -> 1.8e11.
    assert parse_salary_range("$180,000 Max") == (180_000, 180_000, "USD")
    assert parse_salary_range("$120,000 Minimum to $180,000 Maximum") == (
        120_000,
        180_000,
        "USD",
    )


def test_adjacent_suffix_still_scales():
    assert parse_salary_range("$211.4K – $290.6K") == (211_400, 290_600, "USD")
    assert parse_salary_range("$1.2M") == (1_200_000, 1_200_000, "USD")


def test_implausibly_large_amounts_are_ignored():
    # 5,000,000 is the same ceiling the scorer applies to model-read comp.
    assert parse_salary_range("Ref 98765432101 pays $150,000") == (150_000, 150_000, "USD")
    assert parse_salary_range("$98,765,432,101") == (0, 0, "USD")


@pytest.mark.parametrize(
    "text",
    ["From $150,000", "Starting at $150,000", "$150,000+", "$150K and up", "Minimum $150,000"],
)
def test_open_ended_pay_is_a_minimum_with_unknown_max(text):
    assert parse_salary_range(text) == (150_000, 0, "USD")


def test_a_real_range_with_from_is_still_a_range():
    assert parse_salary_range("From $150,000 to $190,000") == (150_000, 190_000, "USD")


def test_single_plain_figure_still_mirrors():
    assert parse_salary_range("$150,000") == (150_000, 150_000, "USD")


@pytest.mark.parametrize(
    ("text", "currency"),
    [
        ("£90,000 - £110,000", "GBP"),
        ("€90,000 - €110,000", "EUR"),
        ("CA$150,000", "CAD"),
        ("150,000 CAD", "CAD"),
        ("A$150,000", "AUD"),
        ("$150,000 - $190,000", "USD"),
    ],
)
def test_currency_is_detected(text, currency):
    assert parse_salary_range(text)[2] == currency


def _decide(**kw):
    base = dict(
        alignment=90,
        comp_provenance="posted",
        comp_max=100_000,
        geo_status=GeoStatus.VERIFIED,
        geo_passes=True,
        cfg=DiscoveryConfig(comp_floor_usd=200_000),
    )
    return decide(**{**base, **kw})


def test_posted_usd_below_floor_still_hard_rejects():
    assert _decide().review_state == ReviewState.NEW
    assert _decide(comp_currency="USD").review_state == ReviewState.NEW


def test_posted_non_usd_comp_never_hard_rejects_against_a_usd_floor():
    out = _decide(comp_currency="GBP")
    assert out.review_state == ReviewState.SURFACED


def test_unknown_max_never_hard_rejects():
    assert _decide(comp_max=0).review_state == ReviewState.SURFACED


def test_plus_must_touch_the_amount():
    assert parse_salary_range("$150,000 + bonus") == (150_000, 150_000, "USD")
    assert parse_salary_range("$150K or more") == (150_000, 0, "USD")

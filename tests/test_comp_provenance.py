"""Comp provenance must distinguish a board-published figure from a model's guess.

The gate is the place that costs money to get wrong: a sub-floor figure hard-rejects
the role. That is only a defensible reject when the number was actually POSTED. A
figure the LLM read out of a job description is INFERRED — it goes to the review
queue so a human can look, never to the bin.
"""

from __future__ import annotations

from blastjob.matching.gate import decide
from blastjob.models.config import DiscoveryConfig
from blastjob.models.job import CompProvenance, GeoStatus, ReviewState

CFG = DiscoveryConfig(comp_floor_usd=200_000)  # threshold 78 (default)


def _decide(alignment, provenance, comp_max, geo=GeoStatus.APPROXIMATE, passes=True):
    return decide(
        alignment=alignment,
        comp_provenance=provenance,
        comp_max=comp_max,
        geo_status=geo,
        geo_passes=passes,
        cfg=CFG,
    )


# ---------- the enum ----------


def test_inferred_is_a_distinct_provenance_value():
    assert CompProvenance.INFERRED.value == "inferred"


def test_existing_provenance_values_unchanged():
    # 780 live rows are 'unknown', 120 'estimated', 68 'posted' — never renumber.
    assert CompProvenance.POSTED.value == "posted"
    assert CompProvenance.ESTIMATED.value == "estimated"
    assert CompProvenance.UNKNOWN.value == "unknown"


def test_inferred_is_not_posted():
    assert CompProvenance.INFERRED.value != CompProvenance.POSTED.value


# ---------- the gate ----------


def test_inferred_sub_floor_queues_rather_than_rejects():
    # A model's reading of a JD said $150k. That is not a verified number, so it
    # may not bury a role that could really pay above the floor.
    out = _decide(95, CompProvenance.INFERRED.value, 150_000)
    assert out.review_state == ReviewState.QUEUED
    assert "below floor" not in out.reason or "inferred" in out.reason.lower()


def test_inferred_sub_floor_reason_names_the_inference():
    out = _decide(95, CompProvenance.INFERRED.value, 150_000)
    assert "inferred" in out.reason.lower()


def test_posted_sub_floor_still_hard_rejects():
    out = _decide(95, CompProvenance.POSTED.value, 150_000)
    assert out.review_state == ReviewState.NEW
    assert "below floor" in out.reason


def test_inferred_above_floor_surfaces_like_today():
    out = _decide(90, CompProvenance.INFERRED.value, 250_000)
    assert out.review_state == ReviewState.SURFACED


def test_inferred_sub_floor_below_alignment_band_is_still_not_surfaced():
    # Comp handling must not resurrect a role the alignment band already dropped.
    out = _decide(40, CompProvenance.INFERRED.value, 150_000)
    assert out.review_state == ReviewState.NEW
    assert "below band" in out.reason


def test_inferred_sub_floor_still_rejected_when_geo_fails():
    # Geo is the first hard reject and stays ahead of any comp consideration.
    out = _decide(95, CompProvenance.INFERRED.value, 150_000, geo=GeoStatus.VERIFIED, passes=False)
    assert out.review_state == ReviewState.NEW
    assert "commute" in out.reason


def test_unknown_comp_still_surfaces_badged():
    # The recorded policy (exec roles rarely post pay) is unchanged by this work.
    assert _decide(92, CompProvenance.UNKNOWN.value, 0).review_state == ReviewState.SURFACED


def test_estimated_comp_behaviour_unchanged():
    # 'estimated' is a level+geo model, not a JD read; this change does not touch it.
    assert _decide(92, CompProvenance.ESTIMATED.value, 150_000).review_state != ReviewState.NEW

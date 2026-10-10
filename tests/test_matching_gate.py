"""Tests for the gate/rank disposition and the scorer's defensive JSON parsing."""

from __future__ import annotations

from blastjob.matching.gate import decide
from blastjob.matching.scorer import parse_scores
from blastjob.models.config import DiscoveryConfig
from blastjob.models.job import GeoStatus, ReviewState

CFG = DiscoveryConfig(comp_floor_usd=200_000)  # threshold 78 (default)


def _decide(
    alignment, provenance="posted", comp_max=250000, geo=GeoStatus.APPROXIMATE, passes=True
):
    return decide(
        alignment=alignment,
        comp_provenance=provenance,
        comp_max=comp_max,
        geo_status=geo,
        geo_passes=passes,
        cfg=CFG,
    )


def test_strong_match_verified_comp_surfaces():
    assert _decide(90).review_state == ReviewState.SURFACED


def test_borderline_alignment_goes_to_queue():
    # 75 is within the 78 +/- 5 band -> visible in the review queue, not hidden
    assert _decide(75).review_state == ReviewState.QUEUED


def test_below_band_not_surfaced():
    assert _decide(70).review_state == ReviewState.NEW


def test_unknown_comp_surfaces_badged_not_queued():
    # Exec roles rarely post pay; unknown comp surfaces (badged), never quarantined.
    out = _decide(92, provenance="unknown", comp_max=0)
    assert out.review_state == ReviewState.SURFACED


def test_conditional_geo_routes_to_queue():
    # Extended-zone role: high alignment but must verify hybrid <=2 days.
    out = decide(
        alignment=92,
        comp_provenance="unknown",
        comp_max=0,
        geo_status=GeoStatus.APPROXIMATE,
        geo_passes=True,
        cfg=CFG,
        geo_conditional=True,
    )
    assert out.review_state == ReviewState.QUEUED
    assert "hybrid" in out.reason


def test_verified_comp_below_floor_hard_rejected_even_at_high_alignment():
    out = _decide(95, provenance="posted", comp_max=150000)
    assert out.review_state == ReviewState.NEW
    assert "below floor" in out.reason


def test_geo_far_hard_rejected():
    out = _decide(95, geo=GeoStatus.VERIFIED, passes=False)
    assert out.review_state == ReviewState.NEW
    assert "commute" in out.reason


def test_unknown_geo_routes_to_queue():
    out = _decide(90, geo=GeoStatus.UNKNOWN, passes=True)
    assert out.review_state == ReviewState.QUEUED


def test_threshold_78_band_boundaries():
    # Explicit threshold=78 config: band is 73..77 (threshold-BAND .. threshold-1).
    cfg = DiscoveryConfig(alignment_threshold=78)

    def _at(alignment):
        return decide(
            alignment=alignment,
            comp_provenance="posted",
            comp_max=250000,
            geo_status=GeoStatus.APPROXIMATE,
            geo_passes=True,
            cfg=cfg,
        )

    assert _at(78).review_state == ReviewState.SURFACED
    assert _at(77).review_state == ReviewState.QUEUED
    assert _at(73).review_state == ReviewState.QUEUED
    assert _at(72).review_state == ReviewState.NEW


# ---------- scorer parse: defensive against junk + injection ----------


def test_parse_scores_extracts_valid_ids_only():
    text = '[{"job_id": 1, "alignment": 88, "rationale": "good"}, {"job_id": 99, "alignment": 100}]'
    out = parse_scores(text, valid_ids={1, 2})
    assert set(out) == {1}  # id 99 wasn't in the batch -> ignored
    assert out[1]["alignment"] == 88


def test_parse_scores_clamps_and_drops_extra_fields():
    text = '[{"job_id": 2, "alignment": 250, "rationale": "x", "evil": "DROP TABLE"}]'
    out = parse_scores(text, valid_ids={2})
    assert out[2]["alignment"] == 100  # clamped
    assert "evil" not in out[2]  # only whitelisted keys survive


def test_parse_scores_tolerates_prose_around_array():
    text = 'Here you go:\n[{"job_id": 1, "alignment": 77}]\nHope that helps!'
    out = parse_scores(text, valid_ids={1})
    assert out[1]["alignment"] == 77


def test_parse_scores_accepts_bare_object():
    # The CLI sometimes returns a single object, not a 1-element array.
    out = parse_scores('{"job_id": 1, "alignment": 90}', valid_ids={1})
    assert out[1]["alignment"] == 90


def test_parse_scores_strips_code_fences():
    text = '```json\n[{"job_id": 1, "alignment": 84}]\n```'
    out = parse_scores(text, valid_ids={1})
    assert out[1]["alignment"] == 84


def test_parse_scores_raises_without_json():
    import pytest

    with pytest.raises(ValueError):
        parse_scores("no json here", valid_ids={1})

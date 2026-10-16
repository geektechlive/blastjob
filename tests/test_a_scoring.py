"""Group A, A4: a model `null` must not drop the whole fit score."""

from __future__ import annotations

import json

from blastjob.core.scoring import parse_score
from blastjob.models.fit_score import ClaimCheck, FitScore

HISTORY = "Led team of 8 engineers to deliver the payments platform."


def _score_json(claims) -> str:
    return json.dumps(
        {
            "overall_score": 80,
            "jd_alignment_score": 75,
            "groundedness_score": 90,
            "summary": "ok",
            "claims": claims,
        }
    )


def test_null_evidence_quote_is_an_empty_quote_not_a_dropped_score():
    claim = ClaimCheck.model_validate(
        {"claim_text": "Led team", "grounded": False, "evidence_quote": None}
    )

    assert claim.evidence_quote == ""


def test_parse_score_survives_a_null_evidence_quote_and_marks_the_claim_ungrounded():
    claims = [
        {
            "claim_text": "Invented",
            "grounded": True,
            "evidence_quote": None,
            "source_section": None,
        },
        {"claim_text": "Led team", "grounded": True, "evidence_quote": "Led team of 8 engineers"},
    ]

    score = parse_score(_score_json(claims), HISTORY)

    assert [c.grounded for c in score.claims] == [False, True]
    assert score.unsupported_count == 1


def test_other_null_fields_fall_back_to_their_defaults():
    score = FitScore.model_validate(
        {"overall_score": None, "summary": None, "claims": None, "undated_employers": None}
    )

    assert score.overall_score == 0
    assert score.summary == ""
    assert score.claims == []
    assert score.undated_employers == []


def test_a_null_grounded_flag_is_not_grounded():
    claim = ClaimCheck.model_validate({"claim_text": "x", "grounded": None})

    assert claim.grounded is False

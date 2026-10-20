"""JSON extraction from model output must survive bracketed prose around the payload."""

from __future__ import annotations

import json

import pytest

from blastjob.core.scoring import parse_score
from blastjob.llm.jsonio import extract_json_list

ERR = "no json"


def test_array_survives_bracketed_prose_before_and_after():
    text = 'Scores [see below]: [{"a": 1}, {"a": 2}] and a footnote [1].'

    assert extract_json_list(text, error_message=ERR) == [{"a": 1}, {"a": 2}]


def test_single_object_survives_braced_prose_around_it():
    text = 'Note {not json} then {"a": 1} and {also not}.'

    assert extract_json_list(text, error_message=ERR) == [{"a": 1}]


def test_object_fallback_runs_when_the_brackets_are_not_json():
    text = 'Result [draft] is {"a": 1}'

    assert extract_json_list(text, error_message=ERR) == [{"a": 1}]


def test_no_json_raises_the_callers_message():
    with pytest.raises(ValueError, match=ERR):
        extract_json_list("nothing [here] {at all}", error_message=ERR)


def test_parse_score_survives_braced_prose_around_the_object():
    payload = json.dumps(
        {"overall_score": 80, "jd_alignment_score": 75, "summary": "ok", "claims": []}
    )

    score = parse_score(f"Here {{is}} the score: {payload}\nDone {{end}}", "history")

    assert score.jd_alignment_score == 75


def test_parse_score_without_an_object_keeps_its_error():
    with pytest.raises(ValueError, match="No JSON object found in scoring response"):
        parse_score("no braces {here} at all", "history")

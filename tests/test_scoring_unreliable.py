"""#106: a fit-score run whose verdicts contradict the work history is marked
unreliable, never shown as a low score and never as an all-clear.

Live case (invented here): the scorer's summary said every claim traced to the
work history, yet it marked most claims unsupported, each one verbatim from the
history. Groundedness fell from 100 to 43 on an unchanged resume. A check that
did not run correctly is reported as "not checked, re-score", like a scorer that
did not run at all. No claim is ever flipped to grounded by this guard.
"""

from __future__ import annotations

import json
from pathlib import Path

from blastjob.core import build as core_build
from blastjob.core import packet_gate, packet_verify, scoring
from blastjob.models.fit_score import FitScore
from blastjob.web.fitscore import load_fit_score

HISTORY = """\
# Work History

## Acme Corp | Springfield, OH | Jan 2018 - Dec 2023

### Director of IT Operations

- Built a 24x7 service desk for revenue-generating applications with 99.9% uptime.
- Moved all manual database deployments to automated pipelines, cutting failures 80%.
- Negotiated multi-year vendor contracts that saved $400K in the first year.
- Migrated the data center to two public clouds with least-privilege access.
- Replaced aging storage with a 60TB hyperconverged cluster.
- Segmented a flat network into five zones behind next-generation firewalls.
"""

VERBATIM = [
    "Built a 24x7 service desk for revenue-generating applications with 99.9% uptime.",
    "Moved all manual database deployments to automated pipelines, cutting failures 80%.",
    "Negotiated multi-year vendor contracts that saved $400K in the first year.",
    "Migrated the data center to two public clouds with least-privilege access.",
    "Replaced aging storage with a 60TB hyperconverged cluster.",
]


def _response(claims: list[dict], summary: str = "Every claim traces to the work history.") -> str:
    return json.dumps(
        {
            "overall_score": 90,
            "jd_alignment_score": 70,
            "groundedness_score": 100,
            "claims": claims,
            "unsupported_count": 0,
            "summary": summary,
        }
    )


def _ungrounded(text: str) -> dict:
    return {"claim_text": text, "grounded": False, "evidence_quote": "", "source_section": ""}


def _grounded(text: str) -> dict:
    return {"claim_text": text, "grounded": True, "evidence_quote": text, "source_section": "Acme"}


def _contradictory_score() -> FitScore:
    claims = [_ungrounded(f"- **{t}**") for t in VERBATIM] + [
        _grounded("Segmented a flat network into five zones behind next-generation firewalls.")
    ]
    return scoring.parse_score(_response(claims), HISTORY)


# --- the guard ---------------------------------------------------------------


def test_most_claims_unsupported_but_verbatim_marks_the_run_unreliable() -> None:
    score = _contradictory_score()
    assert score.unreliable is True
    assert "unreliable" in score.summary.lower()


def test_the_guard_never_flips_a_claim_to_grounded() -> None:
    score = _contradictory_score()
    assert sum(1 for c in score.claims if not c.grounded) == len(VERBATIM)


def test_unsupported_claims_that_are_not_in_the_history_stay_a_low_score() -> None:
    claims = [
        _ungrounded("Led a global team of 200 engineers across four continents."),
        _ungrounded("Owned a $50M budget and reported to the board."),
        _ungrounded("Drove a company-wide AI transformation program."),
        _grounded(VERBATIM[0]),
    ]
    score = scoring.parse_score(_response(claims), HISTORY)
    assert score.unreliable is False
    assert score.groundedness_score == 25


def test_a_minority_of_verbatim_misses_is_ordinary_noise_not_unreliable() -> None:
    claims = [_ungrounded(VERBATIM[0])] + [_grounded(t) for t in VERBATIM[1:]]
    score = scoring.parse_score(_response(claims), HISTORY)
    assert score.unreliable is False


def test_a_partial_word_match_is_not_verbatim() -> None:
    # "Built a 24x7 service desk for revenue-generating application" is a prefix
    # of the source line but stops mid-word: whole tokens only.
    claims = [
        _ungrounded("Built a 24x7 service desk for revenue-generating application"),
        _ungrounded("Negotiated multi-year vendor contract"),
        _ungrounded("Replaced aging storage with a 60TB hyperconverged clust"),
    ]
    score = scoring.parse_score(_response(claims), HISTORY)
    assert score.unreliable is False


def test_date_findings_do_not_count_toward_the_verbatim_test() -> None:
    claims = [_ungrounded("Acme Corp | Springfield, OH | Jan 2018 - Dec 2023")]
    claims[0]["source_section"] = "dates"
    claims += [_grounded(t) for t in VERBATIM]
    score = scoring.parse_score(_response(claims), HISTORY)
    assert score.unreliable is False


def test_the_report_says_unreliable_instead_of_a_score() -> None:
    md = scoring.render_score_md(_contradictory_score())
    assert "unreliable" in md.lower()
    assert "**Groundedness:** 16/100" not in md


def test_an_old_score_file_without_the_flag_reads_as_reliable() -> None:
    assert FitScore.model_validate({"groundedness_score": 90}).unreliable is False


# --- every consumer treats it as "not checked" -------------------------------


def test_the_packet_report_records_an_unreliable_run_as_not_checked(tmp_path: Path) -> None:
    report = core_build._verify_packet(
        resume_md="# Pat Example\n",
        cover_letter_md="",
        work_history_md=HISTORY,
        job_description="Director of IT",
        out_dir=tmp_path,
        resume_stem="Resume",
        fit_score=_contradictory_score(),
    )
    assert report.groundedness is None
    assert packet_verify.SKIPPED_RESUME_UNRELIABLE in report.checks_skipped
    assert not [f for f in report.findings if f.code == "unsupported_claim"]


def test_the_gate_explains_the_unreliable_run() -> None:
    label = packet_gate._SKIPPED_LABELS[packet_verify.SKIPPED_RESUME_UNRELIABLE]
    assert "unreliable" in label.lower()


def test_the_cockpit_shows_an_unreliable_run_as_not_scored(tmp_path: Path) -> None:
    (tmp_path / "fit_score.json").write_text(
        _contradictory_score().model_dump_json(), encoding="utf-8"
    )
    summary = load_fit_score(tmp_path)
    assert summary.scored is False
    assert summary.needs_review is False
    assert "unreliable" in summary.reason.lower()

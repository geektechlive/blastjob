"""Tests for core/prose_check.py — deterministic, advisory-only prose checks.

Uses synthetic packet-shaped samples (a cover letter with em dashes, a clean
one, a resume + job description sharing a role title, a work history) plus
crafted text for positive cases per rule. Nothing is read from disk.
"""

from __future__ import annotations

import pytest

from blastjob.core.packet_report import Artifact, Severity
from blastjob.core.prose_check import check_prose

# Packet-shaped samples for the "real output" checks: a generated cover letter
# that still carries em dashes, a clean one, and a resume / job description /
# work history that share a role title the echo check must not flag.
FAIRVIEW_COVER_LETTER = (
    "Dear Hiring Manager,\n\n"
    "Fairview Robotics is hiring a Chief Technology Officer \u2014 someone to own the "
    "platform roadmap \u2014 and that is the work I have done for nine years. At my current "
    "company I moved our warehouse software onto one stack and cut release time in half. "
    "I would welcome a conversation about your plans for the next product line.\n\n"
    "Sincerely,\nJordan Sample\n"
)
MERIDIAN_COVER_LETTER = (
    "Dear Hiring Manager,\n\n"
    "I run technology for a mid-sized manufacturer, and the job is rarely the one on the "
    "org chart. Most weeks it is getting three plants to agree on one way of working. "
    "Last year that meant replacing a scheduling system nobody trusted. It took eight "
    "months and the plants run on it today. I'd like to hear what you are trying to fix.\n\n"
    "Sincerely,\nJordan Sample\n"
)
FAIRVIEW_RESUME = (
    "# Jordan Sample\n\n"
    "## Chief Technology Officer, Northwind Logistics (2019-present)\n"
    "- Moved warehouse software onto one stack; release time fell from 6 weeks to 3.\n"
    "- Built a 40-person engineering group across two sites.\n"
    # Lifted from the job description, so the echo rule has something to find.
    "- Partners with the CEO on strategy and leads a growing engineering organization.\n\n"
    "## Director of Engineering, Contoso Freight (2012-2019)\n"
    "- Led the move from on-premise servers to a hosted platform.\n"
)
FAIRVIEW_JOB_DESCRIPTION = (
    "Fairview Robotics is hiring a Chief Technology Officer. The Chief Technology Officer "
    "owns the product platform, leads a growing engineering organization, and partners "
    "with the CEO on strategy. Experience scaling engineering teams across multiple sites "
    "is required.\n"
)
WORK_HISTORY = (
    "---\nname: Jordan Sample\n---\n\n"
    "## Northwind Logistics -- Chief Technology Officer (2019-present)\n"
    "- Moved warehouse software onto one stack; release time fell from 6 weeks to 3.\n"
    "- Built a 40-person engineering group across two sites.\n\n"
    "## Contoso Freight -- Director of Engineering (2012-2019)\n"
    "- Led the move from on-premise servers to a hosted platform.\n"
)

# A short, deliberately human-sounding cover-letter-style paragraph: varied
# sentence length (short punchy sentences mixed with longer ones), no
# em dashes, no cliches, no stock openers/closers, no stacked adjectives.
# Used both as a "clean" sample under test and as a work-history stand-in for
# the jd_phrase_echo exemption in tests that don't care about that rule.
CLEAN_HUMAN_SAMPLE = (
    "I lead technology strategy at a mid-sized manufacturer. It's harder than it sounds. "
    "Every acquisition brings its own stack and its own habits, and I have to make it all "
    "work together without slowing the business down. That's the job. At my current "
    "company I rebuilt a legacy network that hadn't been touched in a decade, and we "
    "finished with zero downtime. Before that, at a healthcare data company, I helped the "
    "team earn two major security certifications over about a year. I'd welcome the "
    "chance to talk about your roadmap."
)


@pytest.fixture(scope="module")
def fairview_cover_letter() -> str:
    return FAIRVIEW_COVER_LETTER


@pytest.fixture(scope="module")
def meridian_cover_letter() -> str:
    return MERIDIAN_COVER_LETTER


@pytest.fixture(scope="module")
def fairview_resume() -> str:
    return FAIRVIEW_RESUME


@pytest.fixture(scope="module")
def fairview_job_description() -> str:
    return FAIRVIEW_JOB_DESCRIPTION


@pytest.fixture(scope="module")
def work_history() -> str:
    return WORK_HISTORY


# ---------------------------------------------------------------------------
# Findings are always advisory (MINOR) and tagged to the right artifact.
# ---------------------------------------------------------------------------


def test_all_findings_are_minor_and_never_block() -> None:
    text = "I would like to delve into the details and showcase my meticulous approach."
    findings = check_prose(text, history=CLEAN_HUMAN_SAMPLE, kind="cover_letter")
    assert findings
    assert all(f.severity is Severity.MINOR for f in findings)


def test_resume_findings_tagged_resume_artifact() -> None:
    text = "I am a results-driven team player with a proven track record."
    findings = check_prose(text, history=CLEAN_HUMAN_SAMPLE, kind="resume")
    assert findings
    assert all(f.artifact is Artifact.RESUME for f in findings)


def test_cover_letter_findings_tagged_cover_letter_artifact() -> None:
    text = "I am a results-driven team player with a proven track record."
    findings = check_prose(text, history=CLEAN_HUMAN_SAMPLE, kind="cover_letter")
    assert findings
    assert all(f.artifact is Artifact.COVER_LETTER for f in findings)


# ---------------------------------------------------------------------------
# Clean human-sounding text should produce few or no findings.
# ---------------------------------------------------------------------------


def test_clean_human_sample_produces_no_findings_against_itself() -> None:
    findings = check_prose(CLEAN_HUMAN_SAMPLE, history=CLEAN_HUMAN_SAMPLE, kind="cover_letter")
    assert findings == []


def test_clean_synthetic_sample_is_quiet_with_a_packet_history(
    meridian_cover_letter: str,
) -> None:
    findings = check_prose(CLEAN_HUMAN_SAMPLE, history=meridian_cover_letter, kind="cover_letter")
    assert findings == []


def test_clean_human_sounding_letter_produces_few_findings() -> None:
    """A checker that fires constantly is noise, not signal — a realistic,
    human-sounding cover letter with a JD it genuinely doesn't mirror should
    stay quiet."""
    jd = "We are hiring a Director of Manufacturing Operations for our Midwest plants."
    findings = check_prose(CLEAN_HUMAN_SAMPLE, kind="cover_letter", job_description=jd)
    assert len(findings) <= 1


# ---------------------------------------------------------------------------
# Rule 1: llm_register_word (Kobak et al.) — kept separate from rule 2.
# ---------------------------------------------------------------------------


def test_llm_register_word_fires_on_kobak_vocabulary() -> None:
    text = "I would like to delve into the details and showcase my meticulous approach."
    findings = check_prose(text, history=CLEAN_HUMAN_SAMPLE, kind="cover_letter")
    codes = [f.code for f in findings]
    assert codes.count("llm_register_word") == 3


def test_llm_register_word_matches_inflections() -> None:
    text = "The results surpassed every target we set, underscoring the strategy."
    findings = check_prose(text, history=CLEAN_HUMAN_SAMPLE, kind="cover_letter")
    words = {f.evidence.lower() for f in findings if f.code == "llm_register_word"}
    assert "surpassed" in words
    assert "underscoring" in words


def test_llm_register_word_and_stale_cliche_are_separate_codes() -> None:
    text = "I want to delve into how I spearheaded the rollout."
    findings = check_prose(text, history=CLEAN_HUMAN_SAMPLE, kind="cover_letter")
    codes = {f.code for f in findings}
    assert "llm_register_word" in codes
    assert "stale_resume_cliche" in codes


# ---------------------------------------------------------------------------
# Rule 2: stale_resume_cliche — flagged only without a following figure/fact.
# ---------------------------------------------------------------------------


def test_stale_resume_cliche_fires_without_a_following_figure() -> None:
    text = "I am a results-driven team player with a proven track record."
    findings = [f for f in check_prose(text, history=CLEAN_HUMAN_SAMPLE, kind="resume")]
    codes = [f.code for f in findings]
    assert codes.count("stale_resume_cliche") == 3


def test_stale_resume_cliche_exempt_when_followed_by_a_figure() -> None:
    text = "I leveraged automation to cut costs by 40% across three teams."
    findings = check_prose(text, history=CLEAN_HUMAN_SAMPLE, kind="resume")
    assert "stale_resume_cliche" not in [f.code for f in findings]


def test_stale_resume_cliche_exempt_when_followed_by_a_named_fact() -> None:
    text = "I spearheaded the rollout of Fabrikam ServiceDesk across the organization."
    findings = check_prose(text, history=CLEAN_HUMAN_SAMPLE, kind="resume")
    assert "stale_resume_cliche" not in [f.code for f in findings]


# ---------------------------------------------------------------------------
# Rule 3: formulaic opening/closing (cover_letter only).
# ---------------------------------------------------------------------------


def test_formulaic_opening_and_closing_fire_on_stock_phrases() -> None:
    text = (
        "I am writing to express my interest in this role. "
        + CLEAN_HUMAN_SAMPLE
        + " Thank you for your time and consideration."
    )
    findings = check_prose(text, history=CLEAN_HUMAN_SAMPLE, kind="cover_letter")
    codes = {f.code for f in findings}
    assert "formulaic_opening" in codes
    assert "formulaic_closing" in codes


def test_formulaic_opening_and_closing_not_checked_for_resume() -> None:
    text = (
        "I am writing to express my interest in this role. "
        + CLEAN_HUMAN_SAMPLE
        + " Thank you for your time and consideration."
    )
    findings = check_prose(text, history=CLEAN_HUMAN_SAMPLE, kind="resume")
    codes = {f.code for f in findings}
    assert "formulaic_opening" not in codes
    assert "formulaic_closing" not in codes


# ---------------------------------------------------------------------------
# Rule 4: antithesis_overuse — more than 1 per document.
# ---------------------------------------------------------------------------


def test_antithesis_overuse_fires_above_the_cap() -> None:
    text = (
        "This role is not just about infrastructure, but about people. "
        "It's not about the technology, it's about the outcomes it enables."
    )
    findings = check_prose(text, history=CLEAN_HUMAN_SAMPLE, kind="cover_letter")
    assert "antithesis_overuse" in [f.code for f in findings]


def test_antithesis_single_use_does_not_fire() -> None:
    text = "This role is not just about infrastructure, but about people. " + CLEAN_HUMAN_SAMPLE
    findings = check_prose(text, history=CLEAN_HUMAN_SAMPLE, kind="cover_letter")
    assert "antithesis_overuse" not in [f.code for f in findings]


# ---------------------------------------------------------------------------
# Rule 5: tricolon_overuse — more than 2 three-item lists per document.
# ---------------------------------------------------------------------------


def test_tricolon_overuse_fires_above_the_cap() -> None:
    text = (
        "I bring strategy, execution, and discipline. I value speed, quality, and trust. "
        "I focus on people, process, and technology."
    )
    findings = check_prose(text, history=CLEAN_HUMAN_SAMPLE, kind="cover_letter")
    assert "tricolon_overuse" in [f.code for f in findings]


def test_two_tricolons_does_not_fire() -> None:
    text = (
        "I bring strategy, execution, and discipline. " + CLEAN_HUMAN_SAMPLE + " I value "
        "speed, quality, and trust."
    )
    findings = check_prose(text, history=CLEAN_HUMAN_SAMPLE, kind="cover_letter")
    assert "tricolon_overuse" not in [f.code for f in findings]


# ---------------------------------------------------------------------------
# Rule 6: em_dash (absolute) and adjective_stack (absolute, per-occurrence).
# ---------------------------------------------------------------------------


def test_em_dash_flags_every_occurrence() -> None:
    text = CLEAN_HUMAN_SAMPLE + " I rebuilt the network — end to end — without downtime."
    findings = check_prose(text, kind="cover_letter")
    assert [f.code for f in findings].count("em_dash") == 2


def test_em_dash_fires_with_no_history_supplied() -> None:
    text = "I rebuilt the network — end to end — without downtime."
    findings = check_prose(text, kind="cover_letter")
    assert "em_dash" in [f.code for f in findings]


def test_adjective_stack_flags_each_occurrence() -> None:
    text = "We delivered a robust, scalable, innovative, and comprehensive platform. " + (
        CLEAN_HUMAN_SAMPLE
    )
    findings = check_prose(text, kind="cover_letter")
    assert [f.code for f in findings].count("adjective_stack") == 4


def test_adjective_stack_exempts_an_occurrence_followed_by_a_figure() -> None:
    text = "We delivered a robust platform that cut latency by 40% across three regions."
    findings = check_prose(text, kind="cover_letter")
    assert "adjective_stack" not in [f.code for f in findings]


# ---------------------------------------------------------------------------
# Rule 7: jd_phrase_echo, with the proper-noun/credential/title exemption.
# ---------------------------------------------------------------------------


def test_jd_phrase_echo_fires_on_a_lifted_phrase() -> None:
    jd = "We need someone to modernize legacy environments across the business."
    text = "My whole career has been about how to modernize legacy environments at scale."
    findings = check_prose(text, history=CLEAN_HUMAN_SAMPLE, kind="resume", job_description=jd)
    assert "jd_phrase_echo" in [f.code for f in findings]


def test_jd_phrase_echo_does_not_trip_on_director_of_business_operations() -> None:
    """CRITICAL exemption: job titles must never trip this rule."""
    jd = "We seek a Director of Business Operations to lead our infrastructure team."
    text = "### Director of Business Operations, Northwind Logistics, Fairhaven, OH"
    findings = check_prose(text, history=CLEAN_HUMAN_SAMPLE, kind="resume", job_description=jd)
    echoes = [f for f in findings if f.code == "jd_phrase_echo"]
    assert echoes == []
    assert not any("director of business operations" in f.evidence.lower() for f in findings)


def test_jd_phrase_echo_exempts_phrases_also_present_in_history() -> None:
    """A phrase that is also in the candidate's own history isn't pure mirroring."""
    jd = "candidates should have run vendor contract renegotiations across multiple regions"
    history = "I ran vendor contract renegotiations across multiple regions on three deals."
    text = "In this role I ran vendor contract renegotiations across multiple regions."
    findings = check_prose(text, history=history, kind="resume", job_description=jd)
    assert "jd_phrase_echo" not in [f.code for f in findings]


def test_jd_phrase_echo_uses_the_work_history_as_its_exemption_list() -> None:
    """The exemption list is words the candidate legitimately uses (his work history),
    supplied via `history` — the same work-history text exempts the same
    lifted phrase whether the artifact under test is the resume or the
    cover letter, because it is the candidate's history, not the document
    under test, that decides the exemption."""
    jd = "candidates should have run vendor contract renegotiations across multiple regions"
    work_history_text = "I ran vendor contract renegotiations across multiple regions, three times."
    resume_text = "In this role I ran vendor contract renegotiations across multiple regions."
    letter_text = "For this role I ran vendor contract renegotiations across multiple regions."

    resume_findings = check_prose(
        resume_text, history=work_history_text, kind="resume", job_description=jd
    )
    letter_findings = check_prose(
        letter_text, history=work_history_text, kind="cover_letter", job_description=jd
    )

    assert "jd_phrase_echo" not in [f.code for f in resume_findings]
    assert "jd_phrase_echo" not in [f.code for f in letter_findings]

    # Without the work history supplied, the same phrase is flagged —
    # proving the exemption came from `history`, not from nothing firing.
    unexempted = check_prose(resume_text, kind="resume", job_description=jd)
    assert "jd_phrase_echo" in [f.code for f in unexempted]


def test_jd_phrase_echo_ignores_short_job_description() -> None:
    findings = check_prose(
        "some resume text with no overlap at all",
        history=CLEAN_HUMAN_SAMPLE,
        kind="resume",
        job_description="",
    )
    assert "jd_phrase_echo" not in [f.code for f in findings]


# ---------------------------------------------------------------------------
# Real-packet sanity checks: findings on real generated output are believable
# (not exhaustive assertions on exact counts, since those packets predate
# this checker and are expected to contain genuine defects).
# ---------------------------------------------------------------------------


def test_packet_cover_letter_flags_its_own_em_dashes(
    fairview_cover_letter: str,
) -> None:
    findings = check_prose(fairview_cover_letter, kind="cover_letter")
    assert "em_dash" in [f.code for f in findings]


def test_packet_cover_letter_runs_clean(
    meridian_cover_letter: str, fairview_cover_letter: str
) -> None:
    # Sanity: running the checker on real output never raises and always
    # returns MINOR-only findings, whatever it finds.
    findings = check_prose(
        meridian_cover_letter, history=fairview_cover_letter, kind="cover_letter"
    )
    assert all(f.severity is Severity.MINOR for f in findings)


def test_packet_resume_jd_echo_does_not_flag_role_titles(
    fairview_resume: str, fairview_job_description: str, work_history: str
) -> None:
    findings = check_prose(
        fairview_resume,
        history=work_history,
        kind="resume",
        job_description=fairview_job_description,
    )
    echoes = [f for f in findings if f.code == "jd_phrase_echo"]
    assert echoes, "the lifted line should be flagged, or this test proves nothing"
    for finding in echoes:
        assert "chief technology officer" not in finding.evidence.lower()

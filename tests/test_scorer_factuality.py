"""The groundedness report must not strengthen the claim it is checking.

A resume that said a certification was still in progress was restated by three
consecutive `fit_score.md` reports as though it were already earned. The
document whose whole job is verifying factuality was upgrading the credential.
Any restatement stronger than the work history's own words is itself an
unsupported claim.

These tests pin the prompt constraints. They assert on the system prompts rather
than on model output because no test may make a live LLM call.
"""

from __future__ import annotations

import pytest

from blastjob.core import scoring
from blastjob.llm import prompts


def test_fit_score_prompt_requires_credentials_to_be_quoted():
    low = prompts.FIT_SCORE_SYSTEM.lower()
    assert "credential" in low
    assert "quote" in low or "quoted" in low
    assert "paraphrase" in low


def test_fit_score_prompt_names_the_credential_offenders_explicitly():
    low = prompts.FIT_SCORE_SYSTEM.lower()
    assert "coursework toward a degree is not the degree" in low
    assert "acting or interim is not the permanent title" in low
    assert "certification in progress is not a certification" in low


def test_fit_score_prompt_treats_a_stronger_restatement_as_ungrounded():
    low = prompts.FIT_SCORE_SYSTEM.lower()
    assert "stronger" in low
    assert "grounded=false" in low


def test_scoring_still_uses_the_fit_score_prompt():
    assert scoring.prompts.FIT_SCORE_SYSTEM is prompts.FIT_SCORE_SYSTEM


@pytest.mark.parametrize(
    "prompt_name",
    ["RESUME_SYSTEM", "RESUME_ATS_SYSTEM", "COVER_LETTER_SYSTEM"],
)
def test_writing_prompts_allow_paraphrase_but_forbid_importing_nouns(prompt_name: str):
    text: str = getattr(prompts, prompt_name)
    low = text.lower()
    # Paraphrase is legitimate advocacy and must stay explicitly allowed.
    assert "paraphras" in low
    # A noun not in the work history may not enter the document.
    assert "noun" in low
    for example in ("compliance framework", "certification"):
        assert example in low, f"{prompt_name} should name {example} as a no-import category"


@pytest.mark.parametrize(
    "prompt_name",
    ["RESUME_SYSTEM", "RESUME_ATS_SYSTEM", "COVER_LETTER_SYSTEM"],
)
def test_writing_prompts_forbid_completion_tense_upgrades(prompt_name: str):
    text: str = getattr(prompts, prompt_name)
    low = text.lower()
    assert "completion-tense" in low or "completion tense" in low
    assert "began work to streamline onboarding" in low
    assert "started is not finished" in low


@pytest.mark.parametrize(
    "prompt_name",
    ["RESUME_SYSTEM", "RESUME_ATS_SYSTEM", "COVER_LETTER_SYSTEM"],
)
def test_writing_prompts_carry_the_credential_constraint(prompt_name: str):
    low: str = getattr(prompts, prompt_name).lower()
    assert "coursework toward a degree is not the degree" in low
    assert "acting or interim is not the permanent title" in low
    assert "certification in progress is not a certification" in low
    # The credential is keyed to the work history's own phrasing, not to a
    # stronger status word.
    assert "the way the work history writes it" in low


# --- constraints traced to a real packet that scored groundedness 62 -----------


WRITING_PROMPTS = ["RESUME_SYSTEM", "RESUME_ATS_SYSTEM", "COVER_LETTER_SYSTEM"]


@pytest.mark.parametrize("prompt_name", WRITING_PROMPTS)
def test_writing_prompts_name_technologies_and_metrics_as_no_import(prompt_name: str):
    """The history listed 'spreadsheet reporting, team dashboards'; the
    resume added 'Machine Learning Operations, Data Lakehouse (Iceberg, Trino,
    Airflow)'. Named technologies are nouns like any other."""
    low: str = getattr(prompts, prompt_name).lower()
    assert "named technolog" in low
    assert "metric" in low
    assert "machine learning operations" in low
    assert "iceberg" in low and "trino" in low and "airflow" in low


@pytest.mark.parametrize("prompt_name", WRITING_PROMPTS)
def test_writing_prompts_forbid_reframing_an_employers_industry(prompt_name: str):
    """An employer described as furniture manufacturing and logistics in the
    work history was called a software company to match the target sector."""
    low: str = getattr(prompts, prompt_name).lower()
    assert "industry is a noun" in low
    assert "furniture manufacturing and logistics" in low
    assert "software company" in low


@pytest.mark.parametrize("prompt_name", WRITING_PROMPTS)
def test_writing_prompts_name_the_jd_borrowed_qualifiers(prompt_name: str):
    low: str = getattr(prompts, prompt_name).lower()
    for qualifier in (
        "multi-region deployment",
        "distributed team",
        "self-service tooling",
        "predictive modeling",
        "unified data platform",
    ):
        assert qualifier in low, f"{prompt_name} should name '{qualifier}' as a borrowed qualifier"


@pytest.mark.parametrize("prompt_name", WRITING_PROMPTS)
def test_writing_prompts_forbid_derived_figures(prompt_name: str):
    """A large 'annual savings' figure was an unstated sum of four separate numbers,
    one of them travel rather than licensing. Every component was true and the
    total was still a fabrication."""
    low: str = getattr(prompts, prompt_name).lower()
    assert "as the source states" in low
    assert "do not sum" in low
    assert "combine" in low and "round" in low


def test_fit_score_prompt_also_rejects_derived_figures():
    low = prompts.FIT_SCORE_SYSTEM.lower()
    assert "sum" in low
    assert "aggregate" in low or "combined" in low

"""Shared fictional candidate-profile fixture.

`[candidate] target_titles` is empty by default (see
`models/config.py::CandidateConfig`): a fresh install has no candidate profile
configured, and `matching.pipeline.run_matching` makes no LLM call until it is
set (`CandidateConfig.is_configured()`). Any test that drives `run_matching`
(directly or through the web/daily layers) end to end and expects a real score
needs SOME profile configured, or the run is a no-op by design.

Never use `CandidateConfig()`'s bare defaults in a test that expects scoring
to happen -- that combination means "no candidate profile" (see
matching/pipeline.py), so the scorer is never called and every job stays
unscored.
"""

from __future__ import annotations

from blastjob.models.config import BlastJobConfig, CandidateConfig

TARGET_TITLES = ["VP of Operations", "Director of Operations"]
SENIORITY = "director"
FIELD = "operations"


def profile_cfg() -> CandidateConfig:
    """A CandidateConfig with a fictional profile fully configured."""
    return CandidateConfig(
        target_titles=list(TARGET_TITLES),
        seniority=SENIORITY,
        field=FIELD,
    )


def apply_profile(cfg: BlastJobConfig) -> BlastJobConfig:
    """Mutate `cfg.candidate` in place, for fixtures that build a whole
    `BlastJobConfig` and need the profile layered on rather than replaced.
    Returns `cfg` for chaining."""
    cfg.candidate.target_titles = list(TARGET_TITLES)
    cfg.candidate.seniority = SENIORITY
    cfg.candidate.field = FIELD
    return cfg

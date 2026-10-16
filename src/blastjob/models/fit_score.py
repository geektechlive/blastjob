from typing import Any

from pydantic import BaseModel, Field, model_validator


class _NullTolerant(BaseModel):
    """Model JSON can carry an explicit `null` for any field. Pydantic reads that
    as a wrong type, so ONE null `evidence_quote` rejected the whole fit score,
    the packet then had no score and no scorer-skipped note, and the gate read
    READY. A null means "the model gave nothing", i.e. the field's default."""

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        return {k: v for k, v in data.items() if v is not None}


class ClaimCheck(_NullTolerant):
    claim_text: str = ""
    grounded: bool = False
    # A null quote becomes "" here, which fails the substring check in
    # `scoring.verify_claims` -- correct: a claim with no quote has no evidence.
    evidence_quote: str = ""
    source_section: str = ""


class FitScore(_NullTolerant):
    overall_score: int = 0
    jd_alignment_score: int = 0
    groundedness_score: int = 0
    claims: list[ClaimCheck] = Field(default_factory=list)
    unsupported_count: int = 0
    # Employers named in the resume with no date range beside them. Not a false
    # statement, so it does not count against groundedness, but it is reported.
    undated_employers: list[str] = Field(default_factory=list)
    summary: str = ""

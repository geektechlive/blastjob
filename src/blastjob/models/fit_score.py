from pydantic import BaseModel, Field


class ClaimCheck(BaseModel):
    claim_text: str = ""
    grounded: bool = False
    evidence_quote: str = ""
    source_section: str = ""


class FitScore(BaseModel):
    overall_score: int = 0
    jd_alignment_score: int = 0
    groundedness_score: int = 0
    claims: list[ClaimCheck] = Field(default_factory=list)
    unsupported_count: int = 0
    # Employers named in the resume with no date range beside them. Not a false
    # statement, so it does not count against groundedness, but it is reported.
    undated_employers: list[str] = Field(default_factory=list)
    summary: str = ""

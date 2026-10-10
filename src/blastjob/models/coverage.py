from pydantic import BaseModel, Field


class Requirement(BaseModel):
    text: str = ""
    priority: str = "must"  # "must" | "nice"
    covered: bool = False
    evidence_quote: str = ""
    gap_note: str = ""


class CoverageReport(BaseModel):
    coverage_score: int = 0  # 0-100, weighted toward must-have coverage
    summary: str = ""
    requirements: list[Requirement] = Field(default_factory=list)

    @property
    def must_have_coverage_pct(self) -> int:
        musts = [r for r in self.requirements if r.priority == "must"]
        if not musts:
            return 0
        covered = sum(1 for r in musts if r.covered)
        return int(100 * covered / len(musts))

    @property
    def gap_count(self) -> int:
        return sum(1 for r in self.requirements if not r.covered)

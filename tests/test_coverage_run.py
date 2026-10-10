# ruff: noqa: E501  -- JOB_DESCRIPTION and RESUME_MD below are verbatim literals; their source lines are long.
"""Unit tests for core/coverage_run.py.

JOB_DESCRIPTION and RESUME_MD below are a fictional candidate's job description
and resume text, built to mirror the shape of a real Chief Operations Officer
packet (~/Documents/blastjob/fairview-robotics-2026-09-16/), per the task's
instruction to test against packet-shaped text rather than minimal invented
copy. `analyze_coverage` is stubbed in every test (see test_coverage.py for
the same pattern) — no network call is ever made.
"""

from __future__ import annotations

import asyncio

from blastjob.core import coverage_run
from blastjob.core.coverage_run import coverage_gaps_for, render_gaps_md
from blastjob.core.packet_report import CoverageGap
from blastjob.llm.cost import CostTracker
from blastjob.models.config import BlastJobConfig, PathsConfig
from blastjob.models.coverage import CoverageReport, Requirement

# Models the shape of a real chief-technology-officer.job_description.md.
JOB_DESCRIPTION = (
    "Fairview Specialty Chemicals, a regional distributor of specialty "
    "fittings and bonding materials, is hiring for a Chief "
    "Operations Officer position. This role sits at our main "
    "office in Lakeville, OH and is an on-site/hybrid role.\n\n"
    "Set the technology and transformation agenda for the business, sitting "
    "on the Executive Leadership Team (ELT) and working side by side with the "
    "VP Ops, so the organization's technical capabilities keep pace with where "
    "the business is headed.\n\n"
    "A decade or more of growing technology leadership responsibility, with at "
    "least five years spent in a senior or executive capacity\n\n"
    "A proven record holding a senior technology leadership or advisor title, "
    "whether permanent or on an interim basis\n\n"
    "A history of taking organizations through digital transformation, "
    "system rollouts, cybersecurity maturity work, or M&A integration"
)

# Models the shape of a real
# Alex_Rivera_Resume_-_Fairview_Specialty_Chemicals_Chief_Operations_Officer.md
RESUME_MD = """# Alex J. Rivera

Lakeview, OH 43215 | 614-555-0147 | alex.rivera@example.com | https://www.linkedin.com/in/example-user/

## Professional Summary

Technology executive with 20+ years of experience, from hands-on systems administration to running operations at the director level. Industries include logistics, insurance, benefits administration, managed services and specialty distribution. Currently Director of Business Operations at Northwind Logistics, a privately held holding-company subsidiary, where they own the NetSuite ERP and application portfolio, the shift to the public cloud, and an enterprise collaboration-suite rollout that is 95% deployed. At Contoso Health Analytics they built IT operations up from a single analyst-engineer pair into a multi-function team as the company scaled from $60M to $180M in revenue, and achieved a regional security attestation and an industry data-handling certification. Has run technical due diligence on acquisitions and submitted an internal automation framework for an industry innovation award that reached finalist status. Certified Scaled Agile Practice Consultant.

## Career Experience

### Director of Business Operations
**Northwind Logistics (Crestline Holdings subsidiary)** | Rivermont, OH | Jan 2029 - Present
- Run operations strategy and delivery for a 110-employee business unit. Lead a twelve-person team across five functions (network, security, service desk, release engineering and line-of-business applications) and a $1.2M operating budget, not counting salaries.
- Drove the enterprise collaboration-suite rollout under executive sponsorship. It reached 95% adoption with company-wide training complete.
- Overseeing the shift to a fully managed cloud footprint: 85% complete, aiming to wrap the migration inside the next twelve months.
- Own the rolling technology and ERP roadmap, plus capital and operating planning, vendor strategy and contract negotiations.
- Stand up a regional-attestation-aligned security program for infrastructure. Act as the escalation point business-unit leaders rely on for outage response and risk calls.

### Director of Operations
**Contoso Health Analytics** | Oakbrook, OH | Nov 2024 - Dec 2027
- Took IT operations from one analyst-engineer pair at a $60M firm up to a multi-function team at a $180M firm across four years. Led eighteen people spanning systems work, data operations, the help desk, release work, and app support, moving two into management.
- Earned a regional security attestation and an industry data-handling certification in a heavily regulated analytics business.
- Led the migration to a managed cloud platform with a web application firewall, intrusion detection and least-privilege access.
- Took ownership of IT financial management and procurement, trimming $600K from run-rate spend within the first year by consolidating tools and renegotiating multi-year vendor contracts.

## Skills

Technology Strategy and Roadmaps, Enterprise Architecture, NetSuite ERP, Collaboration Suite Rollouts, Managed Cloud Migration, Cybersecurity Governance, Regional Security Attestation, Data-Handling Certification, Network Segmentation, IT Financial Management, Capital and Operating Planning, Vendor Management, Contract Negotiation, M&A Technical Diligence, Change Management, Disaster Recovery
"""

# A quote already present verbatim in RESUME_MD -- the requirement it backs
# must never surface as a gap.
USED_QUOTE = "Earned a regional security attestation and an industry data-handling certification"

# A true work-history sentence that never made it into RESUME_MD (modeled on
# the real measured case in core/coverage_gap.py's docstring: a
# change-advisory-board process that was absent from the generated bullets).
UNUSED_QUOTE = (
    "Established a formal change-advisory-board process that governed every "
    "high-risk infrastructure change across the organization."
)


def _report_with(*, used: bool, unused: bool) -> CoverageReport:
    reqs = []
    if used:
        reqs.append(
            Requirement(
                text="Security compliance leadership",
                priority="must",
                covered=True,
                evidence_quote=USED_QUOTE,
            )
        )
    if unused:
        reqs.append(
            Requirement(
                text="Change management / risk governance",
                priority="must",
                covered=True,
                evidence_quote=UNUSED_QUOTE,
            )
        )
    return CoverageReport(coverage_score=80, summary="Strong fit.", requirements=reqs)


def _cfg(tmp_path) -> BlastJobConfig:
    return BlastJobConfig(
        paths=PathsConfig(data_dir=str(tmp_path / "data"), output_dir=str(tmp_path / "out"))
    )


class _CountingStub:
    """Stands in for analyze_coverage: returns a fixed report, counts calls."""

    def __init__(self, report: CoverageReport):
        self.report = report
        self.calls: list[str] = []

    async def __call__(self, job_description, app_config, cost_tracker, on_text=None):
        self.calls.append(job_description)
        return self.report


# ---------------------------------------------------------------------------
# coverage_gaps_for
# ---------------------------------------------------------------------------


def test_coverage_gaps_for_finds_the_unused_requirement(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    stub = _CountingStub(_report_with(used=True, unused=True))
    monkeypatch.setattr(coverage_run, "analyze_coverage", stub)

    gaps = asyncio.run(
        coverage_gaps_for(
            JOB_DESCRIPTION,
            RESUME_MD,
            cfg=cfg,
            cost_tracker=CostTracker(),
            cache_dir=tmp_path / "cache",
        )
    )

    assert len(gaps) == 1
    assert gaps[0].history_quote == UNUSED_QUOTE
    assert gaps[0].requirement == "Change management / risk governance"


def test_coverage_gaps_for_does_not_flag_a_used_requirement(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    stub = _CountingStub(_report_with(used=True, unused=False))
    monkeypatch.setattr(coverage_run, "analyze_coverage", stub)

    gaps = asyncio.run(
        coverage_gaps_for(
            JOB_DESCRIPTION,
            RESUME_MD,
            cfg=cfg,
            cost_tracker=CostTracker(),
            cache_dir=tmp_path / "cache",
        )
    )

    assert gaps == []


def test_cache_hit_avoids_a_second_llm_call(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    stub = _CountingStub(_report_with(used=True, unused=True))
    monkeypatch.setattr(coverage_run, "analyze_coverage", stub)
    cache_dir = tmp_path / "cache"

    first = asyncio.run(
        coverage_gaps_for(
            JOB_DESCRIPTION, RESUME_MD, cfg=cfg, cost_tracker=CostTracker(), cache_dir=cache_dir
        )
    )
    second = asyncio.run(
        coverage_gaps_for(
            JOB_DESCRIPTION, RESUME_MD, cfg=cfg, cost_tracker=CostTracker(), cache_dir=cache_dir
        )
    )

    assert len(stub.calls) == 1  # the second call was served from cache
    assert first == second
    assert len(first) == 1


def test_cache_miss_on_a_changed_job_description(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    stub = _CountingStub(_report_with(used=True, unused=True))
    monkeypatch.setattr(coverage_run, "analyze_coverage", stub)
    cache_dir = tmp_path / "cache"

    asyncio.run(
        coverage_gaps_for(
            JOB_DESCRIPTION, RESUME_MD, cfg=cfg, cost_tracker=CostTracker(), cache_dir=cache_dir
        )
    )
    changed_jd = JOB_DESCRIPTION + " Additional requirement: SAP experience."
    asyncio.run(
        coverage_gaps_for(
            changed_jd, RESUME_MD, cfg=cfg, cost_tracker=CostTracker(), cache_dir=cache_dir
        )
    )

    assert len(stub.calls) == 2  # different JD text -> cache miss -> second call


def test_default_cache_dir_is_under_data_dir(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    stub = _CountingStub(_report_with(used=True, unused=True))
    monkeypatch.setattr(coverage_run, "analyze_coverage", stub)

    asyncio.run(coverage_gaps_for(JOB_DESCRIPTION, RESUME_MD, cfg=cfg, cost_tracker=CostTracker()))

    default_dir = tmp_path / "data" / "coverage_cache"
    assert default_dir.exists()
    assert list(default_dir.glob("*.json"))


def test_analyze_coverage_failure_yields_empty_list_not_an_exception(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)

    async def _raise(*_a, **_kw):
        raise RuntimeError("LLM call failed")

    monkeypatch.setattr(coverage_run, "analyze_coverage", _raise)

    gaps = asyncio.run(
        coverage_gaps_for(
            JOB_DESCRIPTION,
            RESUME_MD,
            cfg=cfg,
            cost_tracker=CostTracker(),
            cache_dir=tmp_path / "cache",
        )
    )

    assert gaps == []


def test_corrupt_cache_file_is_treated_as_a_miss(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    stub = _CountingStub(_report_with(used=True, unused=True))
    monkeypatch.setattr(coverage_run, "analyze_coverage", stub)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    bad_path = coverage_run._cache_path(cache_dir, JOB_DESCRIPTION)
    bad_path.write_text("not valid json {{{", encoding="utf-8")

    gaps = asyncio.run(
        coverage_gaps_for(
            JOB_DESCRIPTION, RESUME_MD, cfg=cfg, cost_tracker=CostTracker(), cache_dir=cache_dir
        )
    )

    assert len(stub.calls) == 1
    assert len(gaps) == 1


# ---------------------------------------------------------------------------
# render_gaps_md
# ---------------------------------------------------------------------------


def test_render_gaps_md_no_gaps():
    md = render_gaps_md([])
    assert "No gaps found" in md


def test_render_gaps_md_preserves_history_quote_verbatim():
    gap = CoverageGap(
        requirement="Change management / risk governance",
        history_quote=UNUSED_QUOTE,
        priority="must",
    )
    md = render_gaps_md([gap])

    assert UNUSED_QUOTE in md
    assert gap.requirement in md
    assert "must" in md


def test_render_gaps_md_never_rewrites_the_quote():
    # A quote containing punctuation/casing that an LLM paraphrase would be
    # tempted to "clean up" must survive character-for-character.
    quirky_quote = "piloted a scheduling tool for two teams (not yet rolled out)."
    gap = CoverageGap(requirement="Scheduling tool rollout", history_quote=quirky_quote)
    md = render_gaps_md([gap])
    assert quirky_quote in md


def test_render_gaps_md_multiple_gaps_each_rendered():
    gaps = [
        CoverageGap(requirement="Req A", history_quote="Quote A", priority="must"),
        CoverageGap(requirement="Req B", history_quote="Quote B", priority="nice"),
    ]
    md = render_gaps_md(gaps)
    assert "Req A" in md and "Quote A" in md
    assert "Req B" in md and "Quote B" in md

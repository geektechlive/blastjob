# ruff: noqa: E501  -- RESUME_MD below is a verbatim literal; its source lines are long.
"""Tests for core/coverage_gap.py -- the "third bucket": work-history-provable
requirements the resume never used.

RESUME_MD below is a fictional candidate's complete resume markdown, built to
mirror the shape of a real, previously generated packet:
  ~/Documents/blastjob/fairview-robotics-2026-09-15/
  Alex_Rivera_Resume_-_Fairview_Robotics_Chief_Operations_Officer.md
(Fairview Robotics, Chief Operations Officer, built 2026-09-15). The full
document -- not an excerpt -- is used because coverage_gap._OVERLAP_THRESHOLD was
calibrated against a real file of this shape (see coverage_gap.py's module-level
comment); an excerpt would drop unrelated vocabulary (e.g. the word "savings"
appears in the Contoso Health Analytics bullet, not the Harborview one) and
silently change which quotes cross the threshold.

The `_QUOTE_*` constants below mirror, with fictional wording, the same
verbatim/reordered/trimmed/absent relationships a real
~/.blastjob/work_history.md exhibited at module-write time.

No test in this file makes a real LLM call -- `CoverageReport`/`Requirement`
objects are constructed directly as literals, exactly as `coverage.py`'s
Python-side verification would have produced them after downgrading any quote
that isn't a genuine substring of work_history.md.
"""

from __future__ import annotations

from blastjob.core.coverage_gap import unused_support
from blastjob.core.packet_report import CoverageGap
from blastjob.models.coverage import CoverageReport, Requirement

RESUME_MD = """\
# Alex J. Rivera

Lakeview, OH 43215 | 614-555-0147 | alex.rivera@example.com | https://www.linkedin.com/in/example-user/

## Summary

Operations-minded technology leader spanning two decades across logistics, benefits administration, insurance, specialty distribution, and early-stage startups. Scales support organizations up from nothing, swaps out aging infrastructure while the business keeps running, owns ERP and line-of-business application portfolios, and sits with executives on funding sequencing, risk appetite, and multi-year planning. Came up through hands-on systems administration and solution design before moving into director-level leadership. Holds a Certified Scrum Professional credential. Now treats AI adoption as a standing discipline and is repositioning the department as a change-management partner to the rest of the company.

## Experience

### Director of Business Operations
**Northwind Logistics (Crestline Holdings / Meridian Group)** | Rivermont, OH | Jan 2029 - Present
- Run technology strategy and delivery for a subsidiary of a national holding company that handles in-store merchandising, digital storefronts, product data, and loss-prevention tooling for retail brands.
- Set and drive a multi-year technology and ERP plan tied to operating efficiency, accurate reporting, and the ability to scale; control a $1.4M budget covering roughly 115 staff.
- Run a twelve-person group spanning five functions that report up directly: network and endpoint, security, service desk, quality, and release engineering.
- Drove adoption of a company-wide AI writing assistant under direct executive backing - rollout reached 95% of staff with training wrapped; rebranded the group as 'Platform Enablement' so it reads as a change-management partner rather than a cost center.
- Moving the estate to a single public-cloud provider, now about 85% done, aiming to finish within the year, across what had been a patchwork of two providers and on-site racks.
- Stood up a security governance program aligned to two regional compliance frameworks and partnered with the security team on identity and access controls; is the first call business-group heads make when something breaks or a risk decision is needed.

### Director of Security and Business Operations / vCOO
**Lakeshore Total IT** | Crestview, OH | Apr 2028 - Jan 2029
- Ran engineering and delivery for a managed-services firm supporting client businesses ranging from 100 to 1,000 staff; answered directly to the owner.
- Put in place standardized onboarding steps that lifted client retention by 18%.
- Built the roadmap alongside client leadership teams to track to their own goals; reviewed internal workflows and kicked off a tool-consolidation effort; drafted a plan to turn delivery into a repeatable practice.

### Director of Operations
**Contoso Health Analytics** | Crestview, OH | Nov 2024 - Dec 2027
- Grew the support group from a lone analyst and one engineer at a $55M firm into a multi-function organization at a $160M firm across four years; ran a staff of nineteen split across engineering, data operations, the help desk, release work, and app support, moving two of them up into management.
- Walked the business through a regional data-handling certification and an industry security attestation inside a tightly regulated analytics setting; brought in an outside firm for recurring audits and built the follow-up remediation plans with the internal team.
- Moved workloads onto a managed cloud platform with a web firewall, tightly scoped access, and intrusion monitoring, cutting the time to ship new products from months to weeks; standardized release tooling so that nearly all manual deploys ran through pipelines instead, cutting deploy-related incidents substantially.
- Took over budgeting, purchasing, and expense tracking for the group; delivered real savings in year one by renegotiating long-running vendor deals and folding overlapping tools together, and stood up cloud spend dashboards broken out by product and usage trend.
- Brought the group onto a structured planning cadence and earned a practice-level agile credential, folding the team into company-wide planning cycles to speed up delivery meaningfully; stood up a formal service desk holding strong uptime targets with round-the-clock coverage of revenue-critical systems, cutting ticket response times sharply.
- Sits on the company's cross-group technology steering committee alongside the COO and President, pushing a single shared operating model across dozens of practice areas and pooling purchasing power on shared tools.

### Operations Manager
**Contoso Health Analytics** | Crestview, OH | Jan 2021 - Oct 2024
- Took the aging network apart and rebuilt it along with roughly 165 virtual machines and the directory service, splitting one flat network into five separated zones behind next-generation firewalls and layering in a web firewall and intrusion monitoring; the year-plus effort closed out without a single outage or lost record.
- Replaced worn-out gear with a converged compute and storage platform and a faster backbone network; stood up a 60TB analytics warehouse for dashboarding and trend reporting, designed from the ground up.

### Operations Manager, Technical Services
**Lakeside Health Partners** | Thornridge, OH | Sep 2020 - Dec 2020
- Ran a group of thirty-three staff plus three team leads across every infrastructure workstream covering roughly 8,700 clinical and administrative employees, spanning the network and server estate for four hospital sites and dozens of clinics; held uptime above target for care-critical systems the whole stretch.
- Put in place a structured change-control workflow to track and announce every environment change, giving executives clearer visibility into risk; cut response times for care-system incidents down to a handful of minutes.

### Operations Manager, Infrastructure Engineering Services
**Cedar Ridge Mutual Insurance** | Cedarbrook, OH | Mar 2019 - Sep 2020
- Ran a fourteen-person infrastructure crew, including five offshore mainframe specialists and two security engineers, across three sites; controlled an $18M budget for every infrastructure and operations line item.
- Worked with leadership to shift spending from a capital model toward a mostly operating one over several years, trimming costs meaningfully; got a better rate on the virtualization license renewal and locked in cheaper connectivity contracts.
- Shifted mainframe support offshore, trimming payroll costs the following year; stood up a large flash storage array that delivered a big jump in throughput while landing under budget.

### Team Operations Manager
**Harborview Consulting LLC** | Millbrook, OH | Dec 2018 - Mar 2019
- Ran a managed-IT practice serving a growing client roster, including a regional lab client as it cleared a major regulatory milestone; grew the practice from a single hire to six and rolled out a new data-services line.
- Renegotiated contracts with regional vendors to cut operating costs by 22%; partnered with leadership and outside vendors on a new service line, which brought in a meaningful share of new business every year.

### Director of Facilities Operations
**Keystone Analytics Inc.** | Thornridge, OH | Jul 2015 - Nov 2018
- Served as lead architect over all enterprise infrastructure and operations, smoothing out data intake and ingestion for analytical tooling built around a regulated-industry dataset; owned the entire operating function including budgeting and planning as part of the leadership table.
- Pitched and piloted an internal automation framework that reached finalist standing in an industry innovation award; was not named the winner. The work sped up onboarding of new accounts, drew interest from outside investors, and helped lift the company's valuation.
- Mapped out an enterprise architecture plan with near- and long-term targets to bring down the cost of doing business while lifting throughput through automation; trimmed recurring operating costs meaningfully in year one through vendor renegotiation.

### Director of Business Operations
**Vertex Software, Inc.** | Rivermont, OH | Mar 2010 - Jul 2015
- Led technical and financial due diligence across two acquisitions, bringing both business units onto the parent company's systems within the first year.

### Database Team Manager
**Solstice Solutions, Inc.** | Deer Run, OH | Oct 2003 - Mar 2010

### Computer Consultant
**Meridian Business Systems** | Crestview, OH | Jun 2002 - Oct 2003

### Database Administrator
**Hopewell Living Services** | Crestview, OH | Oct 2001 - Jun 2002

## Skills

Enterprise Architecture | Technology Strategy | Solution Design | IT Transformation | IT Financial Management | Capital and Operating Planning | Multi-Year Planning and Budgeting | Mergers and Acquisitions (M&A) | Vendor Management and Negotiation | IT Service Management | ERP Platforms | Line-of-Business Applications | Process Improvement | Data Center Operations | Public and Hybrid Cloud | Virtualization | Network Engineering | Identity and Access Management | Endpoint and Identity Management | Zero Trust | Cybersecurity Governance | Data-Handling Certification | Regional Security Attestation | Compliance | Intrusion Monitoring | Risk Management | Disaster Recovery and Business Continuity | Web Application Firewall | BI Dashboards | Release Engineering and CI/CD | Data Analytics | Data Warehousing | SQL | ETL | AI Writing Assistants | Artificial Intelligence (AI) | Emerging Technologies | Service Management Frameworks | Agile Planning | Program and Project Management | Managed Services | Change Management (CAB) | Team Building and Mentorship | Organizational Scaling | Executive Communication

## Education

- Northshore University (Northshore College)
- Lakeside Institute of Design

## Certifications

- Certified Scrum Professional (CSP), Agile Credentialing Board - March 2027
- Service Management Foundation, LinkedIn Learning - November 2026"""

# --- Fictional work_history.md quotes, mirroring a real file's verbatim/
# reordered/trimmed/absent relationships at module-write time. ---

# used by the resume (Operations Manager bullet), reordered but not
# materially reworded.
_QUOTE_80TB = (
    "Designed and stood up a 60TB analytics warehouse for dashboarding and trend reporting."
)

# used verbatim by the resume (Vertex Software bullet).
_QUOTE_MA = (
    "Led technical and financial due diligence across two acquisitions, "
    "bringing both business units onto the parent company's systems within "
    "the first year."
)

# used by the resume (Harborview bullet); resume drops the trailing clause
# but keeps the operative sentence.
_QUOTE_VENDOR_25PCT = (
    "Renegotiated contracts with regional vendors to cut operating costs by "
    "22%, passing a share of the savings on to downstream clients."
)

# used by the resume (Lakeside Health Partners bullet) but with its back half
# reworded: "raising visibility of risk for executive leadership" became
# "giving executives clearer visibility into risk". Same idea, different words.
_QUOTE_CHANGE_PROCESS = (
    "Put in place a structured change-control workflow to track and "
    "announce every environment change, raising visibility of risk for "
    "executive leadership."
)

# genuinely absent from every resume bullet. The only trace of "CAB" anywhere
# in RESUME_MD is the bare term "Change Management (CAB)" in the Skills line,
# which this module must not count as usage.
_QUOTE_CAB = (
    "Partnered with information security to cut risk by introducing a "
    "change advisory board process in the ticketing tool for tighter "
    "oversight of production changes."
)


def _report(*requirements: Requirement) -> CoverageReport:
    return CoverageReport(coverage_score=0, summary="", requirements=list(requirements))


def test_covered_requirement_present_in_resume_is_not_a_gap():
    """An M&A requirement whose evidence quote was copied into the resume
    verbatim must not be reported -- the resume already used it."""
    req = Requirement(
        text="Track record of guiding organizations through ... M&A integration",
        priority="must",
        covered=True,
        evidence_quote=_QUOTE_MA,
    )
    gaps = unused_support(_report(req), RESUME_MD)
    assert gaps == []


def test_covered_requirement_absent_from_resume_is_a_gap():
    """The CAB-process requirement's evidence never appears as a claim in any
    bullet -- only the bare term survives in the Skills line, which must not
    count as usage. This is the third bucket the module exists to catch."""
    req = Requirement(
        text="Establish enterprise technology governance and prioritization frameworks",
        priority="must",
        covered=True,
        evidence_quote=_QUOTE_CAB,
    )
    gaps = unused_support(_report(req), RESUME_MD)
    assert gaps == [
        CoverageGap(
            requirement=req.text,
            history_quote=_QUOTE_CAB,
            priority="must",
        )
    ]


def test_covered_requirement_absent_ignores_skills_line_mention():
    """Confirms the Skills-line exclusion specifically: the bare term is
    present in RESUME_MD, yet the requirement is still a gap. Regression guard
    in case a future edit accidentally matches against the raw resume instead
    of the skills-stripped body."""
    assert "Change Management (CAB)" in RESUME_MD  # the bare term IS present
    req = Requirement(
        text="Establish enterprise technology governance and prioritization frameworks",
        priority="must",
        covered=True,
        evidence_quote=_QUOTE_CAB,
    )
    gaps = unused_support(_report(req), RESUME_MD)
    assert len(gaps) == 1  # still a gap despite the keyword sitting in Skills


def test_uncovered_requirement_is_never_a_gap():
    """A requirement the work history cannot prove (covered=False) must never
    be reported, no matter what the resume says -- there is nothing true to
    insert, and reporting it would point toward fabrication."""
    req = Requirement(
        text="Graduate business degree (MBA) or other advanced credential preferred",
        priority="nice",
        covered=False,
        evidence_quote="",
        gap_note="Not found in work history; the candidate holds no MBA.",
    )
    gaps = unused_support(_report(req), RESUME_MD)
    assert gaps == []


def test_covered_requirement_reworded_in_resume_is_not_a_gap():
    """The change-control-workflow quote survives in the resume with its
    back half reordered ("raising visibility of risk for leadership" ->
    "giving executives clearer visibility into risk"). Real paraphrase,
    real resume -- must not be reported as missing."""
    req = Requirement(
        text=(
            "Keep enterprise risk in check through sound policy, oversight, and vendor discipline"
        ),
        priority="nice",
        covered=True,
        evidence_quote=_QUOTE_CHANGE_PROCESS,
    )
    gaps = unused_support(_report(req), RESUME_MD)
    assert gaps == []


def test_covered_requirement_with_light_rewording_is_not_a_gap():
    """The 22%-vendor-savings quote is present but the resume drops its
    trailing clause. Enough of the sentence survives -- including "savings",
    which recurs in the Contoso bullet elsewhere in the same resume -- that
    it must not be flagged."""
    req = Requirement(
        text="Hold vendors to a disciplined selection, contracting, and "
        "performance-accountability standard",
        priority="nice",
        covered=True,
        evidence_quote=_QUOTE_VENDOR_25PCT,
    )
    gaps = unused_support(_report(req), RESUME_MD)
    assert gaps == []


def test_heavy_rewrite_is_a_known_false_positive():
    """KNOWN FALSE POSITIVE, documented per the module docstring: bag-of-words
    overlap cannot survive a sufficiently heavy rewrite. This pair is
    synthetic (not from the real Fairview Robotics packet) specifically to demonstrate
    the failure mode named in coverage_gap.unused_support's docstring -- a
    resume bullet that says the same true thing in almost none of the same
    words gets flagged as unused even though it is on the page. This is the
    accepted cost of a deterministic, non-LLM matcher (see the module's
    "NO LLM REPAIR HERE" rationale)."""
    quote = (
        "Reduced enterprise cloud spend by consolidating redundant vendor "
        "contracts across three business units."
    )
    resume_md = (
        "## Experience\n\n### Director\n**Acme** | City, ST | 2020 - 2023\n"
        "- Cut cloud costs company-wide by folding overlapping supplier "
        "agreements together.\n"
    )
    req = Requirement(text="Cost discipline", priority="nice", covered=True, evidence_quote=quote)
    gaps = unused_support(_report(req), resume_md)
    assert len(gaps) == 1  # false positive: the fact IS on the page, differently worded
    assert gaps[0].history_quote == quote


def test_unused_support_returns_only_the_genuine_gap_from_a_mixed_report():
    """End-to-end sanity check across all five categories in one report: used
    verbatim (M&A), used reworded (change-management process, vendor 22%),
    uncovered (MBA), and the one genuine gap (CAB)."""
    ma = Requirement(
        text="M&A track record", priority="must", covered=True, evidence_quote=_QUOTE_MA
    )
    eighty_tb = Requirement(
        text="Modernize core data platforms",
        priority="must",
        covered=True,
        evidence_quote=_QUOTE_80TB,
    )
    vendor = Requirement(
        text="Vendor contract discipline",
        priority="nice",
        covered=True,
        evidence_quote=_QUOTE_VENDOR_25PCT,
    )
    change_process = Requirement(
        text="Policies, controls, and vendor practices",
        priority="nice",
        covered=True,
        evidence_quote=_QUOTE_CHANGE_PROCESS,
    )
    mba = Requirement(
        text="Graduate business degree or other advanced credential preferred",
        priority="nice",
        covered=False,
        evidence_quote="",
    )
    cab = Requirement(
        text="Enterprise technology governance frameworks",
        priority="must",
        covered=True,
        evidence_quote=_QUOTE_CAB,
    )

    gaps = unused_support(_report(ma, eighty_tb, vendor, change_process, mba, cab), RESUME_MD)

    assert len(gaps) == 1
    assert gaps[0].history_quote == _QUOTE_CAB
    assert gaps[0].requirement == cab.text

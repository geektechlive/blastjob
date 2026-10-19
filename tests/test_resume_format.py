"""Tests for `core.resume_format.check_structure` and `core.docx_text`.

Fixtures are a fictional candidate's work history and resume, built to mirror
the shape of a real `~/.blastjob/work_history.md` and a real
northwind-logistics-2026-09-16 packet under `~/Documents/blastjob/` (same
structure, headings, and edge cases) rather than read from those paths at
test time, so the suite is hermetic and reproducible on any machine.
"""

# ruff: noqa: E501 -- these fixture lines mirror real work-history/resume
# lines in shape and are not line-wrapped to keep that shape intact.

from __future__ import annotations

from datetime import date
from textwrap import dedent

import pytest

from blastjob.core.docx_text import DocxRegion, extract_lines, extract_text
from blastjob.core.packet_report import Severity
from blastjob.core.resume_format import check_structure
from blastjob.exporters.docx import write as write_docx

TODAY = date(2030, 9, 16)

# Mirrors a real ~/.blastjob/work_history.md (headings only, plus a
# placeholder bullet where the real file has real accomplishment bullets --
# role_not_in_history and employer_names() only read `### ` heading lines).
WORK_HISTORY_MD = dedent("""\
    ---
    name: Alex J. Rivera
    email: alex.rivera@example.com
    phone: 614-555-0147
    ---

    ## Experience

    ### Director of Business Operations (internally rebranded as 'Platform Enablement') | Northwind Logistics (Crestline Holdings / Meridian Group) | Fairhaven, OH (Jan 2029 – Present)
    - placeholder bullet

    ### Director of Security and Business Operations / vCOO | Lakeshore Total IT | Oakbrook, OH (Apr 2028 – Jan 2029)
    - placeholder bullet

    ### Director of Operations | Contoso Health Analytics | Oakbrook, OH (Nov 2024 – Dec 2027)
    - placeholder bullet

    ### Operations Manager | Contoso Health Analytics | Oakbrook, OH (Jan 2021 – Oct 2024)
    - placeholder bullet

    ### Operations Manager, Service Delivery (Manager, Business Services – Infrastructure) | Lakeside Health Partners | Elmcrest, OH (Sep 2020 – Dec 2020)
    - placeholder bullet

    ### Operations Manager, Network Engineering Services | Cedar Ridge Mutual Insurance | Cedarbrook, OH (Mar 2019 – Sep 2020)
    - placeholder bullet

    ### Team Operations Manager | Harborview Consulting LLC | Rivergate, OH (Dec 2018 – Mar 2019)
    - placeholder bullet

    ### Director of Facilities Operations | Keystone Analytics Inc. | Elmcrest, OH (listed under a different city in earlier resume drafts) (Jul 2015 – Nov 2018)
    - placeholder bullet

    ### Director of Business Operations | Vertex Software, Inc. | Fairhaven, OH (Mar 2010 – Jul 2015)
    - placeholder bullet

    ### Database Team Manager | Solstice Solutions, Inc. | Hartwell, OH (listed under a different city in an earlier resume draft) (Oct 2003 – Mar 2010)

    ### Computer Consultant | Meridian Business Systems | Oakbrook, OH (Jun 2002 – Oct 2003)

    ### Database Administrator | Hopewell Living Services | Oakbrook, OH (Oct 2001 – Jun 2002)
    """)

# Modelled on the real, currently-shipping
# ~/Documents/blastjob/northwind-logistics-2026-09-16/..._Resume_....md -- the "good"
# shape post-fix: every role that ended within 20 years has a bullet, roles
# are title-only headings with a `**Employer** | City, ST | dates` line.
CLEAN_RESUME_MD = dedent("""\
    # Alex J. Rivera

    Lakeview, OH 43215 | 614-555-0147 | alex.rivera@example.com | linkedin.com/in/example-user

    ## Professional Summary

    Operations-minded technology leader with 25+ years spanning logistics, benefits administration, insurance, and early-stage startups, including stints at a managed-services firm and a consulting shop. Currently owns the department end to end: ERP and line-of-business applications, network and endpoint, the help desk, quality, and release engineering. Has scaled teams up from nothing, walked a regulated business through a security attestation and a data-handling certification, and drove an AI-assistant rollout to 95% adoption with training wrapped company-wide. A steady, service-minded leader who sits with executives on budgets, vendors, risk, and acquisition diligence. Holds a Scaled Agile practice credential and a service-management foundation badge.

    ## Core Competencies

    - **Delivery & Applications:** Release Engineering, Deployment Pipelines, CI/CD, Change Management (CAB), ERP Platforms, Line-of-Business Applications, Agile Planning, Program Oversight
    - **Operations & Support:** Service Desk Leadership, Incident and Escalation Handling, End-User Device Support, Google Workspace, Jamf, Okta, Public/Hybrid Cloud (AWS, Azure), Business Continuity Planning, Network Engineering
    - **Security & Compliance:** Access Governance, Zero-Trust Network Access, Identity Lifecycle Controls, Regional Security Attestation, Data-Handling Certification, HIPAA, NIST Alignment, SIEM, EDR, Governance
    - **Leadership & Commercial:** Technology Strategy, Budget Ownership, Vendor Negotiation, Mergers & Acquisitions (M&A), Managed Services Oversight, AI Writing Assistants, Mentorship and Team Growth

    ## Career Experience

    ### Director of Business Operations
    **Northwind Logistics (Crestline Holdings / Meridian Group)** | Fairhaven, OH | Jan 2029 - Present
    - Run technology strategy and delivery across infrastructure, the ERP platform, and line-of-business applications. Lead a twelve-person group across five functions reporting up directly (network, security, service desk, quality, and release engineering), covering roughly 115 staff.
    - Drove the enterprise AI writing-assistant rollout under direct executive backing: 95% adoption, with company-wide training wrapped.
    - Stand up a security program aligned to regional compliance frameworks. Partner with the security team on zero-trust and identity controls.
    - Control a $1.4M operating budget (excluding payroll), capital and operating planning, vendor strategy, and contract negotiations. Set the multi-year technology and ERP plan.
    - Moving the estate to a single public-cloud provider, now about 85% complete. Rebranded the group as 'Platform Enablement' so it is seen company-wide as the team that leads process improvement rather than just keeping the lights on.

    ### Director of Security and Business Operations / vCOO
    **Lakeshore Total IT** | Oakbrook, OH | Apr 2028 - Jan 2029
    - Ran engineering and delivery for a managed-services firm supporting client businesses from 100 to 1,000 staff. Reported straight to the founder and president.
    - Put in place standardized onboarding steps that lifted client retention by 18%.
    - Built the roadmap alongside client leadership. Visited client sites to capture input on priorities and friction points.
    - Kicked off a tool-consolidation effort and drafted a plan to turn delivery into a repeatable practice.

    ### Director of Operations
    **Contoso Health Analytics** | Oakbrook, OH | Nov 2024 - Dec 2027
    - Grew the support group from a single analyst and engineer at a $55M firm into a multi-function organization at a $160M firm within four years. Led nineteen people across systems engineering, data operations, the service desk, release engineering, and application support, moving two of them into management.
    - Walked the business through a regional security attestation and a data-handling certification in a heavily regulated analytics setting. Ran recurring outside audits and built remediation plans with the internal security team.
    - Took ownership of technology budgeting, sourcing, and spend tracking, cutting run-rate costs meaningfully within the first year by folding overlapping tools together and renegotiating multi-year supplier agreements.
    - Stood up a service desk that held strong uptime targets. Standardized on a service framework and cut incident response times substantially.
    - Moved nearly all manual code and database deploys onto pipelines, cutting deployment issues sharply. Added a change-advisory-board process in the ticketing tool and brought the group into company-wide planning cycles, speeding delivery meaningfully.

    ### Operations Manager
    **Contoso Health Analytics** | Oakbrook, OH | Jan 2021 - Oct 2024
    - Took apart and rebuilt the network, roughly 165 virtual machines, and the directory service. Split a flat network into 5 separate zones behind next-generation firewalls and added a web firewall and intrusion monitoring. The year-long project closed out with zero outages or lost records.
    - Put every system on a rolling update schedule to keep security patches current.

    ### Operations Manager, Service Delivery
    **Lakeside Health Partners** | Elmcrest, OH | Sep 2020 - Dec 2020
    - Ran a crew of thirty-two staff plus three shift leads supporting roughly 8,500 clinical and administrative employees across 4 hospital sites and 40+ clinics. Held uptime above target for care-critical systems throughout.
    - Put a structured change-approval workflow in place and shrank incident response times for care-critical systems to a handful of minutes.

    ### Operations Manager, Network Engineering Services
    **Cedar Ridge Mutual Insurance** | Cedarbrook, OH | Mar 2019 - Sep 2020
    - Ran an infrastructure crew of 14, including 5 offshore mainframe specialists and 2 security engineers, and controlled an $18M budget.
    - Worked with leadership to shift spending from a capital model toward a mostly operating model, trimming costs meaningfully over 3 years.
    - Got a better rate on the virtualization license renewal and locked in cheaper connectivity contracts going forward.

    ### Team Operations Manager
    **Harborview Consulting LLC** | Rivergate, OH | Dec 2018 - Mar 2019
    - Grew a client-services group from a single new hire into a team of six and launched a new data-services offering. Clients included a regional lab as it cleared a major regulatory milestone.
    - Partnered with leadership and outside vendors on new offerings. One new service line brought in a meaningful share of new business every year.
    - Renegotiated supplier agreements, trimming run-rate spend by 22%. Rolled out a ticketing platform that cut average turnaround from weeks to a matter of hours.

    ### Director of Facilities Operations
    **Keystone Analytics Inc.** | Elmcrest, OH | Jul 2015 - Nov 2018
    - Owned the entire IT function, including budgeting and planning, as part of the leadership table. Renegotiated vendor contracts to trim recurring costs meaningfully in the first year.
    - Pitched and piloted an internal automation framework that reached finalist standing in an industry innovation award; was not named the winner. The work helped lift the company's valuation.

    ### Director of Business Operations
    **Vertex Software, Inc.** | Fairhaven, OH | Mar 2010 - Jul 2015
    - Supported technology diligence on two prospective acquisitions and led the systems integration once both closed.

    ### Database Team Manager
    **Solstice Solutions, Inc.** | Hartwell, OH | Oct 2003 - Mar 2010

    ### Computer Consultant
    **Meridian Business Systems** | Oakbrook, OH | Jun 2002 - Oct 2003

    ### Database Administrator
    **Hopewell Living Services** | Oakbrook, OH | Oct 2001 - Jun 2002

    ## Certifications

    - Certified Scrum Professional (CSP) | Agile Credentialing Board | Feb 2027
    - Project Management Fundamentals | LinkedIn Learning | Dec 2026
    - Advanced Vendor Negotiation | Fabrikam Procurement Institute | Mar 2029

    ## Education

    - Northshore University
    - Lakeside Institute of Design
    """)


def _codes(findings, code: str) -> list:
    return [f for f in findings if f.code == code]


# ---------------------------------------------------------------------------
# empty_recent_role (FATAL) -- the defect that actually shipped
# ---------------------------------------------------------------------------


def test_empty_recent_role_flags_contoso_infrastructure_manager():
    """The exact shape that shipped in fairview-robotics-2026-09-13,
    cedar-ridge and northwind-logistics: a heading + employer line with no
    bullet before the next heading, for a role that ended in 2024."""
    resume_md = dedent("""\
        # Alex J. Rivera

        ## Professional Experience

        ### Director of Operations
        **Contoso Health Analytics** | Oakbrook, OH | Nov 2024 - Dec 2027
        - Led a team of 19 specialists.

        ### Operations Manager
        **Contoso Health Analytics** | Oakbrook, OH | Jan 2021 - Oct 2024

        ### Operations Manager, Service Delivery
        **Lakeside Health Partners** | Elmcrest, OH | Sep 2020 - Dec 2020
        - Led 34 individual contributors and three team supervisors.
        """)
    findings = check_structure(resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY)
    empty = _codes(findings, "empty_recent_role")
    assert len(empty) == 1
    assert empty[0].severity is Severity.FATAL
    assert "Operations Manager" in empty[0].evidence


def test_early_career_tail_role_with_no_bullets_is_not_flagged():
    """Solstice Solutions ended Mar 2010, which is before the (2010, 9) cutoff for
    TODAY=2030-09-16 at the 20-year tail -- by six months, so this margin
    is deliberately thin rather than comfortably clear. No bullets there is
    the allowed early-career tail, not a defect."""
    resume_md = dedent("""\
        # Alex J. Rivera

        ## Professional Experience

        ### Database Team Manager
        **Solstice Solutions, Inc.** | Hartwell, OH | Oct 2003 - Mar 2010

        ### Director of Business Operations
        **Vertex Software, Inc.** | Fairhaven, OH | Mar 2010 - Jul 2015
        - Supported technical and financial diligence on prospective mergers and acquisitions.
        """)
    findings = check_structure(resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY)
    assert not _codes(findings, "empty_recent_role")
    assert not [f for f in findings if f.severity is Severity.FATAL]


# ---------------------------------------------------------------------------
# banned_characters (FATAL)
# ---------------------------------------------------------------------------


def test_banned_characters_detected_in_markdown_fallback():
    resume_md = dedent("""\
        # Alex J. Rivera

        ## Professional Experience

        ### Operations Manager, Service Delivery
        **Lakeside Health Partners** | Elmcrest, OH | Sep 2020 - Dec 2020
        - Owned the <Confidential> vendor contract and cut spend by 10%.
        """)
    findings = check_structure(resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY)
    banned = _codes(findings, "banned_characters")
    assert banned and banned[0].severity is Severity.FATAL
    assert "<Confidential>" in banned[0].evidence


@pytest.mark.parametrize("char", list('<>[]{}"\\'))
def test_each_logged_banned_character_is_flagged_on_the_markdown_path(char: str):
    """The exact set behind the Workday rejection ('Contains illegal
    characters', 2026-09-10, llm/prompts.py:195 / exporters/ats.py:19-30):
    < > [ ] { } " \\ -- every one of them, individually, must FATAL. A bare
    '[Confidential]' has no '(...)' after it, so `_MD_LINK_RE` does not match
    it and it must survive `_strip_markdown_syntax` to still be caught."""
    resume_md = dedent(f"""\
        # Alex J. Rivera

        ## Professional Experience

        ### Operations Manager, Service Delivery
        **Lakeside Health Partners** | Elmcrest, OH | Sep 2020 - Dec 2020
        - Owned the {char}Confidential{char} vendor contract and cut spend by 10%.
        """)
    findings = check_structure(resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY)
    banned = _codes(findings, "banned_characters")
    assert banned and banned[0].severity is Severity.FATAL
    assert char in banned[0].evidence


def test_backtick_is_not_a_banned_character():
    """No rejection evidence names a backtick -- flagging one invents a new
    FATAL with no basis in the real application path."""
    resume_md = dedent("""\
        # Alex J. Rivera

        ## Professional Experience

        ### Operations Manager, Service Delivery
        **Lakeside Health Partners** | Elmcrest, OH | Sep 2020 - Dec 2020
        - Owned the `Confidential` vendor contract and cut spend by 10%.
        """)
    findings = check_structure(resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY)
    assert not _codes(findings, "banned_characters")
    assert not [f for f in findings if f.severity is Severity.FATAL]


def test_markdown_link_in_source_markdown_is_not_flagged():
    """A real markdown link (e.g. a LinkedIn URL written as link syntax) is
    not a defect on the markdown path -- `_strip_markdown_syntax` resolving
    it to display text is intentional modeling of what the exporters ship."""
    resume_md = dedent("""\
        # Alex J. Rivera

        [My LinkedIn Profile](https://www.linkedin.com/in/example-user/)

        ## Professional Experience

        ### Operations Manager, Service Delivery
        **Lakeside Health Partners** | Elmcrest, OH | Sep 2020 - Dec 2020
        - Cut incident response times significantly.
        """)
    findings = check_structure(resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY)
    assert not _codes(findings, "banned_characters")
    assert not [f for f in findings if f.severity is Severity.FATAL]


def test_markdown_link_literal_in_exported_docx_is_flagged():
    """The same link syntax, if it somehow survived export into the actual
    .docx text the ATS receives, contains '[' and ']' -- banned characters --
    and is caught on the docx path via the banned-character set, not a
    dedicated link check."""
    resume_md = dedent("""\
        # Alex J. Rivera

        ## Professional Experience

        ### Operations Manager, Service Delivery
        **Lakeside Health Partners** | Elmcrest, OH | Sep 2020 - Dec 2020
        - Cut incident response times significantly.
        """)
    docx_lines = [
        "Alex J. Rivera",
        "[My LinkedIn Profile](https://www.linkedin.com/in/example-user/)",
        "Operations Manager, Service Delivery",
        "Cut incident response times significantly.",
    ]
    findings = check_structure(
        resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY, docx_lines=docx_lines
    )
    banned = _codes(findings, "banned_characters")
    assert banned and "[My LinkedIn Profile]" in banned[0].evidence


def test_banned_characters_checked_against_docx_lines_when_provided():
    """The markdown itself is clean; the (simulated) exported .docx text is
    not -- docx_lines must be what gets scanned, per V1 (verify the artifact,
    not the markdown)."""
    resume_md = dedent("""\
        # Alex J. Rivera

        ## Professional Experience

        ### Operations Manager, Service Delivery
        **Lakeside Health Partners** | Elmcrest, OH | Sep 2020 - Dec 2020
        - Cut incident response times significantly.
        """)
    docx_lines = [
        "Alex J. Rivera",
        "Operations Manager, Service Delivery",
        "Cut incident response {times} significantly.",
    ]
    findings = check_structure(
        resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY, docx_lines=docx_lines
    )
    banned = _codes(findings, "banned_characters")
    assert banned and "{times}" in banned[0].evidence


# ---------------------------------------------------------------------------
# encoding_damage (FATAL)
# ---------------------------------------------------------------------------


def test_encoding_damage_detects_replacement_character():
    resume_md = dedent("""\
        # Alex J. Rivera

        ## Professional Experience

        ### Operations Manager, Service Delivery
        **Lakeside Health Partners** | Elmcrest, OH | Sep 2020 - Dec 2020
        - Cut incident response times by 75�.
        """)
    findings = check_structure(resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY)
    damage = _codes(findings, "encoding_damage")
    assert damage and damage[0].severity is Severity.FATAL


# ---------------------------------------------------------------------------
# role_not_in_history (FATAL)
# ---------------------------------------------------------------------------


def test_role_not_in_history_flags_unknown_employer():
    resume_md = dedent("""\
        # Alex J. Rivera

        ## Professional Experience

        ### Director of Cloud Engineering
        **Totally Fabricated Corp** | Chicago, IL | Jan 2021 - Present
        - Built things that never happened.
        """)
    findings = check_structure(resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY)
    fatal = _codes(findings, "role_not_in_history")
    assert any("Totally Fabricated Corp" in f.evidence for f in fatal)


def test_role_not_in_history_flags_dates_that_contradict_work_history():
    resume_md = dedent("""\
        # Alex J. Rivera

        ## Professional Experience

        ### Operations Manager, Service Delivery
        **Lakeside Health Partners** | Elmcrest, OH | Sep 2020 - Dec 2022
        - Led 34 individual contributors and three team supervisors.
        """)
    findings = check_structure(resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY)
    fatal = _codes(findings, "role_not_in_history")
    assert fatal and "Lakeside" in fatal[0].message


def test_role_not_in_history_accepts_one_half_of_a_split_promotion():
    """A resume that mentions only the Operations Manager portion of
    Contoso must not be flagged just because history's combined span for
    that employer (across both its headings) runs later -- the individual
    heading it claims is itself real and unaltered."""
    resume_md = dedent("""\
        # Alex J. Rivera

        ## Professional Experience

        ### Operations Manager
        **Contoso Health Analytics** | Oakbrook, OH | Jan 2021 - Oct 2024
        - Redesigned and rebuilt the network.
        """)
    findings = check_structure(resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY)
    assert not _codes(findings, "role_not_in_history")


# ---------------------------------------------------------------------------
# skills_term_without_backing (MAJOR)
# ---------------------------------------------------------------------------


def test_skills_term_without_backing_flags_unbacked_keyword():
    """Demoted to MINOR, 2026-09-17: verbatim substring matching cannot see
    that 'Vendor Management' is backed by prose like 'vendor strategy and
    contract negotiations' -- the real northwind-logistics-2026-09-16 packet (a
    currently-shipping, correct resume) produced 17 of these on legitimate,
    categorically-backed terms. The user's 2026-09-15 'must have something
    behind it' standard is a quality preference, not an observed parse
    failure, so it does not belong at the same severity as a logged Workday
    rejection. `packet_gate.gate` only ever reads `report.fatal()`, so this
    demotion changes nothing about what quarantines -- it only stops
    teaching the user to ignore a checker that cries wolf on their own resume."""
    resume_md = dedent("""\
        # Alex J. Rivera

        ## Core Competencies

        - **Cloud Platforms:** AWS, Azure, Kubernetes

        ## Professional Experience

        ### Operations Manager, Service Delivery
        **Lakeside Health Partners** | Elmcrest, OH | Sep 2020 - Dec 2020
        - Led a team supporting AWS and Azure infrastructure across three hospitals.
        """)
    findings = check_structure(resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY)
    unbacked = _codes(findings, "skills_term_without_backing")
    assert unbacked and unbacked[0].severity is Severity.MINOR
    assert any(f.evidence == "Kubernetes" for f in unbacked)
    assert not any(f.evidence == "AWS" for f in unbacked)
    assert not any(f.evidence == "Azure" for f in unbacked)


def test_skills_term_with_nested_comma_in_parens_is_not_split_into_garbage():
    """'Hybrid Cloud (AWS, Azure)' was being comma-split into the malformed
    terms 'Hybrid Cloud (AWS' and 'Azure)' -- an unmatched paren in the
    evidence text, and a term that can never match its own backing. The
    split must respect parens so the parenthetical stays one candidate
    group, per the existing paren-handling in `_skills_terms`/`_check_skills_backing`."""
    resume_md = dedent("""\
        # Alex J. Rivera

        ## Core Competencies

        - **Technology Operations:** Hybrid Cloud (AWS, Azure), Network Engineering

        ## Professional Experience

        ### Operations Manager, Service Delivery
        **Lakeside Health Partners** | Elmcrest, OH | Sep 2020 - Dec 2020
        - Ran hybrid cloud infrastructure across AWS and Azure, and led network engineering.
        """)
    findings = check_structure(resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY)
    unbacked = _codes(findings, "skills_term_without_backing")
    assert not any(f.evidence == "Hybrid Cloud (AWS" for f in unbacked)
    assert not any(f.evidence == "Azure)" for f in unbacked)


# ---------------------------------------------------------------------------
# heading_carries_employer (MAJOR)
# ---------------------------------------------------------------------------


def test_heading_carries_employer_flags_pre_fix_one_line_format():
    resume_md = dedent("""\
        # Alex J. Rivera

        ## Professional Experience

        ### Operations Manager | Contoso Health Analytics | Oakbrook, OH | Jan 2021 - Oct 2024
        - Redesigned and rebuilt the network.
        """)
    findings = check_structure(resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY)
    major = _codes(findings, "heading_carries_employer")
    assert major and major[0].severity is Severity.MAJOR


# ---------------------------------------------------------------------------
# A clean, real, currently-shipping packet: zero FATAL findings
# ---------------------------------------------------------------------------


def test_clean_current_packet_has_zero_fatal_findings():
    findings = check_structure(CLEAN_RESUME_MD, work_history_md=WORK_HISTORY_MD, today=TODAY)
    fatal = [f for f in findings if f.severity is Severity.FATAL]
    assert fatal == []


def test_clean_current_packet_has_zero_fatal_findings_through_real_docx_export(tmp_path):
    """The full V1 loop: export the clean resume to a real .docx, read it
    back with docx_text, and check against that -- not the markdown."""
    docx_path = write_docx(CLEAN_RESUME_MD, tmp_path, stem="resume")
    docx_lines = extract_text(docx_path)
    assert docx_lines  # the export actually produced body text

    findings = check_structure(
        CLEAN_RESUME_MD, work_history_md=WORK_HISTORY_MD, today=TODAY, docx_lines=docx_lines
    )
    fatal = [f for f in findings if f.severity is Severity.FATAL]
    assert fatal == []


# ---------------------------------------------------------------------------
# core.docx_text
# ---------------------------------------------------------------------------


def test_extract_text_reads_body_paragraphs_in_order(tmp_path):
    md = dedent("""\
        # Alex J. Rivera

        Lakeview, OH | 614-555-0147 | alex.rivera@example.com

        ## Professional Experience

        ### Operations Manager, Service Delivery
        **Lakeside Health Partners** | Elmcrest, OH | Sep 2020 - Dec 2020
        - Led 34 individual contributors and three team supervisors.
        """)
    path = write_docx(md, tmp_path, stem="resume")
    lines = extract_text(path)
    assert "Alex J. Rivera" in lines
    assert any("Lakeside Health Partners" in line for line in lines)
    assert any("Led 34 individual contributors" in line for line in lines)
    # Body order is preserved: the name comes before the role, which comes
    # before its bullet.
    name_idx = lines.index("Alex J. Rivera")
    role_idx = next(i for i, line in enumerate(lines) if "Lakeside Health Partners" in line)
    bullet_idx = next(i for i, line in enumerate(lines) if "Led 34 individual" in line)
    assert name_idx < role_idx < bullet_idx


def test_extract_lines_tags_a_plain_export_as_body_only(tmp_path):
    md = "# Alex J. Rivera\n\n## Experience\n\n### Role\n**Employer** | City, ST | Jan 2020 - Present\n- Did the thing.\n"
    path = write_docx(md, tmp_path, stem="resume")
    tagged = extract_lines(path)
    assert tagged
    assert all(line.region is DocxRegion.BODY for line in tagged)


# ---------------------------------------------------------------------------
# empty_recent_role scoping (issue #39): only roles inside Experience count
# ---------------------------------------------------------------------------


def test_headings_outside_experience_are_not_roles():
    resume_md = dedent("""\
        # Alex J. Rivera

        ## Professional Experience

        ### Director of Business Operations
        **Northwind Logistics** | Fairhaven, OH | Jan 2029 - Present
        - Run technology strategy and delivery.

        ## Selected Projects

        ### Selected Projects
        Built an internal automation framework used company-wide.

        ## Skills

        ### Technical Skills
        * Python, SQL
        * Cloud platforms
        """)
    findings = check_structure(resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY)
    assert not _codes(findings, "empty_recent_role")


def test_bullet_markers_star_and_dot_count_as_bullets_in_experience():
    resume_md = dedent("""\
        # Alex J. Rivera

        ## Work Experience

        ### Director of Business Operations
        **Northwind Logistics** | Fairhaven, OH | Jan 2029 - Present
        • Run technology strategy and delivery.

        ### Director of Operations
        **Contoso Health Analytics** | Oakbrook, OH | Nov 2024 - Dec 2027
        * Led a team of 19 specialists.
        """)
    findings = check_structure(resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY)
    assert not _codes(findings, "empty_recent_role")


def test_empty_recent_role_inside_experience_is_still_fatal_with_other_sections():
    resume_md = dedent("""\
        # Alex J. Rivera

        ## Experience

        ### Operations Manager
        **Contoso Health Analytics** | Oakbrook, OH | Jan 2021 - Oct 2024

        ## Skills

        ### Technical Skills
        * Python, SQL
        """)
    findings = check_structure(resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY)
    empty = _codes(findings, "empty_recent_role")
    assert len(empty) == 1
    assert empty[0].severity is Severity.FATAL
    assert "Operations Manager" in empty[0].evidence


def test_no_experience_section_falls_back_to_whole_document():
    resume_md = dedent("""\
        # Alex J. Rivera

        ## Roles

        ### Operations Manager
        **Contoso Health Analytics** | Oakbrook, OH | Jan 2021 - Oct 2024
        """)
    findings = check_structure(resume_md, work_history_md=WORK_HISTORY_MD, today=TODAY)
    assert len(_codes(findings, "empty_recent_role")) == 1

# ruff: noqa: E501 -- the fixtures are verbatim resume and work-history lines;
# rewrapping them would change exactly what is under test.
"""Employment-date verification.

The fixtures below mirror the real numbers from a 2026-09-10 Meridian
Industries build (a fictional candidate's build, not invented numbers chosen
to fit the implementation). That build carried seven date ranges, six of them
fabricated, and the LLM groundedness pass scored it 62/100 while flagging
thirteen prose claims and zero wrong dates. Anchoring the tests to a real
failure shape is the point: a fixture written to match the implementation
would have passed then too.
"""

from __future__ import annotations

from blastjob.core import resume_dates as rd
from blastjob.core.scoring import parse_score

# Trimmed from the live work_history.md. Note Contoso's heading carries TWO
# ranges because of an internal promotion -- the tenure is the full span.
WORK_HISTORY = """\
## Experience

### Director of Business Operations | Northwind Logistics (Crestline Holdings / Meridian Group) | Fairhaven, OH (Jan 2029 – Present)
- Own enterprise ops strategy.

### Director of Security and Business Operations / vCOO | Lakeshore Total IT | Oakbrook, OH (Apr 2028 – Jan 2029)
- Led engineering delivery.

### Director of Operations (Nov 2024 – Dec 2027) / Systems Infrastructure Lead (Jan 2021 – Oct 2024) | Contoso Health Analytics | Oakbrook, OH
- Achieved HITRUST certification.

### Operations Manager, Service Delivery | Lakeside Health Partners | Elmcrest, OH (Sep 2020 – Dec 2020)
- Ran technical services.

### Director of Business Operations | Vertex Software, Inc. | Fairhaven, OH (Mar 2010 – Jul 2015)
- Set infrastructure vision.
"""


def test_parses_a_promotion_heading_as_the_full_tenure():
    """Contoso's heading holds two ranges. Reading only the first reported a
    correct resume as wrong -- a false positive found against real data."""
    span = rd.parse_range(
        "### Director of Operations (Nov 2024 – Dec 2027) / "
        "Systems Infrastructure Lead (Jan 2021 – Oct 2024) | Contoso Health Analytics"
    )
    assert str(span) == "Jan 2021 – Dec 2027"


def test_parses_the_formats_that_actually_appear():
    assert str(rd.parse_range("| Jan 2029 – Present")) == "Jan 2029 – Present"
    assert str(rd.parse_range("Elmcrest, OH | 2009–2011")) == "2009 – 2011"
    assert str(rd.parse_range("Oct 2001 to Jun 2002")) == "Oct 2001 – Jun 2002"
    assert str(rd.parse_range("Mar 2010 - Jul 2015")) == "Mar 2010 – Jul 2015"
    assert rd.parse_range("no dates on this line") is None
    assert rd.parse_range("Reduced costs by 30% across 2 sites") is None


def test_catches_the_real_meridian_fabrications():
    """Verbatim from the 2026-09-10 Meridian Industries resume. Two four-month roles were
    inflated into two-year roles."""
    resume = """\
### Director of Business Operations — Northwind Logistics (Crestline Holdings / Meridian Group) | Fairhaven, OH | 2028–Present
### Director of Operations / Systems Infrastructure Lead — Contoso Health Analytics | Oakbrook, OH | 2021–2027
### Operations Manager, Service Delivery — Lakeside Health Partners | Elmcrest, OH | 2019–2021
### Director of Business Operations — Vertex Software, Inc. | Fairhaven, OH | 2010–2013
"""
    bad, _ = rd.verify(resume, WORK_HISTORY)
    by_employer = {f.employer: f for f in bad}

    # Contoso is the one the model got right, because it was the only employer
    # with dates in the source at the time. It must NOT be flagged.
    assert "Contoso Health Analytics" not in by_employer

    assert set(by_employer) == {
        "Northwind Logistics",
        "Lakeside Health Partners",
        "Vertex Software, Inc.",
    }
    assert by_employer["Lakeside Health Partners"].claimed == "2019 – 2021"
    assert by_employer["Lakeside Health Partners"].actual == "Sep 2020 – Dec 2020"


def test_a_correct_resume_produces_no_findings():
    resume = """\
### Director of Business Operations — Northwind Logistics (Crestline Holdings / Meridian Group) | Fairhaven, OH | Jan 2029 – Present
### Director of Security and Business Operations / vCOO — Lakeshore Total IT | Oakbrook, OH | Apr 2028 – Jan 2029
### Director of Operations (Nov 2024 – Dec 2027) / Systems Infrastructure Lead (Jan 2021 – Oct 2024) — Contoso Health Analytics | Oakbrook, OH
### Operations Manager, Service Delivery — Lakeside Health Partners | Elmcrest, OH | Sep 2020 – Dec 2020
### Director of Business Operations — Vertex Software, Inc. | Fairhaven, OH | Mar 2010 – Jul 2015
"""
    bad, undated = rd.verify(resume, WORK_HISTORY)
    assert bad == []
    assert undated == []


def test_a_year_only_resume_date_is_not_a_contradiction():
    """Vagueness is forgiven; conflict is not. "2029" is satisfied by
    "Jan 2029" because the year is all the resume claimed."""
    resume = (
        "### Director of Business Operations — Northwind Logistics | Fairhaven, OH | 2029–Present\n"
    )
    bad, _ = rd.verify(resume, WORK_HISTORY)
    assert bad == []


def test_a_wrong_month_in_the_same_year_is_a_contradiction():
    resume = "### Director of Business Operations — Vertex Software, Inc. | Fairhaven, OH | Jul 2010 – Jul 2015\n"
    bad, _ = rd.verify(resume, WORK_HISTORY)
    assert [f.employer for f in bad] == ["Vertex Software, Inc."]


def test_reports_employers_shown_without_any_dates():
    resume = """\
### Director of Business Operations — Northwind Logistics | Fairhaven, OH | Jan 2029 – Present
### Director of Business Operations — Vertex Software, Inc. | Fairhaven, OH
"""
    bad, undated = rd.verify(resume, WORK_HISTORY)
    assert bad == []
    assert undated == ["Vertex Software, Inc."]


def test_an_employer_absent_from_the_resume_is_not_reported():
    resume = "### Director of Business Operations — Northwind Logistics | Fairhaven, OH | Jan 2029 – Present\n"
    _, undated = rd.verify(resume, WORK_HISTORY)
    assert undated == []


def test_parse_score_counts_a_bad_date_against_groundedness():
    """The whole point: a fabricated date must reach the fit report."""
    model_output = """{
      "jd_alignment_score": 90,
      "summary": "Looks strong.",
      "claims": [
        {"claim_text": "Achieved HITRUST certification.", "grounded": true,
         "evidence_quote": "Achieved HITRUST certification.", "source_section": "Contoso"}
      ]
    }"""
    resume = "### Operations Manager, Service Delivery — Lakeside Health Partners | Elmcrest, OH | 2019–2021\n"

    clean = parse_score(model_output, WORK_HISTORY)
    assert clean.unsupported_count == 0
    assert clean.groundedness_score == 100

    checked = parse_score(model_output, WORK_HISTORY, resume)
    assert checked.unsupported_count == 1
    assert checked.groundedness_score < 100
    assert any("Lakeside Health Partners" in c.claim_text for c in checked.claims if not c.grounded)
    assert "contradict the work history" in checked.summary


def test_a_promotion_split_into_two_entries_is_not_a_contradiction():
    """An ATS parses a promotion best as two entries, one per title. The two
    ranges must be unioned before comparing -- reading only the first line
    reported a correctly formatted resume as wrong."""
    resume = """\
### Director of Operations
**Contoso Health Analytics** | Oakbrook, OH | Nov 2024 – Dec 2027
- Achieved HITRUST certification.

### Systems Infrastructure Lead
**Contoso Health Analytics** | Oakbrook, OH | Jan 2021 – Oct 2024
- Promoted to Director of Operations in Nov 2024.
"""
    bad, undated = rd.verify(resume, WORK_HISTORY)
    assert bad == []
    assert "Contoso Health Analytics" not in undated


def test_a_split_promotion_that_understates_the_span_is_still_caught():
    """Splitting must not become a way to hide a wrong date."""
    resume = """\
### Director of Operations
**Contoso Health Analytics** | Oakbrook, OH | Nov 2024 – Dec 2027

### Systems Infrastructure Lead
**Contoso Health Analytics** | Oakbrook, OH | Jan 2023 – Oct 2024
"""
    bad, _ = rd.verify(resume, WORK_HISTORY)
    assert [f.employer for f in bad] == ["Contoso Health Analytics"]
    assert bad[0].claimed == "Jan 2023 – Dec 2027"

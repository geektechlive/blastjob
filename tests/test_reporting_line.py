"""Who a role reports to, read from the description text (issue #82)."""

from __future__ import annotations

import pytest

from blastjob.core.reporting_line import ReportingLine, extract_reporting_line


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("This role reports to the Chief Executive Officer.", "CEO"),
        ("Reports to: CEO", "CEO"),
        ("The VP of IT role reports directly to the Chief Financial Officer (CFO).", "CFO"),
        ("You will report to our COO and lead a team.", "COO"),
        ("Position reports into the CIO.", "CIO"),
        ("Reporting to the CTO, you will own the platform.", "CTO"),
        ("Acme, Inc. is hiring. Reporting to the President, you set direction.", "President"),
        ("Reports to the Owner and the board.", "Owner and the board"),
        ("Reporting to the VP of Operations, you run the data team.", "VP of Operations"),
        ("This position reports to the Senior Vice President of Finance.", "SVP of Finance"),
        (
            "Reports to the Managing Director of Technology Services.",
            "Managing Director of Technology Services",
        ),
    ],
)
def test_explicit_reports_to_is_normalized(text: str, expected: str) -> None:
    assert extract_reporting_line(text).reports_to == expected


def test_long_unknown_phrase_is_trimmed_not_dropped() -> None:
    text = "Reports to the Senior Executive Director of Enterprise Systems and Digital Programs."
    got = extract_reporting_line(text).reports_to
    assert got is not None
    assert got.startswith("Senior Executive Director")
    assert len(got) <= 43  # 40 plus the trailing marker


def test_executive_team_membership_is_detected() -> None:
    text = "You will be a member of the executive leadership team."
    line = extract_reporting_line(text)
    assert line.exec_team is True
    assert line.reports_to is None
    assert line.label == "Executive team"


def test_label_joins_both_facts() -> None:
    text = "Reports to the CFO. Join our senior leadership team."
    assert extract_reporting_line(text).label == "Reports to CFO, executive team"


def test_no_mention_is_unknown() -> None:
    line = extract_reporting_line("Lead our infrastructure group. Great benefits.")
    assert line == ReportingLine(reports_to=None, exec_team=False)
    assert line.label == ""


@pytest.mark.parametrize("text", [None, "", "   "])
def test_empty_description_is_unknown(text: str | None) -> None:
    assert extract_reporting_line(text).label == ""


@pytest.mark.parametrize(
    "text",
    [
        "You will have 5 direct reports.",
        "Five direct reports to this role, including two managers.",
        "The network team reports to the CIO.",
        "Team members will report to the Director of IT.",
        "Our analysts report to you and sit in Madison.",
        "Reports to you: a team of six engineers.",
        "You manage staff who report to this position.",
        "Collaborate with the executive team on planning.",
        "Present quarterly results to the executive leadership team.",
    ],
)
def test_subordinates_and_collaboration_are_not_matches(text: str) -> None:
    assert extract_reporting_line(text).label == ""


def test_first_valid_match_wins_after_a_rejected_one() -> None:
    text = "Direct reports to this role: 4. The role reports to the COO."
    assert extract_reporting_line(text).reports_to == "COO"


def test_multiline_bullet_form() -> None:
    text = "Details\n- Location: Remote\n- Reports to: Chief Operating Officer\n- Travel: 10%"
    assert extract_reporting_line(text).reports_to == "COO"

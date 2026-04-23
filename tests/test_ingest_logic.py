"""Unit tests for pure functions in core/ingest.py."""

import json

import pytest

from blastjob.core.ingest import _emit_summary, _parse_history, _serialize_history, _split_templates
from blastjob.models.history import (
    Certification,
    DateRange,
    Education,
    Experience,
    MasterHistory,
    Project,
)

# ---------------------------------------------------------------------------
# _parse_history
# ---------------------------------------------------------------------------


def _minimal_history_json(**overrides) -> str:
    data = {
        "name": "Jane Doe",
        "email": "jane@example.com",
        "phone": "555-1234",
        "location": "San Francisco, CA",
        "linkedin": "",
        "github": "",
        "summary": "Experienced engineer.",
        "experience": [],
        "education": [],
        "skills": [],
        "projects": [],
        "certifications": [],
    }
    data.update(overrides)
    return json.dumps(data)


def test_parse_history_minimal():
    text = _minimal_history_json()
    result = _parse_history(text)
    assert result.name == "Jane Doe"
    assert result.email == "jane@example.com"


def test_parse_history_with_surrounding_text():
    json_blob = _minimal_history_json(name="Bob Smith")
    text = f"Some preamble text\n\n{json_blob}\n\nSome trailing text."
    result = _parse_history(text)
    assert result.name == "Bob Smith"


def test_parse_history_no_json_raises():
    with pytest.raises(ValueError, match="no JSON object"):
        _parse_history("There is no JSON here at all.")


def test_parse_history_experience():
    data = {
        "name": "Alice",
        "email": "",
        "phone": "",
        "location": "",
        "linkedin": "",
        "github": "",
        "summary": "",
        "experience": [
            {
                "title": "Engineer",
                "company": "Acme",
                "dates": {"start": "2020", "end": "2023"},
                "location": "Remote",
                "bullets": ["Built things", "Fixed bugs"],
            }
        ],
        "education": [],
        "skills": ["Python", "Go"],
        "projects": [],
        "certifications": [],
    }
    result = _parse_history(json.dumps(data))
    assert len(result.experience) == 1
    assert result.experience[0].title == "Engineer"
    assert result.experience[0].company == "Acme"
    assert result.experience[0].dates.start == "2020"
    assert len(result.skills) == 2


# ---------------------------------------------------------------------------
# _serialize_history
# ---------------------------------------------------------------------------


def _make_history(**kwargs) -> MasterHistory:
    defaults = {
        "name": "Jane Doe",
        "email": "jane@example.com",
        "phone": "555-1234",
        "location": "SF, CA",
    }
    defaults.update(kwargs)
    return MasterHistory(**defaults)


def test_serialize_history_frontmatter():
    h = _make_history()
    out = _serialize_history(h)
    assert out.startswith("---\n")
    assert "name: Jane Doe" in out
    assert "email: jane@example.com" in out
    assert "---" in out


def test_serialize_history_optional_links():
    h = _make_history(linkedin="https://linkedin.com/in/jane", github="")
    out = _serialize_history(h)
    assert "linkedin:" in out
    assert "github:" not in out


def test_serialize_history_summary():
    h = _make_history(summary="A great engineer.")
    out = _serialize_history(h)
    assert "## Summary" in out
    assert "A great engineer." in out


def test_serialize_history_experience():
    exp = Experience(
        title="Senior Engineer",
        company="Megacorp",
        dates=DateRange(start="2019", end="2023"),
        bullets=["Shipped product"],
    )
    h = _make_history(experience=[exp])
    out = _serialize_history(h)
    assert "## Experience" in out
    assert "Senior Engineer" in out
    assert "Megacorp" in out
    assert "- Shipped product" in out


def test_serialize_history_skills():
    h = _make_history(skills=["Python", "Rust", "SQL"])
    out = _serialize_history(h)
    assert "## Skills" in out
    assert "Python, Rust, SQL" in out


def test_serialize_history_certifications():
    cert = Certification(name="AWS Solutions Architect", issuer="Amazon", date="2022")
    h = _make_history(certifications=[cert])
    out = _serialize_history(h)
    assert "## Certifications" in out
    assert "AWS Solutions Architect" in out
    assert "Amazon" in out


def test_serialize_history_education():
    edu = Education(
        degree="BS Computer Science",
        institution="State University",
        dates=DateRange(start="2012", end="2016"),
    )
    h = _make_history(education=[edu])
    out = _serialize_history(h)
    assert "## Education" in out
    assert "BS Computer Science" in out


def test_serialize_history_projects():
    proj = Project(
        name="BlastJob",
        description="A resume tool.",
        bullets=["Built TUI"],
        technologies=["Python", "Textual"],
    )
    h = _make_history(projects=[proj])
    out = _serialize_history(h)
    assert "## Projects" in out
    assert "BlastJob" in out
    assert "Python, Textual" in out


def test_serialize_history_github_field():
    h = _make_history(github="https://github.com/jane")
    out = _serialize_history(h)
    assert "github: https://github.com/jane" in out


def test_serialize_history_education_with_details():
    edu = Education(
        degree="BS Computer Science",
        institution="State University",
        dates=DateRange(start="2012", end="2016"),
        details=["GPA: 3.9", "Dean's List"],
    )
    h = _make_history(education=[edu])
    out = _serialize_history(h)
    assert "- GPA: 3.9" in out
    assert "- Dean's List" in out


def test_serialize_history_experience_with_location():
    exp = Experience(
        title="Engineer",
        company="Acme",
        location="New York, NY",
        bullets=[],
    )
    h = _make_history(experience=[exp])
    out = _serialize_history(h)
    assert "New York, NY" in out


def test_serialize_history_roundtrip():
    exp = Experience(
        title="Staff Engineer",
        company="FooCorp",
        dates=DateRange(start="2018", end="2024"),
        location="NYC",
        bullets=["Led team", "Built platform"],
    )
    h = _make_history(experience=[exp], skills=["Kotlin", "Terraform"])
    out = _serialize_history(h)
    assert "Staff Engineer" in out
    assert "Kotlin, Terraform" in out


# ---------------------------------------------------------------------------
# _split_templates
# ---------------------------------------------------------------------------


def test_split_templates_with_delimiter():
    raw = "===STANDARD===\nStandard template content here.\n===ATS===\nATS template content here."
    standard, ats = _split_templates(raw)
    assert "Standard template content here." in standard
    assert "ATS template content here." in ats
    assert "===STANDARD===" not in standard
    assert "===ATS===" not in ats


def test_split_templates_without_delimiter():
    raw = "Just some template content with no delimiter."
    standard, ats = _split_templates(raw)
    assert "Just some template content" in standard
    assert ats == ""


def test_split_templates_strips_whitespace():
    raw = "  ===STANDARD===\n  content  \n===ATS===\n  ats content  "
    standard, ats = _split_templates(raw)
    assert standard == "content"
    assert ats == "ats content"


# ---------------------------------------------------------------------------
# _emit_summary
# ---------------------------------------------------------------------------


def test_emit_summary_collects_output():
    exp = Experience(title="Engineer", company="Acme", dates=DateRange(start="2020", end="2023"))
    h = _make_history(
        name="Bob",
        experience=[exp],
        skills=["Python", "Go"],
        certifications=[Certification(name="AWS")],
    )
    collected: list[str] = []
    _emit_summary(h, lambda msg: collected.append(msg))
    combined = "".join(collected)
    assert "Bob" in combined
    assert "1" in combined  # roles found
    assert "Engineer" in combined


def test_emit_summary_no_name():
    h = MasterHistory()
    collected: list[str] = []
    _emit_summary(h, lambda msg: collected.append(msg))
    combined = "".join(collected)
    assert "(not found)" in combined

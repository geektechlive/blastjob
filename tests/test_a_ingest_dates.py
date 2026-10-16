"""Group A, A2: ingest writes dates on the line AFTER `### Title | Company`; the
date parsers must read them there, or the FATAL date check never runs on an
ingested history."""

from __future__ import annotations

from datetime import date

from blastjob.core import resume_dates
from blastjob.core.ingest import _serialize_history
from blastjob.core.resume_format import _history_ranges, check_structure
from blastjob.models.history import DateRange, Experience, MasterHistory


def _ingested_history() -> str:
    return _serialize_history(
        MasterHistory(
            name="Jane Doe",
            experience=[
                Experience(
                    title="VP of IT",
                    company="Example Widgets",
                    location="Springfield, WI",
                    dates=DateRange(start="Jan 2020", end="Dec 2023"),
                    bullets=["Ran IT."],
                ),
                Experience(
                    title="Director",
                    company="Sample Corp",
                    dates=DateRange(start="2016", end="Present"),
                    bullets=["Ran ops."],
                ),
                # No dates at all: the next line is a bullet and must never be read as dates.
                Experience(
                    title="Analyst", company="Undated Inc", bullets=["Between 2001 - 2003."]
                ),
            ],
        )
    )


def test_serialized_history_puts_dates_on_the_line_after_the_heading():
    history = _ingested_history()

    assert "### VP of IT | Example Widgets | Springfield, WI\n*Jan 2020 - Dec 2023*" in history


def test_history_dates_reads_the_line_after_an_ingested_heading():
    truth = resume_dates.history_dates(_ingested_history())

    span = truth["example widgets"] if "example widgets" in truth else next(iter(truth.values()))
    assert str(span) == "Jan 2020 – Dec 2023"
    assert len(truth) == 2


def test_a_bullet_line_is_never_read_as_the_roles_dates():
    truth = resume_dates.history_dates(_ingested_history())

    assert not any("2001" in str(v) for v in truth.values())


def test_resume_format_history_ranges_read_the_next_line_too():
    ranges = _history_ranges(_ingested_history())

    assert sorted(len(v) for v in ranges.values()) == [1, 1]


def test_verify_flags_a_resume_that_contradicts_an_ingested_history():
    resume = (
        "### VP of IT\n**Example Widgets** | Springfield, WI | Jan 2017 - Dec 2023\n- Ran IT.\n"
    )

    findings, _undated = resume_dates.verify(resume, _ingested_history())

    assert [f.employer for f in findings] == ["Example Widgets"]


def test_structure_check_raises_the_fatal_date_finding_on_an_ingested_history():
    resume = (
        "# Jane Doe\n\n## Experience\n\n### VP of IT\n"
        "**Example Widgets** | Springfield, WI | Jan 2017 - Dec 2023\n- Ran IT.\n"
    )

    findings = check_structure(
        resume, work_history_md=_ingested_history(), today=date(2026, 10, 16)
    )

    assert any(f.code == "role_not_in_history" and f.severity.value == "fatal" for f in findings)


def test_heading_dates_still_win_when_present():
    history = "### VP of IT | Example Widgets (Jan 2020 - Dec 2023)\n- Ran IT.\n"

    truth = resume_dates.history_dates(history)

    assert [str(v) for v in truth.values()] == ["Jan 2020 – Dec 2023"]

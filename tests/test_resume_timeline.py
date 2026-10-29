"""Claims that contradict their role dates (#105).

Groundedness proves a claim is quoted from the work history. It never asked
whether the claim fits the dates of the role it sits under. These tests pin the
deterministic checks (no model anywhere) that do. Every employer, title and
date is invented.
"""

from __future__ import annotations

from datetime import date

import pytest

from blastjob.core.packet_report import Severity
from blastjob.core.resume_dates import (
    month_indices,
    parse_range,
    parse_ranges,
    role_span_months,
)
from blastjob.core.resume_timeline_checks import (
    TIMELINE_CODES,
    check_history,
    check_resume,
)

TODAY = date(2026, 10, 29)


def _range(text: str):
    rng = parse_range(text)
    assert rng is not None
    return rng


# ----- parser support --------------------------------------------------------


def test_parse_ranges_returns_each_range_of_a_promotion_heading():
    heading = "Director of Operations (Nov 2023 - Dec 2026) / Operations Lead (Jan 2020 - Oct 2023)"
    ranges = parse_ranges(heading)
    assert [str(r) for r in ranges] == ["Nov 2023 – Dec 2026", "Jan 2020 – Oct 2023"]


def test_parse_range_still_returns_the_union():
    heading = "Director of Operations (Nov 2023 - Dec 2026) / Operations Lead (Jan 2020 - Oct 2023)"
    assert str(parse_range(heading)) == "Jan 2020 – Dec 2026"


def test_parse_ranges_is_empty_without_a_range():
    assert parse_ranges("no dates here") == []


@pytest.mark.parametrize(
    ("text", "months"),
    [
        ("Jan 2020 - Dec 2022", 36),
        ("2020 - 2022", 36),  # year-only: Jan start, Dec end (benefit of the doubt)
        ("Mar 2026 - Present", 8),  # Mar..Oct 2026 inclusive
        ("Jun 2020 - Sep 2020", 4),
    ],
)
def test_role_span_months_is_inclusive_with_benefit_of_the_doubt(text, months):
    assert role_span_months(_range(text), TODAY) == months


@pytest.mark.parametrize(
    "text", ["Dec 2022 - Jan 2020", "Jan 2030 - Dec 2031", "Jan 2020 - Dec 2030"]
)
def test_role_span_months_is_none_for_reversed_or_future_ranges(text):
    assert role_span_months(_range(text), TODAY) is None
    assert month_indices(_range(text), TODAY) is None


# ----- fixtures --------------------------------------------------------------


def _history(*roles: tuple[str, str, str, list[str]]) -> str:
    parts = ["## Experience", ""]
    for title, employer, dates, bullets in roles:
        parts += [f"### {title} | {employer} | Oakbrook, OH", f"*{dates}*"]
        parts += [f"- {b}" for b in bullets]
        parts.append("")
    return "\n".join(parts)


def _resume(*roles: tuple[str, str, str, list[str]]) -> str:
    parts = ["# Alex J. Rivera", "", "## Experience", ""]
    for title, employer, dates, bullets in roles:
        parts += [f"### {title}", f"**{employer}** | Oakbrook, OH | {dates}"]
        parts += [f"- {b}" for b in bullets]
        parts.append("")
    return "\n".join(parts)


def _one(bullet: str, dates: str = "Jan 2019 - Dec 2021", employer: str = "Contoso Health"):
    role = ("Director of Operations", employer, dates, [bullet])
    return _resume(role), _history(role)


def _codes(findings, code: str):
    return [f for f in findings if f.code == code]


def _run(bullet: str, dates: str = "Jan 2019 - Dec 2021"):
    resume, history = _one(bullet, dates)
    return check_resume(resume, history, today=TODAY)


# ----- duration_exceeds_role -------------------------------------------------


def test_within_five_years_under_a_three_year_role_fires():
    found = _codes(_run("Built the platform within five years."), "duration_exceeds_role")
    assert len(found) == 1
    assert found[0].severity is Severity.MAJOR
    assert found[0].line is not None


@pytest.mark.parametrize(
    "bullet",
    [
        "Built the platform within 5 years.",
        "Shipped the program over 60 months.",
        "Led a 5-year migration.",
        "Delivered 5 years of savings.",
        "Grew the team over the past five years.",
    ],
)
def test_other_elapsed_forms_fire_in_digits_and_words(bullet):
    assert _codes(_run(bullet), "duration_exceeds_role")


@pytest.mark.parametrize(
    "bullet",
    [
        "Built the platform within three years.",
        "Cut costs in 38 months.",  # inside the 3-month tolerance
        "Defined a 3- and 5-year roadmap.",
        "Wrote a 5-year strategy.",
        "Signed a 5-year agreement.",
        "Retired a 10-year-old system.",
        "Mentored a team with 15 years of experience.",
        "Saved money over 2.5 years.",
        "Reached it in 3 to 5 years.",
    ],
)
def test_durations_that_fit_or_are_not_elapsed_time_do_not_fire(bullet):
    assert not _codes(_run(bullet), "duration_exceeds_role")


def _promo_pair(later_bullet: str, earlier=("Jan 2016 - Dec 2018",), later="Jan 2019 - Dec 2021"):
    roles = [("Operations Lead", "Contoso Health", dates, ["Ran the floor."]) for dates in earlier]
    roles.append(("Director of Operations", "Contoso Health", later, [later_bullet]))
    return _resume(roles[-1], *roles[:-1]), _history(roles[-1], *roles[:-1])


def test_across_both_roles_is_exempt_next_to_an_adjacent_same_employer_role():
    resume, history = _promo_pair("Across both roles, built the platform within six years.")
    assert not _codes(check_resume(resume, history, today=TODAY), "duration_exceeds_role")


def test_across_both_roles_still_fires_without_an_adjacent_same_employer_role():
    found = _run("Across both roles, built the platform within six years.")
    assert _codes(found, "duration_exceeds_role")


def test_a_gapped_pair_does_not_satisfy_over_five_years():
    resume, history = _promo_pair(
        "Across both roles, delivered the program over five years.",
        earlier=("Jan 2015 - Dec 2016",),
        later="Jan 2019 - Dec 2020",
    )
    assert _codes(check_resume(resume, history, today=TODAY), "duration_exceeds_role")


def _three_titles(bullet: str) -> tuple[str, str]:
    roles = [
        ("VP of Operations", "Contoso Health", "Jan 2019 - Dec 2021", [bullet]),
        ("Director of Operations", "Contoso Health", "Jan 2016 - Dec 2018", ["Ran sites."]),
        ("Operations Lead", "Contoso Health", "Jan 2013 - Dec 2015", ["Ran the floor."]),
    ]
    return _resume(*roles), _history(*roles)


def test_a_chain_of_three_titles_satisfies_over_my_nine_years_at():
    resume, history = _three_titles("Delivered savings over my nine years at Contoso Health.")
    assert not _codes(check_resume(resume, history, today=TODAY), "duration_exceeds_role")


def test_across_both_roles_means_exactly_one_neighbour_not_the_whole_chain():
    resume, history = _three_titles("Across both roles, delivered savings over nine years.")
    assert _codes(check_resume(resume, history, today=TODAY), "duration_exceeds_role")


# ----- result_after_role_end -------------------------------------------------


def test_a_result_year_after_the_role_ended_fires():
    found = _codes(
        _run("By 2019, reducing costs by a third.", "Jan 2013 - Dec 2016"),
        "result_after_role_end",
    )
    assert len(found) == 1
    assert found[0].severity is Severity.MAJOR
    assert "2019" in found[0].message and "2016" in found[0].message


def test_a_present_role_never_fires_result_after_role_end():
    assert not _codes(
        _run("By 2019, reducing costs.", "Jan 2019 - Present"), "result_after_role_end"
    )


def test_a_year_equal_to_a_year_only_end_never_fires():
    assert not _codes(
        _run("In 2016, cut costs by a third.", "2013 - 2016"), "result_after_role_end"
    )


def test_a_month_after_the_end_month_in_the_same_year_fires():
    late = _run("In Sep 2016, cut costs by a third.", "Jan 2013 - Jun 2016")
    early = _run("In Mar 2016, cut costs by a third.", "Jan 2013 - Jun 2016")
    assert _codes(late, "result_after_role_end")
    assert not _codes(early, "result_after_role_end")


@pytest.mark.parametrize(
    "bullet",
    [
        "Wrote the 2025 roadmap for the data center.",
        "Set a plan to retire the site by 2025.",
        "Migrated 40 hosts to Windows Server 2019.",
        "Set a goal for 2030.",
    ],
)
def test_plan_version_and_future_contexts_are_exempt(bullet):
    assert not _codes(_run(bullet, "Jan 2013 - Dec 2016"), "result_after_role_end")


# ----- promotion_line_dropped ------------------------------------------------

PROMO_SOURCE = (
    "Promoted from Operations Lead to Director of Operations after a first year of growth."
)


def _promo_history() -> str:
    return _history(
        (
            "Director of Operations",
            "Contoso Health",
            "Jan 2019 - Dec 2021",
            [PROMO_SOURCE, "Ran it."],
        ),
        ("Operations Lead", "Contoso Health", "Jan 2016 - Dec 2018", ["Ran the floor."]),
        ("Analyst", "Northwind Traders", "Jan 2012 - Dec 2015", ["Counted stock."]),
    )


def _promo_resume(director_bullets: list[str], lead_bullets=("Ran the floor.",)) -> str:
    return _resume(
        ("Director of Operations", "Contoso Health", "Jan 2019 - Dec 2021", director_bullets),
        ("Operations Lead", "Contoso Health", "Jan 2016 - Dec 2018", list(lead_bullets)),
        ("Analyst", "Northwind Traders", "Jan 2012 - Dec 2015", ["Counted stock."]),
    )


def test_a_dropped_promotion_line_fires():
    found = _codes(
        check_resume(_promo_resume(["Ran it."]), _promo_history(), today=TODAY),
        "promotion_line_dropped",
    )
    assert len(found) == 1 and found[0].severity is Severity.MAJOR


def test_a_present_first_bullet_naming_both_titles_passes():
    resume = _promo_resume(
        ["Promoted from Operations Lead to Director of Operations in 2019.", "Ran it."]
    )
    assert not _codes(check_resume(resume, _promo_history(), today=TODAY), "promotion_line_dropped")


def test_a_promotion_line_in_the_wrong_position_fires():
    resume = _promo_resume(["Ran it.", "Promoted from Operations Lead to Director of Operations."])
    assert _codes(check_resume(resume, _promo_history(), today=TODAY), "promotion_line_dropped")


def test_a_promotion_line_under_the_wrong_role_fires():
    resume = _promo_resume(
        ["Ran it."],
        lead_bullets=["Promoted from Operations Lead to Director of Operations."],
    )
    assert _codes(check_resume(resume, _promo_history(), today=TODAY), "promotion_line_dropped")


def test_a_promotion_line_under_the_wrong_employer_fires():
    resume = _resume(
        ("Director of Operations", "Contoso Health", "Jan 2019 - Dec 2021", ["Ran it."]),
        ("Operations Lead", "Contoso Health", "Jan 2016 - Dec 2018", ["Ran the floor."]),
        (
            "Analyst",
            "Northwind Traders",
            "Jan 2012 - Dec 2015",
            ["Promoted from Operations Lead to Director of Operations."],
        ),
    )
    assert _codes(check_resume(resume, _promo_history(), today=TODAY), "promotion_line_dropped")


def test_a_promotion_line_that_names_only_one_title_fires():
    resume = _promo_resume(["Promoted to Director of Operations.", "Ran it."])
    assert _codes(check_resume(resume, _promo_history(), today=TODAY), "promotion_line_dropped")


def test_a_resume_that_omits_the_promoted_role_is_not_flagged_as_dropped():
    resume = _resume(("Analyst", "Northwind Traders", "Jan 2012 - Dec 2015", ["Counted stock."]))
    assert not _codes(check_resume(resume, _promo_history(), today=TODAY), "promotion_line_dropped")


# ----- promotion_titles_merged -----------------------------------------------


def test_two_titles_stacked_under_one_heading_fire():
    resume = "\n".join(
        [
            "## Experience",
            "",
            "### Director of Operations",
            "**Contoso Health** | Oakbrook, OH | Jan 2016 - Dec 2021",
            "**Director of Operations** (Jan 2019 - Dec 2021)",
            "- Ran it.",
            "**Operations Lead** (Jan 2016 - Dec 2018)",
            "- Ran the floor.",
        ]
    )
    found = _codes(check_resume(resume, _promo_history(), today=TODAY), "promotion_titles_merged")
    assert found and found[0].severity is Severity.MAJOR


def test_one_role_spanning_both_source_titles_fires():
    resume = _resume(
        ("Director of Operations", "Contoso Health", "Jan 2016 - Dec 2021", ["Ran it."])
    )
    assert _codes(check_resume(resume, _promo_history(), today=TODAY), "promotion_titles_merged")


def test_separate_headings_do_not_fire_the_merged_check():
    resume = _promo_resume([PROMO_SOURCE, "Ran it."])
    assert not _codes(
        check_resume(resume, _promo_history(), today=TODAY), "promotion_titles_merged"
    )


# ----- short_role_growth -----------------------------------------------------


@pytest.mark.parametrize(
    "bullet",
    [
        "Grew the team from one to five engineers.",
        "Grew the team from a single junior member to five.",
        "Took new business from $1M to $5M.",
        "Built a pipeline: from $1.5M to $4M a year.",
        "Lifted revenue from $1 million to $3 million.",
        "Lifted revenue from $1,000,000 to $3,000,000.",
        "Generated $2M in new business.",
    ],
)
def test_a_growth_figure_in_a_four_month_role_is_a_minor_note(bullet):
    found = _codes(_run(bullet, "Jun 2020 - Sep 2020"), "short_role_growth")
    assert len(found) == 1
    assert found[0].severity is Severity.MINOR
    assert "expect the question" in found[0].message


def test_the_same_growth_figure_in_a_two_year_role_is_silent():
    found = _run("Grew the team from one to five engineers.", "Jan 2019 - Dec 2020")
    assert not _codes(found, "short_role_growth")


# ----- timeline_not_checked --------------------------------------------------


def _unchecked(resume: str, history: str | None = None):
    found = check_resume(resume, history or _promo_history(), today=TODAY)
    return _codes(found, "timeline_not_checked")


def test_a_role_with_no_dates_is_named_as_not_checked():
    resume = (
        "## Experience\n\n### Director of Operations\n"
        "**Contoso Health** | Oakbrook, OH\n- Ran it.\n"
    )
    found = _unchecked(resume)
    assert found and found[0].severity is Severity.MINOR
    assert "Director of Operations" in found[0].message


def test_a_malformed_range_is_named_as_not_checked():
    resume = _resume(("Director of Operations", "Contoso Health", "Janu 20 - Dec 21", ["Ran it."]))
    assert _unchecked(resume)


def test_a_reversed_range_is_named_as_not_checked():
    resume = _resume(
        ("Director of Operations", "Contoso Health", "Dec 2021 - Jan 2019", ["Ran it."])
    )
    assert _unchecked(resume)


def test_a_future_range_is_named_as_not_checked():
    resume = _resume(
        ("Director of Operations", "Contoso Health", "Jan 2030 - Dec 2031", ["Ran it."])
    )
    assert _unchecked(resume)


def test_a_role_with_no_employer_is_named_as_not_checked():
    resume = "## Experience\n\n### Director of Operations\n- Ran it.\n"
    assert _unchecked(resume)


def test_an_experience_section_with_no_parsable_role_is_not_an_all_clear():
    resume = "## Experience\n\nWorked at a few places doing operations work.\n"
    assert _unchecked(resume)


def test_a_role_with_no_bullets_has_nothing_to_check_and_stays_quiet():
    resume = _resume(("Director of Operations", "Contoso Health", "Jan 2019 - Dec 2021", []))
    assert not _unchecked(resume)


# ----- check_history (the source) --------------------------------------------


def test_check_history_runs_the_same_codes_on_the_source():
    history = _history(
        (
            "Director of Operations",
            "Contoso Health",
            "Jun 2020 - Sep 2020",
            ["Grew it from one to five."],
        ),
        ("Analyst", "Northwind Traders", "Jan 2013 - Dec 2016", ["By 2019, cut costs by a third."]),
        ("Lead", "Fabrikam", "Jan 2019 - Dec 2021", ["Built it within five years."]),
    )
    codes = {f.code for f in check_history(history, today=TODAY)}
    assert {"short_role_growth", "result_after_role_end", "duration_exceeds_role"} <= codes


def test_a_combined_multi_range_source_heading_is_not_guessed_at():
    history = (
        "## Experience\n\n"
        "### Director of Operations (Nov 2023 - Dec 2025) / Operations Lead (Jan 2020 - Oct 2023)"
        " | Contoso Health | Oakbrook, OH\n"
        "- Built it within one year.\n"
    )
    found = check_history(history, today=TODAY)
    unchecked = _codes(found, "timeline_not_checked")
    assert unchecked and "one heading per title" in unchecked[0].message
    assert "Director of Operations" in unchecked[0].message
    assert not _codes(found, "duration_exceeds_role")


def test_a_clean_source_has_no_findings():
    assert check_history(_promo_history(), today=TODAY) == []


def test_history_headings_outside_experience_are_not_roles():
    history = (
        _promo_history() + "\n## Education\n\n### BS Business | Example University\n- Honors.\n"
    )
    assert not _codes(check_history(history, today=TODAY), "timeline_not_checked")


# ----- ATS plain text --------------------------------------------------------

ATS_HISTORY = _history(
    ("Director of Operations", "Contoso Health", "Jan 2019 - Dec 2021", ["Ran it."]),
)


def _ats(*bullets: str) -> str:
    lines = [
        "SUMMARY:",
        "Operations leader.",
        "",
        "EXPERIENCE:",
        "Director of Operations",
        "Contoso Health | Oakbrook, OH | Jan 2019 - Dec 2021",
        *bullets,
        "",
        "EDUCATION:",
        "BS Business, Example University, 2005",
    ]
    return "\n".join(lines)


def test_the_same_checks_run_on_ats_text():
    found = check_resume(_ats("- Built it within five years."), ATS_HISTORY, today=TODAY)
    assert _codes(found, "duration_exceeds_role")


def test_a_bullet_wrapped_at_80_columns_is_joined_before_matching():
    found = check_resume(
        _ats("- Built the platform within", "  five years."), ATS_HISTORY, today=TODAY
    )
    assert _codes(found, "duration_exceeds_role")


def test_a_wrapped_continuation_is_never_read_as_a_role():
    found = check_resume(_ats("- Ran it for the whole", "  company."), ATS_HISTORY, today=TODAY)
    assert not _codes(found, "timeline_not_checked")


def test_ats_one_line_role_format_is_understood():
    text = (
        "EXPERIENCE:\nDirector of Operations | Contoso Health | Jan 2019 - Dec 2021\n"
        "- Built it within five years.\n"
    )
    assert _codes(check_resume(text, ATS_HISTORY, today=TODAY), "duration_exceeds_role")


def test_the_ats_education_section_is_not_an_experience_role():
    found = check_resume(_ats("- Ran it."), ATS_HISTORY, today=TODAY)
    assert found == []


# ----- the code set ----------------------------------------------------------


def test_timeline_codes_name_every_check():
    assert TIMELINE_CODES == {
        "duration_exceeds_role",
        "result_after_role_end",
        "promotion_line_dropped",
        "promotion_titles_merged",
        "short_role_growth",
        "timeline_not_checked",
    }


def test_no_timeline_finding_is_fatal():
    resume, history = _one("By 2019, built it within five years.", "Jan 2013 - Dec 2016")
    found = check_resume(resume, history, today=TODAY)
    assert found and all(f.severity is not Severity.FATAL for f in found)


# ----- live-check regressions (owner's real shapes, invented content) --------

TWO_PROMO_HISTORY = _history(
    (
        "Director of Plant Systems",
        "Contoso Health",
        "Mar 2022 - Jun 2025",
        [
            "Promoted from Systems Supervisor (Apr 2018 - Feb 2022) to Director of Plant "
            "Systems (Mar 2022 - Jun 2025).",
            "Ran it.",
        ],
    ),
    (
        "Systems Supervisor",
        "Contoso Health",
        "Apr 2018 - Feb 2022",
        ["Ran the floor.", "Promoted to Director of Plant Systems in Mar 2022."],
    ),
)


def _two_promo_resume(director_first: str) -> str:
    return _resume(
        ("Director of Plant Systems", "Contoso Health", "Mar 2022 - Jun 2025", [director_first]),
        ("Systems Supervisor", "Contoso Health", "Apr 2018 - Feb 2022", ["Ran the floor."]),
    )


def test_a_promotion_bullet_under_the_earlier_heading_maps_to_the_title_promoted_to():
    resume = _two_promo_resume(
        "Promoted from Systems Supervisor to Director of Plant Systems in Mar 2022."
    )
    found = check_resume(resume, TWO_PROMO_HISTORY, today=TODAY)
    assert not _codes(found, "promotion_line_dropped")


def test_a_dropped_line_is_reported_once_for_the_role_promoted_into():
    resume = _two_promo_resume("Ran it.")
    found = _codes(check_resume(resume, TWO_PROMO_HISTORY, today=TODAY), "promotion_line_dropped")
    assert len(found) == 1
    assert "Director of Plant Systems" in found[0].message


def test_a_promotion_line_is_exempt_from_result_after_role_end():
    resume = _two_promo_resume(
        "Promoted from Systems Supervisor to Director of Plant Systems in Mar 2022."
    )
    resume = resume.replace("Ran the floor.", "Promoted to Director of Plant Systems in Mar 2022.")
    found = check_resume(resume, TWO_PROMO_HISTORY, today=TODAY)
    assert not _codes(found, "result_after_role_end")
    assert not _codes(check_history(TWO_PROMO_HISTORY, today=TODAY), "result_after_role_end")


@pytest.mark.parametrize(
    "bullet",
    [
        "Presented three-year service plans to the regional partner group.",
        "Wrote three-year technology roadmaps for the board.",
        "Built 5-year capital budgets and forecasts.",
    ],
)
def test_a_plan_horizon_with_words_before_a_plural_plan_noun_is_exempt(bullet):
    assert not _codes(_run(bullet, "Jun 2020 - Sep 2020"), "duration_exceeds_role")


def test_a_hyphen_year_span_far_from_any_plan_noun_still_fires():
    found = _run(
        "Led a five-year migration of the data centers to the cloud.", "Jun 2020 - Sep 2020"
    )
    assert _codes(found, "duration_exceeds_role")


REAL_ATS = """ALEX J. RIVERA:

Springfield, OH 55555 | 555-0100 | alex@example.com |
https://example.com/in/alex/

PROFESSIONAL SUMMARY:

Technology executive with a long record.

EXPERIENCE:

DIRECTOR OF PLANT SYSTEMS:
Contoso Health | Springfield, OH | Jan 2019 - Dec 2021
- Promoted from Operations Manager to Director of Plant Systems in Jan 2019.
- Across both roles, built the platform within five years. Led 22 staff across
systems, database, service desk, DevOps, and application support.

OPERATIONS MANAGER:
Contoso Health | Springfield, OH | Jan 2016 - Dec 2018
- Redesigned the network.

EDUCATION:

BS Business, Example University
"""

REAL_ATS_HISTORY = _history(
    ("Director of Plant Systems", "Contoso Health", "Jan 2019 - Dec 2021", ["Ran it."]),
    ("Operations Manager", "Contoso Health", "Jan 2016 - Dec 2018", ["Ran sites."]),
)


def test_the_uppercase_exported_ats_shape_is_parsed():
    found = check_resume(REAL_ATS, REAL_ATS_HISTORY, today=TODAY)
    assert not _codes(found, "timeline_not_checked")
    # Across both roles is satisfied by the adjacent same-employer role (72 months).
    assert not _codes(found, "duration_exceeds_role")


def test_the_uppercase_exported_ats_shape_still_catches_a_contradiction():
    text = REAL_ATS.replace("Across both roles, built", "Built")
    found = check_resume(text, REAL_ATS_HISTORY, today=TODAY)
    assert _codes(found, "duration_exceeds_role")


def test_unindented_wrapped_lines_join_their_bullet():
    text = REAL_ATS.replace(
        "- Across both roles, built the platform within five years.",
        "- Built the platform within\nfive years.",
    )
    found = check_resume(text, REAL_ATS_HISTORY, today=TODAY)
    assert _codes(found, "duration_exceeds_role")


def test_the_real_exporter_output_is_what_the_parser_reads(tmp_path):
    from blastjob.exporters import ats

    markdown = _resume(
        (
            "Director of Plant Systems",
            "Contoso Health",
            "Jan 2019 - Dec 2021",
            [
                "Promoted from Operations Manager to Director of Plant Systems in Jan 2019.",
                "Built the platform within five years, then led 22 staff across systems, "
                "database, service desk, DevOps, and application support for the company.",
            ],
        ),
        (
            "Operations Manager",
            "Contoso Health",
            "Jan 2016 - Dec 2018",
            ["Redesigned the network."],
        ),
    )
    exported = ats.write(markdown, tmp_path, stem="r").read_text(encoding="ascii")
    assert "DIRECTOR OF PLANT SYSTEMS:" in exported  # the shape under test
    found = check_resume(exported, REAL_ATS_HISTORY, today=TODAY)
    assert not _codes(found, "timeline_not_checked")
    assert _codes(found, "duration_exceeds_role")


# ----- hostile input (security review) ----------------------------------------


def test_a_long_whitespace_run_after_n_year_is_not_quadratic():
    import time

    bullet = "Led a 5-year" + " " * 50_000 + "migration."
    started = time.perf_counter()
    _run(bullet)
    assert time.perf_counter() - started < 1.0


def test_a_huge_bullet_is_truncated_before_any_regex_runs():
    import time

    bullet = "Built it " + "word " * 200_000 + "within five years."
    started = time.perf_counter()
    found = _run(bullet)
    assert time.perf_counter() - started < 1.0
    # The claim sits past the 2000-character cap, so it is not read.
    assert not _codes(found, "duration_exceeds_role")


@pytest.mark.parametrize(
    "bullet",
    [
        "Built it within ſix years.",  # long s folds to "s" under Unicode IGNORECASE
        "Built it within nıne years.",  # dotless i
        "Shipped in ſept 2030 and by ſept 2012.",
    ],
)
def test_non_ascii_lookalikes_never_raise(bullet):
    assert isinstance(_run(bullet), list)


def test_a_long_parenthesis_in_a_heading_does_not_hang():
    import time

    history = (
        "## Experience\n\n### Lead ("
        + "x" * 50_000
        + " | Contoso Health | Oakbrook, OH\n- Ran it.\n"
    )
    started = time.perf_counter()
    check_history(history, today=TODAY)
    assert time.perf_counter() - started < 1.0

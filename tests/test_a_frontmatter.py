"""Group A, item A5: frontmatter regexes must not cross a newline."""

from __future__ import annotations

from blastjob.core import build

HISTORY_EMPTY_NAME = "---\nname:\nemail: pat@example.com\nphone: 555-0100\n---\n\n## Summary\n"


def test_empty_name_field_does_not_swallow_the_next_line():
    assert build._history_name(HISTORY_EMPTY_NAME) == "Candidate"


def test_name_field_still_reads_a_normal_value():
    assert build._history_name("---\nname:   Pat Example  \nemail: p@example.com\n---\n") == (
        "Pat Example"
    )


def test_letterhead_with_blank_name_builds_no_letterhead():
    body = "Dear team,\n\nHello.\n"

    assert build._letterhead(body, HISTORY_EMPTY_NAME, "Acme") == body


def test_letterhead_blank_phone_is_left_out_not_filled_from_the_next_line():
    history = "---\nname: Pat Example\nemail: pat@example.com\nphone:\nlocation: Springfield\n---\n"

    out = build._letterhead("Hello.", history, "Acme")

    assert "Springfield · pat@example.com" in out
    assert "location:" not in out

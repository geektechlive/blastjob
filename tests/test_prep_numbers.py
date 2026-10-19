"""`mark_unverified_numbers` is the last thing standing between an invented
figure and an interview, so its failure modes get their own file.

Four separate defects are pinned here:

1. the match must not swallow the space before the following word (which both
   glued the marker onto that word and made a trusted figure look untrusted,
   because `trusted_numbers_text` renders figures one per line);
2. money with a spelled-out scale (`$3.5 billion`) must be matched as one token
   and left intact;
3. bare magnitudes, comma-grouped headcounts and years must be matched at all;
4. trust is a whole-token comparison, not a substring test -- `$165` must NOT be
   cleared by a trusted `$165,000`, and `3%` must NOT be cleared by `13%`.
"""

from __future__ import annotations

import pytest

from blastjob.core import prep

CONFIRM = "[confirm]"


def mark(body: str, trusted: str = "") -> tuple[str, int]:
    return prep.mark_unverified_numbers(body, trusted)


# --- defect 1: the trailing space --------------------------------------------


def test_trusted_money_before_a_word_is_not_marked():
    """`trusted_numbers_text` writes one figure per line, so trusted holds
    '$250,000\\n' and never '$250,000 '. Capturing the trailing space made the
    app flag a figure it had on file itself."""
    marked, count = mark("Base is $250,000 base salary.", "$250,000\n")
    assert marked == "Base is $250,000 base salary."
    assert count == 0


def test_marker_is_never_glued_to_the_following_word():
    marked, count = mark("Ran a $9M program this year.", "")
    assert "$9M [confirm] program" in marked
    assert " [confirm]program" not in marked
    assert count == 1


@pytest.mark.parametrize(
    "body",
    [
        "Owned a $250,000 base salary.",
        "A $3.5 billion balance sheet.",
        "A $200 million program.",
        "Spend of $165,000 total.",
    ],
)
def test_no_match_ever_ends_in_whitespace(body: str):
    for m in prep._NUMERIC.finditer(body):
        assert m.group(0) == m.group(0).strip(), m.group(0)


# --- defect 2: spelled-out scales --------------------------------------------


def test_spelled_out_scale_is_one_token_and_the_word_survives():
    marked, count = mark("A $3.5 billion balance sheet.", "")
    assert "$3.5 billion [confirm] balance sheet." in marked
    assert "$3.5  [confirm]billion" not in marked
    assert count == 1


def test_million_phrase_keeps_its_word():
    marked, count = mark("Led a $200 million program.", "")
    assert "$200 million [confirm] program." in marked
    assert count == 1


def test_spelled_out_scale_matches_its_letter_form_in_trusted_text():
    """Trusted renderings are letter-suffixed ('$310M'); the body may spell it
    out. Same number, so it is verified."""
    marked, count = mark("A $310 million book.", "$310M\n")
    assert marked == "A $310 million book."
    assert count == 0


# --- defect 3: what gets matched at all --------------------------------------


def test_comma_grouped_headcount_is_marked():
    marked, count = mark("Team of 1,200 engineers.", "")
    assert "1,200 [confirm] engineers." in marked
    assert count == 1


def test_bare_magnitude_is_marked():
    marked, count = mark("Reduced spend by 40M last year.", "")
    assert "40M [confirm]" in marked
    assert count == 1


def test_comma_grouped_number_with_a_decimal_is_matched_whole():
    """The decimal tail must bind to BOTH bare-number branches. Bound to only
    the no-comma branch, '1,850.5' matches '1,850', the suffix guard then sees
    '.5' and rejects it, and the figure vanishes unmarked -- a miss, which is
    the failure direction this module exists to prevent."""
    marked, count = mark("Ratio of 1,850.5 per branch.", "")
    assert "1,850.5 [confirm] per branch." in marked
    assert count == 1


def test_money_with_cents_is_matched_whole():
    marked, count = mark("Invoiced $850.50 in total.", "")
    assert "$850.50 [confirm] in total." in marked
    assert count == 1


def test_capitalised_scale_word_still_matches_its_letter_form():
    """'$3.5 Billion' must not degrade to a bare '$3.5' match -- that both
    re-glues the marker and falsely flags a figure trusted as '$3.5B'."""
    marked, count = mark("A $3.5 Billion book.", "$3.5B\n")
    assert marked == "A $3.5 Billion book."
    assert count == 0


def test_year_is_marked_when_not_in_the_work_history():
    marked, count = mark("Delivered the cutover in 2019.", "")
    assert "2019 [confirm]." in marked
    assert count == 1


def test_year_present_in_the_work_history_is_left_alone():
    marked, count = mark("Delivered the cutover in 2019.", "Acme, 2019 to 2023.")
    assert marked == "Delivered the cutover in 2019."
    assert count == 0


def test_small_bare_counts_are_still_left_alone():
    """A 30/60/90 plan and 'hired 40 staff' must not turn into confetti."""
    marked, count = mark("In the first 30 days, with 40 staff and 3 vendors.", "")
    assert marked == "In the first 30 days, with 40 staff and 3 vendors."
    assert count == 0


# --- defect 4: whole-token trust ---------------------------------------------


def test_truncated_money_is_not_cleared_by_a_longer_trusted_figure():
    marked, count = mark("Budget of $165 total.", "$165,000\n")
    assert "$165 [confirm] total." in marked
    assert count == 1


def test_smaller_percentage_is_not_cleared_by_a_longer_trusted_one():
    marked, count = mark("Cut cost 3%.", "13%\n")
    assert "3% [confirm]." in marked
    assert count == 1


def test_misplaced_decimal_is_not_cleared():
    marked, count = mark("A $42M program.", "$4.2M\n")
    assert "$42M [confirm]" in marked
    assert count == 1


def test_exact_token_is_cleared():
    marked, count = mark("A $4.2M program.", "Managed a $4.2M budget.")
    assert marked == "A $4.2M program."
    assert count == 0


# --- idempotency (the streaming path re-marks nothing) ------------------------


def test_marking_twice_changes_nothing():
    body = "A $3.5 billion book and 1,200 staff."
    once, first = mark(body, "")
    twice, second = mark(once, "")
    assert twice == once
    assert (first, second) == (2, 0)


# --- the trusted set round-trips through trusted_numbers_text -----------------


def test_figures_on_file_survive_a_round_trip_through_the_trusted_text():
    job = {"comp_min": 0, "comp_max": 0}
    profile = {"employees": 850, "total_assets_usd": 310_000_000}
    signals = [{"low_usd": 150_000, "high_usd": None}]
    trusted = prep.trusted_numbers_text(job, profile, signals, floor=150_000)

    body = "Floor $150,000; assets $310M; staff 850; and an invented $9M line.\n"
    marked, count = mark(body, trusted)
    assert CONFIRM not in marked.split("$9M")[0]
    assert "$9M [confirm]" in marked
    assert count == 1


@pytest.mark.parametrize(
    "form",
    ["30%", "30 %", "30 percent", "30 per cent", "30 pct", "30 Percent", "thirty percent"],
)
def test_percent_forms_unverified_when_absent(form: str) -> None:
    out, n = mark(f"Grew revenue {form} last year.", "Led the team.")
    assert n == 1
    assert out == f"Grew revenue {form} {CONFIRM} last year."


@pytest.mark.parametrize("trusted", ["30%", "30 percent", "thirty percent", "30 pct"])
@pytest.mark.parametrize("body", ["30%", "30 percent", "30 per cent", "thirty percent"])
def test_percent_forms_verify_each_other(body: str, trusted: str) -> None:
    out, n = mark(f"Grew {body}.", f"Grew {trusted} at Acme.")
    assert n == 0
    assert CONFIRM not in out


def test_three_percent_not_verified_by_thirty() -> None:
    assert mark("Cut 3 percent.", "Cut 30%.")[1] == 1
    assert mark("Cut three percent.", "Cut 13 percent.")[1] == 1
    assert mark("Cut 30 percent.", "Cut 3%.")[1] == 1

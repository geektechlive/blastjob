"""The debrief figures, computed from an invented transcript (#84)."""

from __future__ import annotations

import pytest

from blastjob.core.debrief import SILENCE_CAP_S, compute_debrief, fmt_clock
from blastjob.core.transcript import parse_transcript
from tests.debrief_fixtures import BASIC, OTHER, OWNER, SENTINEL, header, vtt, words


def run(turns, trusted: str | None = "", owner: str = OWNER):
    return compute_debrief(parse_transcript(header(turns)), owner, trusted)


def test_word_share_counts_every_turn_including_the_last():
    d = run(BASIC)
    # owner: 3 + 12 + 9 = 24; other: 20 + 5 + 1 + 2 = 28
    assert d.owner_words == 24
    assert d.other_words == 28
    assert d.talk_share_words == pytest.approx(24 / 52 * 100)


def test_time_share_is_an_estimate_with_silence_capped():
    turns = [(0, OTHER, "Hi."), (10, OWNER, "I answer."), (400, OTHER, "Ok."), (410, OWNER, "Z.")]
    d = run(turns)
    assert d.time_basis == "estimate"
    assert SILENCE_CAP_S == 90
    # the owner turn at 10 runs 390s to the next start but is credited 90; the last turn has
    # no known length and is left out
    assert d.owner_seconds == 90
    assert d.other_seconds == 10 + 10
    assert d.talk_share_time == pytest.approx(90 / 110 * 100)


def test_time_share_uses_cue_times_when_the_format_has_ends():
    cues = [(0, 10, OTHER, "Hello."), (20, 30, OWNER, "Hi."), (40, 60, OTHER, "Tell me more.")]
    d = compute_debrief(parse_transcript(vtt(cues)), OWNER, "")
    assert d.time_basis == "cue"
    assert d.owner_seconds == 10 and d.other_seconds == 30
    assert d.talk_share_time == pytest.approx(25.0)


def test_last_turn_is_excluded_from_time_but_counts_in_words():
    turns = [(0, OTHER, "one two"), (10, OWNER, "three four"), (20, OTHER, words(30))]
    d = run(turns)
    assert d.other_seconds == 10
    assert d.other_words == 32


def test_duration_runs_from_the_first_timestamp_to_the_endpoint():
    assert run(BASIC).duration_s == 100


def test_consecutive_owner_turns_merge_into_one_answer():
    turns = [
        (0, OTHER, "Q?"),
        (10, OWNER, words(5, "m")),
        (20, OWNER, words(7, "n")),
        (40, OTHER, "Ok."),
        (50, OWNER, words(3, "z")),
        (60, OTHER, "Bye."),
    ]
    d = run(turns)
    assert d.longest[0].words == 12
    assert d.longest[0].seconds == 30
    assert d.longest[0].start == 10


def test_longest_answers_are_top_three_by_words_with_a_160_char_excerpt():
    long_text = words(80, "long")
    turns = [
        (0, OTHER, "Q"),
        (10, OWNER, words(5, "a")),
        (20, OTHER, "Q"),
        (30, OWNER, long_text),
        (40, OTHER, "Q"),
        (50, OWNER, words(30, "c")),
        (60, OTHER, "Q"),
        (70, OWNER, words(20, "d")),
        (80, OTHER, "Q"),
        (90, OWNER, "tail"),
        (100, OTHER, "Bye"),
    ]
    d = run(turns)
    assert [a.words for a in d.longest] == [80, 30, 20]
    assert d.longest[0].excerpt == long_text[:160]
    assert len(d.longest[0].excerpt) == 160


def test_interviewer_words_are_never_in_the_result():
    blob = repr(run(BASIC))
    assert SENTINEL not in blob and "Welcome" not in blob


def _closing_turns():
    return [
        (0, OTHER, "Start"),
        (10, OWNER, "Early question? Why is that?"),
        (20, OTHER, "Ans"),
        (90, OWNER, "What does success look like in six months"),
        (95, OWNER, "I like the team. Is there a second round?"),
        (98, OTHER, "Yes"),
        (100, OWNER, "Thanks, I statement only."),
    ]


def test_closing_questions_count_only_inside_the_last_fifteen_percent():
    d = run(_closing_turns())
    # the window starts at 85s: the early "?" lines do not count, two later ones do
    assert d.closing_questions == 2
    assert "What does success look like in six months" in d.closing_excerpts


def test_closing_excerpts_hold_at_most_three_of_the_owners_lines():
    turns = [(0, OTHER, "x"), (50, OTHER, "y")] + [
        (90 + i, OWNER, f"Can you tell me item{i}?") for i in range(5)
    ]
    turns.append((100, OTHER, "End"))
    d = run(turns)
    assert d.closing_questions == 5
    assert len(d.closing_excerpts) == 3


def test_an_interrogative_word_counts_without_a_mark_but_a_contraction_does_not():
    turns = [(0, OTHER, "Hi"), (95, OWNER, "Can't say. How would the team grow"), (100, OTHER, "k")]
    assert run(turns).closing_questions == 1


@pytest.mark.parametrize(
    ("spoken", "trusted", "flagged"),
    [
        ("We saved 40% a year.", "Cut spend by 40% in 2019.", []),
        ("We saved 95% a year.", "Cut spend by 40% in 2019.", ["95%"]),
        ("That was in 2019 and 1999.", "nothing here", []),
        ("I led 12 people and 250 servers.", "nothing here", []),
        ("Budget was $182 here.", "Budget was $182,000.", ["$182"]),
        ("Budget was $182,000 here.", "Budget was $182,000.", []),
        ("Revenue grew $3.5 billion.", "Revenue grew $3.5B.", []),
        ("A team of 1,200 people.", "A team of 1200 people.", []),
        ("A team of 12,000 people.", "A team of 1200 people.", ["12,000"]),
    ],
)
def test_numbers_are_checked_as_whole_tokens_under_the_transcript_policy(spoken, trusted, flagged):
    d = run([(0, OTHER, "Q"), (10, OWNER, spoken), (20, OTHER, "Ok")], trusted)
    assert d.numbers_status == "checked"
    assert [p.token for p in d.numbers] == flagged


def test_a_missing_work_history_makes_the_check_unavailable_not_all_prompts():
    d = run([(0, OTHER, "Q"), (10, OWNER, "We saved 95% and $2M."), (20, OTHER, "Ok")], None)
    assert d.numbers_status == "unavailable"
    assert d.numbers == ()


def test_an_empty_work_history_is_unavailable_too():
    d = run([(0, OTHER, "Q"), (10, OWNER, "We saved 95%."), (20, OTHER, "Ok")], "  \n")
    assert d.numbers_status == "unavailable"


def test_numbers_carry_a_timestamp_dedupe_and_stop_at_ten():
    spoken = " ".join(f"{n}%" for n in range(11, 26)) + " 11%"
    d = run([(0, OTHER, "Q"), (75, OWNER, spoken), (90, OTHER, "Ok")], "none")
    assert len(d.numbers) == 10
    assert d.numbers[0].token == "11%" and d.numbers[0].at == 75


def test_only_the_owners_lines_are_number_checked():
    d = run([(0, OTHER, "We pay 95% bonus"), (10, OWNER, "Great."), (20, OTHER, "Ok")], "x")
    assert d.numbers == ()


def test_an_unknown_owner_label_is_a_value_error():
    with pytest.raises(ValueError, match="speaker"):
        run(BASIC, owner="Nobody")


def test_clock_formatting():
    assert fmt_clock(56) == "0:00:56"
    assert fmt_clock(3725) == "1:02:05"

"""Reading a pasted transcript: three formats, and the inputs that are refused (#84)."""

from __future__ import annotations

import pytest

from blastjob.core.transcript import MAX_TRANSCRIPT_CHARS, TranscriptError, parse_transcript
from tests.debrief_fixtures import BASIC, OTHER, OWNER, clock, header, inline, vtt

TWO = [(0, OTHER, "Hello there."), (65, OWNER, "Thanks for having me.")]


def test_header_lines_parse_with_multiline_text():
    text = "00:00:56 Speaker 1\nFirst line.\nSecond line.\n00:01:11 Speaker 2\nReply.\n"
    t = parse_transcript(text)
    assert [(x.start, x.speaker) for x in t.turns] == [(56.0, "Speaker 1"), (71.0, "Speaker 2")]
    assert t.turns[0].text == "First line.\nSecond line."
    assert t.turns[0].end is None


def test_header_lines_accept_minutes_and_seconds_only():
    t = parse_transcript("00:05 Speaker 1\nHi.\n01:10 Speaker 2\nHello.\n")
    assert [x.start for x in t.turns] == [5.0, 70.0]


def test_inline_format_parses_and_joins_continuation_lines():
    text = "[00:00:03] Casey Rivera: Hello.\nmore of it\n[00:00:20] Jordan Example: Hi, there.\n"
    t = parse_transcript(text)
    assert [x.speaker for x in t.turns] == [OTHER, OWNER]
    assert t.turns[0].text == "Hello.\nmore of it"
    assert t.turns[1].text == "Hi, there."


def test_webvtt_voice_tags_parse_with_end_times():
    t = parse_transcript(vtt([(0, 4, OTHER, "Hello."), (5, 12.5, OWNER, "Hi.")]))
    assert [(x.start, x.end, x.speaker) for x in t.turns] == [
        (0.0, 4.0, OTHER),
        (5.0, 12.5, OWNER),
    ]


def test_webvtt_speaker_prefix_parses():
    t = parse_transcript(vtt([(0, 4, OTHER, "Hello."), (5, 9, OWNER, "Hi.")], voice_tags=False))
    assert [x.speaker for x in t.turns] == [OTHER, OWNER]
    assert t.turns[1].text == "Hi."


def test_webvtt_overlapping_cues_are_clipped_to_the_next_start():
    t = parse_transcript(vtt([(0, 10, OTHER, "Hello."), (6, 12, OWNER, "Hi.")]))
    assert t.turns[0].end == 6.0


def test_speakers_are_counted_in_order_of_appearance():
    t = parse_transcript(header(BASIC))
    assert list(t.speakers) == [OTHER, OWNER]
    assert t.speakers[OWNER] == 3


def test_endpoint_is_the_last_cue_end_for_webvtt_and_last_start_otherwise():
    assert parse_transcript(vtt([(0, 4, OTHER, "a b"), (5, 20, OWNER, "c d")])).endpoint == 20.0
    assert parse_transcript(header(TWO)).endpoint == 65.0


@pytest.mark.parametrize("text", ["", "   \n", "Just some prose with no structure at all.\n"])
def test_unsupported_input_is_refused_naming_the_formats(text):
    with pytest.raises(TranscriptError) as err:
        parse_transcript(text)
    message = str(err.value)
    assert "WebVTT" in message and "[00:01:05] Name: text" in message


def test_one_speaker_is_refused():
    with pytest.raises(TranscriptError, match="two speakers"):
        parse_transcript(header([(0, OWNER, "Hi."), (30, OWNER, "Still me.")]))


def test_decreasing_timestamps_are_refused():
    with pytest.raises(TranscriptError, match="order"):
        parse_transcript(header([(30, OTHER, "Hi."), (10, OWNER, "Hello."), (50, OTHER, "Ok.")]))


def test_a_single_distinct_timestamp_is_refused():
    with pytest.raises(TranscriptError, match="two different times"):
        parse_transcript(header([(5, OTHER, "Hi."), (5, OWNER, "Hello.")]))


def test_duplicate_timestamps_alongside_a_distinct_one_are_accepted():
    t = parse_transcript(header([(5, OTHER, "Hi."), (5, OWNER, "Hello."), (9, OTHER, "Ok.")]))
    assert len(t.turns) == 3


def test_text_over_the_size_cap_is_refused_before_parsing():
    with pytest.raises(TranscriptError, match="too large"):
        parse_transcript("x" * (MAX_TRANSCRIPT_CHARS + 1))


def test_a_header_look_alike_inside_a_sentence_is_not_a_speaker_change():
    text = header([(0, OTHER, "We start at"), (20, OWNER, "Sure.")]).replace(
        "We start at", "We start at\n10:30 sharp, is that ok."
    )
    t = parse_transcript(text)
    assert len(t.turns) == 2
    assert clock(0) in text
    assert "10:30 sharp, is that ok." in t.turns[0].text


def test_inline_text_is_detected_over_header_text():
    assert parse_transcript(inline(BASIC)).turns[0].speaker == OTHER

"""Structured run progress: the emit/parse round-trip, and which event a
status line should show.

Defect 1 this closes: a progress update was one free-text string ("Scoring
batch 3/10") with no counts a bar could be drawn from.

Defect 2 this closes: `Runtime.start`'s driver emits the terminal event as
`run.emit("done", "")` -- empty data by design (it's a status transition, not
a message). Naively rendering the run's *last* event blanks the "Done: 12
surfaced, 3 queued" summary the instant the run finishes. `display_event`
finds the newest event that actually carries data instead.
"""

from __future__ import annotations

from blastjob.web import progress as pr
from blastjob.web.runtime import Event


def _ev(id_: int, kind: str, data: str) -> Event:
    return Event(id=id_, kind=kind, data=data)


# ----- emit/parse round-trip --------------------------------------------------


def test_format_payload_round_trips_through_parse():
    payload = pr.format_payload(3, 10, label="Scoring batch")
    parsed = pr.parse(payload)
    assert parsed == pr.Progress(done=3, total=10, label="Scoring batch")


def test_format_payload_label_defaults_to_empty_string():
    parsed = pr.parse(pr.format_payload(1, 2))
    assert parsed is not None
    assert parsed.label == ""


def test_percent_computes_from_done_over_total():
    assert pr.Progress(done=25, total=100).percent == 25
    assert pr.Progress(done=1, total=3).percent == 33  # rounds, doesn't truncate to 0


def test_percent_is_zero_when_total_is_zero_not_a_crash():
    assert pr.Progress(done=0, total=0).percent == 0


def test_percent_clamps_to_100_even_if_done_exceeds_total():
    assert pr.Progress(done=999, total=10).percent == 100


# ----- parse degrades gracefully on anything that isn't our shape ------------


def test_parse_returns_none_for_old_free_text_progress():
    """Pre-existing emit sites (and any not yet migrated) send plain text like
    "Scoring batch 3/10" -- not JSON. That must not raise."""
    assert pr.parse("Scoring batch 3/10") is None


def test_parse_returns_none_for_malformed_json():
    assert pr.parse("{not json") is None


def test_parse_returns_none_when_required_fields_missing():
    assert pr.parse('{"done": 3}') is None


def test_parse_returns_none_for_negative_counts():
    assert pr.parse(pr.format_payload(-1, 10)) is None


def test_parse_returns_none_for_non_object_json():
    assert pr.parse("42") is None
    assert pr.parse("[1, 2]") is None


# ----- latest_progress over a run's events -----------------------------------


def test_latest_progress_is_none_with_no_progress_events():
    events = [_ev(1, "log", "starting")]
    assert pr.latest_progress(events) is None


def test_latest_progress_finds_the_newest_progress_event():
    events = [
        _ev(1, "progress", pr.format_payload(1, 10)),
        _ev(2, "log", "an unrelated log line"),
        _ev(3, "progress", pr.format_payload(5, 10)),
    ]
    assert pr.latest_progress(events) == pr.Progress(done=5, total=10)


def test_latest_progress_degrades_to_none_when_newest_is_malformed():
    """Only the newest progress event is consulted -- an older one parsing
    fine does not paper over a newer, broken one."""
    events = [
        _ev(1, "progress", pr.format_payload(1, 10)),
        _ev(2, "progress", "batch 2/10"),  # old free-text shape
    ]
    assert pr.latest_progress(events) is None


# ----- display_event: the summary must survive the terminal "done" ----------


def test_display_event_is_none_for_an_empty_run():
    assert pr.display_event([]) is None


def test_display_event_picks_the_last_event_when_it_has_data():
    events = [_ev(1, "log", "first"), _ev(2, "log", "second")]
    assert pr.display_event(events) is events[1]


def test_display_event_skips_a_trailing_empty_done_event():
    """The exact defect: Runtime.start's driver appends `done` with empty
    data after the real work is finished. The summary line must keep showing
    the last log message, not go blank."""
    events = [
        _ev(1, "log", "Discovering jobs from configured sources..."),
        _ev(2, "progress", pr.format_payload(3, 3)),
        _ev(3, "log", "Done: 12 surfaced, 3 queued (scored 40 of 120 fetched)."),
        _ev(4, "done", ""),
    ]
    disp = pr.display_event(events)
    assert disp is not None
    assert disp.data == "Done: 12 surfaced, 3 queued (scored 40 of 120 fetched)."


def test_display_event_skips_multiple_trailing_empty_events():
    events = [
        _ev(1, "log", "the real summary"),
        _ev(2, "done", ""),
    ]
    disp = pr.display_event(events)
    assert disp is not None and disp.data == "the real summary"


def test_display_event_returns_none_if_every_event_is_empty():
    events = [_ev(1, "progress", ""), _ev(2, "done", "")]
    assert pr.display_event(events) is None


def test_display_event_does_not_skip_a_populated_error_event():
    events = [
        _ev(1, "log", "Discovering..."),
        _ev(2, "error", "RuntimeError: boom"),
    ]
    disp = pr.display_event(events)
    assert disp is not None and disp.kind == "error"


# ----- Event.progress(): the per-event convenience the template uses --------


def test_event_progress_parses_a_progress_kind_event():
    ev = _ev(1, "progress", pr.format_payload(4, 8, label="Scoring batch"))
    assert ev.progress() == pr.Progress(done=4, total=8, label="Scoring batch")


def test_event_progress_is_none_for_a_non_progress_kind():
    ev = _ev(1, "log", pr.format_payload(4, 8))
    assert ev.progress() is None


def test_event_progress_is_none_for_malformed_data():
    ev = _ev(1, "progress", "not json at all")
    assert ev.progress() is None


# ----- Run convenience methods -----------------------------------------------


def test_run_display_event_matches_module_function():
    from blastjob.web.runtime import Run

    run = Run(id="r-1", name="discovery")
    run.emit("log", "Discovering jobs...")
    run.emit("done", "")
    disp = run.display_event()
    assert disp is not None and disp.data == "Discovering jobs..."


def test_run_latest_progress_matches_module_function():
    from blastjob.web.runtime import Run

    run = Run(id="r-1", name="matching")
    run.emit("progress", pr.format_payload(2, 5, label="Scoring batch"))
    assert run.latest_progress() == pr.Progress(done=2, total=5, label="Scoring batch")


def test_run_latest_progress_is_none_before_any_progress_event():
    from blastjob.web.runtime import Run

    run = Run(id="r-1", name="matching")
    run.emit("log", "starting")
    assert run.latest_progress() is None

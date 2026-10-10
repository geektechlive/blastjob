"""Structured payloads for a run's "progress" events.

Before this, a progress update was one free-text string emitted straight into
`Event.data` -- "Scoring batch 3/10" -- with no counts a template could draw a
bar from. A progress event's data is now a small JSON object (`done`, `total`,
`label`) built and read here.

This module is deliberately small and pure: it knows how to shape and parse
that payload, and which of a run's events is worth showing on a status line.
It does not know about `Runtime`, HTML, or scheduling, and it never raises on
bad input -- a malformed payload (or an old free-text one from a call site
that hasn't been migrated) just parses to `None` so a caller can fall back to
showing the raw text.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, TypeVar


class EventLike(Protocol):
    """Structural stand-in for `runtime.Event`, so this module never has to
    import `runtime` (which imports this module for `Event.progress()` /
    `Run.display_event()` -- importing back would be a cycle)."""

    kind: str
    data: str


_E = TypeVar("_E", bound=EventLike)


@dataclass(frozen=True)
class Progress:
    """One point-in-time progress reading."""

    done: int
    total: int
    label: str = ""

    @property
    def percent(self) -> int:
        """0-100, clamped. A non-positive total reads as 0%, never a
        ZeroDivisionError or a value outside a progress bar's range."""
        if self.total <= 0:
            return 0
        return max(0, min(100, round(100 * self.done / self.total)))


def format_payload(done: int, total: int, label: str = "") -> str:
    """The string an emit site passes as `Event.data` for a "progress" kind."""
    return json.dumps({"done": done, "total": total, "label": label})


def parse(data: str) -> Progress | None:
    """Parse one payload. `None` for anything that isn't our shape -- covers
    old/foreign free-text progress lines and any malformed JSON -- so a
    caller degrades to showing the raw text instead of raising."""
    try:
        obj = json.loads(data)
        done = int(obj["done"])
        total = int(obj["total"])
        label = str(obj.get("label", ""))
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None
    if done < 0 or total < 0:
        return None
    return Progress(done=done, total=total, label=label)


def latest_progress(events: Iterable[_E]) -> Progress | None:
    """The most recent parseable progress payload among `events`. `None` if
    there are no "progress" events, or the newest one doesn't parse -- an
    older one parsing fine does not paper over a broken newer one."""
    for event in reversed(list(events)):
        if event.kind == "progress":
            return parse(event.data)
    return None


def display_event(events: Iterable[_E]) -> _E | None:
    """The event a status line should show: the newest one carrying real
    data. Skips a trailing event emitted with empty data by design --
    `Runtime.start`'s driver appends `run.emit("done", "")` as a pure status
    transition once work finishes. Without this, a completed run's summary
    ("Done: 12 surfaced, 3 queued...") blanks itself the instant it lands."""
    for event in reversed(list(events)):
        if event.data:
            return event
    return None

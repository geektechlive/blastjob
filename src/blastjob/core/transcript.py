"""Read a pasted interview transcript into timed, speaker-labelled turns (#84).

Three layouts are accepted, because those are what transcription tools produce:

- header lines: `00:01:05 Speaker 1`, the turn's text on the lines below;
- WebVTT: cues with `<v Name>` voice tags or a `Name: text` prefix (the only layout with
  cue end times);
- inline: `[00:01:05] Name: text` on one line.

Anything else, a single speaker, or a timeline that does not run forward is refused with a
reason. Pure parsing: no I/O and nothing is kept. See docs/how-it-works/debrief.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

# Characters, not bytes. The route also caps the request body at the HTTP layer.
MAX_TRANSCRIPT_CHARS = 2_000_000
MIN_SPEAKERS = 2
MAX_LABEL_CHARS = 40
MAX_LABEL_WORDS = 4
MAX_NAME_CHARS = 40
MAX_TURNS = 5_000
MAX_SPEAKERS = 12
# Regexes only ever see the first MAX_MATCH_CHARS of a line, so no input can make one run long.
# The text of a turn is taken from the full line, so a long paragraph keeps all its words.
MAX_MATCH_CHARS = 500
MAX_HEADER_LINE_CHARS = 80

FORMATS_HELP = (
    "Accepted formats: header lines like '00:01:05 Speaker 1' with the text on the lines "
    "below; WebVTT (a file that starts with WEBVTT, with <v Name> tags or 'Name: text'); "
    "or one line per turn like '[00:01:05] Name: text'."
)

_TS = r"(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?"
# The header line is matched on the stripped line, with a tail that cannot backtrack.
_HEADER = re.compile(rf"({_TS})\s+(\S.*)")
_INLINE = re.compile(rf"\s*\[({_TS})\]\s*([^:\[\]]{{1,{MAX_NAME_CHARS}}}?)\s*:\s*")
_CUE = re.compile(rf"\s*({_TS})\s*-->\s*({_TS})")
# The class after the dot excludes the dot, so the repetitions cannot split a run two ways.
_VOICE = re.compile(r"<v(?:\.[^\s>.]+)*\s+([^>]{1,200})>", re.IGNORECASE)
_VOICE_CLOSE = re.compile(r"</v>", re.IGNORECASE)
_PREFIX = re.compile(rf"([^:<>]{{1,{MAX_NAME_CHARS}}}):\s+(.*)")
_TAG = re.compile(r"</?[^<>]{0,200}>")


class TranscriptError(ValueError):
    """The pasted text cannot be used; the message says why, for the owner to read."""


@dataclass(frozen=True)
class Turn:
    start: float
    end: float | None
    speaker: str
    text: str


@dataclass(frozen=True)
class Transcript:
    turns: tuple[Turn, ...]

    @property
    def speakers(self) -> dict[str, int]:
        """Speaker label -> number of turns, in order of first appearance."""
        counts: dict[str, int] = {}
        for turn in self.turns:
            counts[turn.speaker] = counts.get(turn.speaker, 0) + 1
        return counts

    @property
    def start(self) -> float:
        return self.turns[0].start

    @property
    def endpoint(self) -> float:
        """The last cue end where the format has ends, else the last timestamp."""
        ends = [t.end for t in self.turns if t.end is not None]
        return max([self.turns[-1].start, *ends])


def _seconds(stamp: str) -> float:
    main, _, frac = stamp.replace(",", ".").partition(".")
    parts = [int(p) for p in main.split(":")]
    h, m, s = ([0] * (3 - len(parts))) + parts
    return h * 3600 + m * 60 + s + (float(f"0.{frac}") if frac else 0.0)


def _plausible_label(label: str) -> bool:
    """A header line's trailing text is a speaker label, not a sentence that happens to
    begin with a clock time ("10:30 sharp, is that ok.")."""
    if len(label) > MAX_LABEL_CHARS or len(label.split()) > MAX_LABEL_WORDS:
        return False
    return not (set(",;") & set(label)) and label[-1] not in "?!:"


def _header_match(line: str) -> re.Match[str] | None:
    """A header line is short by nature; a long line is text, whatever it starts with."""
    stripped = line.strip()
    if len(stripped) > MAX_HEADER_LINE_CHARS:
        return None
    m = _HEADER.fullmatch(stripped)
    return m if m and _plausible_label(m.group(2)) else None


def _inline_match(line: str) -> tuple[float, str, str] | None:
    """(start, speaker, text) of an inline line; the text comes from the full line."""
    m = _INLINE.match(line[:MAX_MATCH_CHARS])
    if m is None:
        return None
    return _seconds(m.group(1)), m.group(2).strip(), line[m.end() :].strip()


def _parse_header(lines: list[str]) -> list[Turn]:
    heads: list[tuple[float, str]] = []
    bodies: list[list[str]] = []
    for line in lines:
        m = _header_match(line)
        if m:
            heads.append((_seconds(m.group(1)), m.group(2).strip()))
            bodies.append([])
        elif heads and line.strip():
            bodies[-1].append(line.strip())
    return [Turn(s, None, who, "\n".join(b)) for (s, who), b in zip(heads, bodies, strict=True)]


def _parse_inline(lines: list[str]) -> list[Turn]:
    heads: list[tuple[float, str]] = []
    bodies: list[list[str]] = []
    for line in lines:
        m = _inline_match(line)
        if m:
            heads.append((m[0], m[1]))
            bodies.append([m[2]] if m[2] else [])
        elif heads and line.strip():
            bodies[-1].append(line.strip())
    return [Turn(s, None, who, "\n".join(b)) for (s, who), b in zip(heads, bodies, strict=True)]


def _cue_segments(text_lines: list[str], last_speaker: str | None) -> list[tuple[str, str]]:
    """(speaker, text) pieces of one cue. A line with a voice tag or a `Name:` prefix starts
    a piece; any other line continues the current one, or the previous cue's speaker."""
    pieces: list[tuple[str, list[str]]] = []
    for line in text_lines:
        voice = _VOICE.search(line[:MAX_MATCH_CHARS])
        prefix = None if voice else _PREFIX.fullmatch(_TAG.sub("", line).strip())
        if voice:
            said = _VOICE_CLOSE.split(line[voice.end() :], maxsplit=1)[0]
            pieces.append((voice.group(1).strip()[:MAX_NAME_CHARS], [said]))
        elif prefix:
            pieces.append((prefix.group(1).strip(), [prefix.group(2)]))
        elif pieces:
            pieces[-1][1].append(line)
        elif last_speaker:
            pieces.append((last_speaker, [line]))
        else:
            raise TranscriptError("The WebVTT cues do not name their speakers. " + FORMATS_HELP)
    return [(who, _TAG.sub("", " ".join(parts)).strip()) for who, parts in pieces]


def _parse_vtt(text: str) -> list[Turn]:
    turns: list[Turn] = []
    last_speaker: str | None = None
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = block.split("\n")
        at = next((i for i, ln in enumerate(lines) if _CUE.match(ln[:MAX_MATCH_CHARS])), None)
        if at is None:
            continue
        cue = _CUE.match(lines[at][:MAX_MATCH_CHARS])
        assert cue is not None
        start, end = _seconds(cue.group(1)), _seconds(cue.group(2))
        body = [ln for ln in lines[at + 1 :] if ln.strip()]
        for who, said in _cue_segments(body, last_speaker):
            turns.append(Turn(start, max(end, start), who, said))
            last_speaker = who
    # Overlapping cues are clipped so one speaker is never credited with the next one's time.
    clipped: list[Turn] = []
    for i, turn in enumerate(turns):
        if turn.end is not None and i + 1 < len(turns):
            turn = replace(turn, end=min(turn.end, turns[i + 1].start))
        clipped.append(turn)
    return clipped


def _validate(turns: list[Turn]) -> Transcript:
    if len(turns) < 1:
        raise TranscriptError("No timed turns were found. " + FORMATS_HELP)
    if len(turns) > MAX_TURNS:
        raise TranscriptError(f"That transcript has too many turns (more than {MAX_TURNS}).")
    if len({t.speaker for t in turns}) > MAX_SPEAKERS:
        raise TranscriptError(f"That transcript has too many speakers (more than {MAX_SPEAKERS}).")
    if len({t.speaker for t in turns}) < MIN_SPEAKERS:
        raise TranscriptError(
            "Only one speaker was found; the debrief needs at least two speakers to "
            "compare your share with theirs."
        )
    starts = [t.start for t in turns]
    if any(b < a for a, b in zip(starts, starts[1:], strict=False)):
        raise TranscriptError("The timestamps are not in order, so the timeline cannot be used.")
    if len(set(starts)) < MIN_SPEAKERS:
        raise TranscriptError("The transcript needs at least two different times to be timed.")
    return Transcript(tuple(turns))


def parse_transcript(text: str) -> Transcript:
    """Parse and validate. Raises TranscriptError with a reason the owner can act on."""
    if len(text) > MAX_TRANSCRIPT_CHARS:
        raise TranscriptError("That transcript is too large (over 2 million characters).")
    clean = text.lstrip("﻿")
    lines = clean.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if clean.lstrip().upper().startswith("WEBVTT"):
        return _validate(_parse_vtt(clean))
    if any(_inline_match(line) for line in lines):
        return _validate(_parse_inline(lines))
    if any(_header_match(line) for line in lines):
        return _validate(_parse_header(lines))
    raise TranscriptError("That text is not in a format blastjob can read. " + FORMATS_HELP)

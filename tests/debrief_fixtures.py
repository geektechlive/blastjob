"""Invented interview transcripts for the debrief tests (Acme, Jordan Example, Casey Rivera).

Three formats, one builder each. Nothing here is real; the interviewer sentinel words
are chosen so a test can search a database for them.
"""

from __future__ import annotations

OWNER = "Jordan Example"
OTHER = "Casey Rivera"
# Words only an interviewer line carries, so "no interviewer text was stored" is checkable.
SENTINEL = "zebrafish"


def clock(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def header(turns: list[tuple[int, str, str]]) -> str:
    """`HH:MM:SS Speaker` header lines, the text on the lines below."""
    return "\n".join(f"{clock(t)} {who}\n{text}" for t, who, text in turns) + "\n"


def inline(turns: list[tuple[int, str, str]]) -> str:
    return "\n".join(f"[{clock(t)}] {who}: {text}" for t, who, text in turns) + "\n"


def _vtt_stamp(seconds: float) -> str:
    whole = int(seconds)
    ms = int(round((seconds - whole) * 1000))
    return f"{clock(whole)}.{ms:03d}"


def vtt(cues: list[tuple[float, float, str, str]], *, voice_tags: bool = True) -> str:
    """WebVTT: (start, end, speaker, text); `<v Speaker>` tags or a `Speaker:` prefix."""
    out = ["WEBVTT", ""]
    for i, (start, end, who, text) in enumerate(cues, 1):
        body = f"<v {who}>{text}</v>" if voice_tags else f"{who}: {text}"
        out += [str(i), f"{_vtt_stamp(start)} --> {_vtt_stamp(end)}", body, ""]
    return "\n".join(out)


def words(n: int, stem: str = "w") -> str:
    return " ".join(f"{stem}{i}" for i in range(n))


# A 100-second interview. Owner: 3 + 12 + 9 words; other: 20 + 4.
BASIC: list[tuple[int, str, str]] = [
    (0, OTHER, f"Welcome in. {SENTINEL} {words(17, 'q')}"),
    (10, OWNER, words(3, "a")),
    (20, OTHER, f"Tell me about Acme. {SENTINEL}"),
    (30, OWNER, words(12, "b")),
    (60, OTHER, "Thanks."),
    (70, OWNER, words(9, "c")),
    (100, OTHER, f"{SENTINEL} bye"),
]

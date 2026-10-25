"""Where a parsed transcript waits, in memory only, between "paste" and "which speaker are you".

Nothing here touches disk or the database. Each entry is bound to the job it was pasted for
and to the speaker labels it contains, expires on a monotonic clock, and is popped (claimed)
under a lock before anything is computed, so a failure means "paste again" and the text is
never retained. The store is bounded in entries and bytes; the oldest entry is evicted first.
The app clears it on shutdown. OS swap of process memory is outside its control (see
docs/how-it-works/debrief.md).
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from blastjob.core.transcript import Transcript

TTL_S = 30 * 60
MAX_ENTRIES = 5
MAX_BYTES = 4 * 1024 * 1024
_ID_BYTES = 16
_TURN_OVERHEAD_BYTES = 200


@dataclass(frozen=True)
class Entry:
    id: str
    job_id: int
    transcript: Transcript
    speakers: frozenset[str]
    label: str
    size: int
    expires: float


def transcript_bytes(transcript: Transcript) -> int:
    """What an entry costs against the byte budget: the held text and labels, plus a fixed
    overhead per turn for the objects holding them. Turn and speaker counts are capped at
    parse time, so the total cannot be inflated by many tiny turns."""
    return sum(
        len(t.text.encode()) + len(t.speaker.encode()) + _TURN_OVERHEAD_BYTES
        for t in transcript.turns
    )


class UploadStore:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = TTL_S,
        max_entries: int = MAX_ENTRIES,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self._clock = clock
        self._ttl = ttl
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._entries: dict[str, Entry] = {}  # insertion order = age, oldest first

    def __len__(self) -> int:
        with self._lock:
            self._sweep()
            return len(self._entries)

    def _sweep(self) -> None:
        now = self._clock()
        for key in [k for k, e in self._entries.items() if e.expires <= now]:
            del self._entries[key]

    def put(self, job_id: int, transcript: Transcript, label: str = "") -> str:
        size = transcript_bytes(transcript)
        if size > self._max_bytes:
            raise ValueError("That transcript is too large to hold.")
        entry_id = secrets.token_urlsafe(_ID_BYTES)
        with self._lock:
            self._sweep()
            while self._entries and (
                len(self._entries) >= self._max_entries
                or sum(e.size for e in self._entries.values()) + size > self._max_bytes
            ):
                del self._entries[next(iter(self._entries))]
            self._entries[entry_id] = Entry(
                entry_id,
                job_id,
                transcript,
                frozenset(transcript.speakers),
                label,
                size,
                self._clock() + self._ttl,
            )
        return entry_id

    def peek(self, entry_id: str, job_id: int) -> Entry | None:
        """The entry for this job, or None (unknown, expired, or another job's)."""
        with self._lock:
            self._sweep()
            entry = self._entries.get(entry_id)
            return entry if entry is not None and entry.job_id == job_id else None

    def claim(self, entry_id: str, job_id: int, speaker: str) -> Entry | None:
        """Pop the entry atomically if it is this job's and `speaker` is one of its labels.
        Otherwise leave it in place and return None."""
        with self._lock:
            self._sweep()
            entry = self._entries.get(entry_id)
            if entry is None or entry.job_id != job_id or speaker not in entry.speakers:
                return None
            del self._entries[entry_id]
            return entry

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

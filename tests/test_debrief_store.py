"""The in-memory holding area for a parsed transcript: bound, expiring, claimed once (#84)."""

from __future__ import annotations

import threading

import pytest

from blastjob.core.debrief_store import MAX_ENTRIES, TTL_S, UploadStore, transcript_bytes
from blastjob.core.transcript import parse_transcript
from tests.debrief_fixtures import BASIC, OTHER, OWNER, header


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> UploadStore:
    return UploadStore(clock=clock)


def transcript():
    return parse_transcript(header(BASIC))


def test_a_stored_upload_is_peeked_for_its_own_job_only(store):
    uid = store.put(7, transcript())
    assert store.peek(uid, 7) is not None
    assert store.peek(uid, 8) is None
    assert store.peek("nope", 7) is None


def test_claim_returns_the_entry_once_and_removes_it(store):
    uid = store.put(7, transcript())
    entry = store.claim(uid, 7, OWNER)
    assert entry is not None and entry.job_id == 7
    assert store.claim(uid, 7, OWNER) is None
    assert len(store) == 0


def test_claim_for_another_job_or_an_invalid_speaker_leaves_the_entry(store):
    uid = store.put(7, transcript())
    assert store.claim(uid, 8, OWNER) is None
    assert store.claim(uid, 7, "Nobody") is None
    assert len(store) == 1


def test_entries_expire_after_the_ttl_and_are_swept_on_the_next_access(store, clock):
    uid = store.put(7, transcript())
    clock.now += TTL_S + 1
    assert store.peek(uid, 7) is None
    assert len(store) == 0


def test_a_new_put_sweeps_expired_entries(store, clock):
    store.put(7, transcript())
    clock.now += TTL_S + 1
    store.put(7, transcript())
    assert len(store) == 1


def test_entry_count_is_bounded_and_the_oldest_is_evicted(store, clock):
    ids = []
    for _ in range(MAX_ENTRIES + 2):
        ids.append(store.put(7, transcript()))
        clock.now += 1
    assert len(store) == MAX_ENTRIES
    assert store.peek(ids[0], 7) is None
    assert store.peek(ids[-1], 7) is not None


def test_total_bytes_are_bounded_and_the_oldest_is_evicted(clock):
    size = transcript_bytes(transcript())
    store = UploadStore(clock=clock, max_bytes=size * 2 + 10)
    ids = []
    for _ in range(3):
        ids.append(store.put(1, transcript()))
        clock.now += 1
    assert len(store) == 2
    assert store.peek(ids[0], 1) is None


def test_one_upload_larger_than_the_whole_budget_is_refused(clock):
    store = UploadStore(clock=clock, max_bytes=10)
    with pytest.raises(ValueError, match="too large"):
        store.put(1, transcript())


def test_clear_empties_the_store(store):
    store.put(7, transcript())
    store.clear()
    assert len(store) == 0


def test_concurrent_claims_yield_exactly_one_winner(store):
    uid = store.put(7, transcript())
    results: list[object] = []
    barrier = threading.Barrier(8)

    def go() -> None:
        barrier.wait()
        results.append(store.claim(uid, 7, OTHER))

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 1

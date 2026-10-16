"""Round 2, R6: the per-resume refine lease is held by a live process, not by an
age. A slow but live refine is never stolen, and two waiters never both win."""

from __future__ import annotations

import os
import threading

import pytest

from blastjob.core.refine_files import RefineBusyError, lease


def test_a_live_lease_older_than_any_stale_age_is_not_stolen(tmp_path):
    with lease(tmp_path, "S"):
        lock = tmp_path / ".refine" / "S.lock"
        os.utime(lock, (1, 1))  # looks ancient, but its holder is alive

        with pytest.raises(RefineBusyError):
            with lease(tmp_path, "S"):
                pass


def test_a_lock_file_whose_holder_is_gone_is_taken_over(tmp_path):
    lock = tmp_path / ".refine" / "S.lock"
    lock.parent.mkdir()
    lock.write_text("999999")  # nobody holds it (a crashed process)

    with lease(tmp_path, "S"):
        pass

    assert not lock.exists()


def test_the_lease_is_released_for_the_next_refine(tmp_path):
    with lease(tmp_path, "S"):
        pass
    with lease(tmp_path, "S"):
        pass


def test_concurrent_waiters_on_a_dead_lock_never_both_hold_it(tmp_path):
    lock = tmp_path / ".refine" / "S.lock"
    lock.parent.mkdir()
    lock.write_text("999999")
    os.utime(lock, (1, 1))
    barrier = threading.Barrier(8)
    guard = threading.Lock()
    state = {"now": 0, "peak": 0, "won": 0}

    def contend():
        barrier.wait()
        try:
            with lease(tmp_path, "S"):
                with guard:
                    state["now"] += 1
                    state["won"] += 1
                    state["peak"] = max(state["peak"], state["now"])
                threading.Event().wait(0.05)
                with guard:
                    state["now"] -= 1
        except RefineBusyError:
            pass

    threads = [threading.Thread(target=contend) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state["peak"] == 1
    assert state["won"] >= 1

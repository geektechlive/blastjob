"""The background-run registry: what happens to a run nobody is watching, a
run_id nobody knows, and a second click on a button that costs money.

Four failures this file pins down:

- an unknown run_id made `stream()` return immediately, so the SSE route answered
  200 with an empty body; `_build_panel.html` only closes on `done` or an `error`
  WITH data, so the browser reconnected forever and the panel sat on
  "Tailoring...". That is the state after a cockpit restart with a panel open.
- `asyncio.create_task` with no saved reference: the loop keeps only a weak one,
  so a discovery/matching/tailor job could be garbage-collected mid-run while the
  UI still showed `running`.
- `_runs` was never evicted, and a tailor/prep event log holds the entire streamed
  LLM output one Event per chunk. Under a launchd KeepAlive service that grows
  for the life of the process.
- `start()` registered and scheduled unconditionally, so a double-click on
  "Find + Score" ran two full discovery+matching passes against one SQLite file,
  and `active()` showed only one of them.
"""

from __future__ import annotations

import asyncio
import gc
import time

from blastjob.web import runtime as rt
from blastjob.web import runtime as runtime

# ----- 3. an unknown run_id must not answer with silence ----------------------


async def test_unknown_run_id_yields_an_error_event_with_data():
    reg = rt.Runtime()
    events = [e async for e in reg.stream("tailor-1-gone")]
    assert len(events) == 1
    assert events[0].kind == "error"
    assert events[0].data, "an error frame with no data reads as a connection blip and reconnects"


async def test_unknown_run_id_respects_last_event_id():
    """The panel resumes from Last-Event-ID; an id it has already seen is
    dropped by EventSource, so the synthetic frame has to come after it."""
    reg = rt.Runtime()
    events = [e async for e in reg.stream("tailor-1-gone", last_id=41)]
    assert [e.id for e in events] == [42]


# ----- 4. the driver task must not be garbage-collected mid-run ---------------


async def test_running_task_survives_a_garbage_collection():
    reg = rt.Runtime()
    gate = asyncio.Event()

    async def work(run):
        await gate.wait()
        run.emit("log", "finished")
        return "ok"

    run = reg.start("discovery", work)
    await asyncio.sleep(0)
    gc.collect()
    gate.set()
    for _ in range(20):
        await asyncio.sleep(0)
        if run.status != "running":
            break
    assert run.status == "done" and run.result == "ok"


async def test_finished_task_reference_is_released():
    reg = rt.Runtime()

    async def work(run):
        return 1

    reg.start("discovery", work)
    for _ in range(20):
        await asyncio.sleep(0)
        if not reg._tasks:
            break
    assert reg._tasks == set()


# ----- 5. finished runs are evicted ------------------------------------------


async def test_old_finished_run_is_evicted_on_the_next_start():
    reg = rt.Runtime()
    old = rt.Run(id="tailor-1-a", name="tailor", status="done")
    old.finished_at = rt.time.monotonic() - rt.RUN_RETENTION_SECONDS - 1
    old.emit("output", "x" * 1000)
    reg._runs[old.id] = old

    async def work(run):
        return None

    reg.start("prep", work)
    assert old.id not in reg._runs


async def test_eviction_never_touches_a_running_run_or_one_being_streamed():
    reg = rt.Runtime()
    running = rt.Run(id="tailor-1-a", name="tailor")
    running.finished_at = rt.time.monotonic() - rt.RUN_RETENTION_SECONDS - 1  # nonsense, but safe
    watched = rt.Run(id="tailor-2-b", name="tailor", status="done")
    watched.finished_at = rt.time.monotonic() - rt.RUN_RETENTION_SECONDS - 1
    watched._waiters.append(asyncio.Event())
    reg._runs[running.id] = running
    reg._runs[watched.id] = watched

    async def work(run):
        return None

    reg.start("prep", work)
    assert running.id in reg._runs and watched.id in reg._runs


async def test_retention_outlives_the_done_refresh_banner():
    """`active()` shows a finished run for `recent_seconds` (300s default). Evict
    sooner than that and the "done -- refresh" notice silently stops appearing."""
    assert rt.RUN_RETENTION_SECONDS > 300.0


async def test_a_recently_finished_run_is_kept():
    reg = rt.Runtime()
    fresh = rt.Run(id="tailor-1-a", name="tailor", status="done")
    fresh.finished_at = rt.time.monotonic()
    reg._runs[fresh.id] = fresh

    async def work(run):
        return None

    reg.start("prep", work)
    assert fresh.id in reg._runs


# ----- 6. no duplicate discovery/matching pass -------------------------------


async def test_second_start_of_a_running_name_is_refused():
    reg = rt.Runtime()
    gate = asyncio.Event()
    calls = []

    async def work(run):
        calls.append(run.id)
        await gate.wait()
        return None

    first = reg.start("discovery", work)
    await asyncio.sleep(0)
    second = reg.start("discovery", work)
    assert second is first, "the caller must get the in-flight run, not a second pass"
    assert calls == [first.id]
    assert len(reg._runs) == 1
    gate.set()
    for _ in range(20):
        await asyncio.sleep(0)
        if first.status != "running":
            break


async def test_a_new_run_starts_once_the_previous_one_finished():
    reg = rt.Runtime()

    async def work(run):
        return None

    first = reg.start("discovery", work)
    for _ in range(20):
        await asyncio.sleep(0)
        if first.status != "running":
            break
    second = reg.start("discovery", work)
    assert second is not first


async def test_per_job_runs_are_not_single_flighted():
    """Tailoring is guarded by claim_for_build's CAS per job; blocking tailor B
    because tailor A is running would be a functional regression."""
    reg = rt.Runtime()
    gate = asyncio.Event()

    async def work(run):
        await gate.wait()
        return None

    a = reg.start("tailor", work)
    await asyncio.sleep(0)
    b = reg.start("tailor", work)
    assert b is not a
    gate.set()
    for _ in range(20):
        await asyncio.sleep(0)
        if a.status != "running" and b.status != "running":
            break


# ----- memory bounds ---------------------------------------------------------
#
# The cockpit is a KeepAlive launchd service: it never restarts on its own, so
# anything unbounded here grows until the machine notices. Two leaks were found
# on 2026-09-16 review: a run's event log had no cap (one Event per streamed LLM
# chunk), and a browser tab left open on a finished run's SSE stream pinned that
# run forever, because _prune refuses to evict a run with a waiter.


async def test_a_runs_event_log_is_capped():
    rt = runtime.Runtime()
    run = runtime.Run(id="r1", name="tailor")
    for i in range(runtime.MAX_EVENTS_PER_RUN * 2):
        run.emit("output", f"chunk {i}")
    assert len(run.events) <= runtime.MAX_EVENTS_PER_RUN
    # The newest output must survive -- that is what a reconnecting browser wants.
    assert run.events[-1].data.endswith(f"{runtime.MAX_EVENTS_PER_RUN * 2 - 1}")
    assert rt is not None


async def test_an_abandoned_sse_waiter_cannot_pin_a_finished_run_forever():
    """A tab left open on a finished run used to keep the run and its whole
    event log resident for the life of the process."""
    rt = runtime.Runtime()
    run = rt.start("tailor", _noop_work)
    await asyncio.sleep(0)
    run.status = "done"
    run.finished_at = time.monotonic() - (runtime.RUN_RETENTION_SECONDS * 2)
    run._waiters.append(asyncio.Event())  # the abandoned reader

    rt._prune()
    assert run.id not in rt._runs, "a long-finished run must be evictable despite a waiter"


async def _noop_work(run):
    return None

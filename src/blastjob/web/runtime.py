"""In-process registry for long-running background work (discovery, matching,
tailoring builds).

Each run has a monotonic, replayable event log so an SSE consumer that reconnects
(browser refresh mid-build) can resume from Last-Event-ID instead of missing
output or restarting the work (Codex #12). Runs continue regardless of whether a
browser is attached.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from blastjob import logging as applog
from blastjob.core.run_lock import RunLockHeld
from blastjob.llm import gateway, ledger
from blastjob.web import progress as progress_mod

log = logging.getLogger(__name__)

# Runs that must not overlap. Discovery and matching write the same SQLite file
# and pay for LLM scoring, and `claim_for_build`'s CAS only protects tailoring,
# so a double-click on "Find + Score" (or a second open tab) used to launch two
# full passes while the banner showed one. Per-job names (tailor/prep/research)
# stay off this list: blocking tailor B because tailor A is running would be a
# functional regression. Ingest overwrites work_history.md -- the single most
# irreplaceable file in the system -- so two concurrent ingests racing to write
# it is exactly the kind of overlap this list exists to prevent.
SINGLE_FLIGHT = ("discovery", "matching", "ingest")

# Names that share one slot. Discovery and matching both score jobs and write
# the same rows, so a "Find + Score" click during a Match run (or the reverse)
# used to run two scoring passes at once. Names absent here are their own group.
FLIGHT_GROUPS: dict[str, str] = {"discovery": "scoring", "matching": "scoring"}

# How long a finished run is kept. Must stay comfortably above `active()`'s
# `recent_seconds` default (300s) or the "done -- refresh" banner silently stops
# appearing. Eviction matters because a tailor/prep/outreach event log holds the
# whole streamed LLM output, one Event per chunk, and the cockpit runs as a
# launchd KeepAlive service that never restarts on its own.
RUN_RETENTION_SECONDS = 1800.0

# Hard ceiling in case a burst of runs all land inside the retention window.
MAX_RUNS = 200

# Per-run event ceiling. One Event is appended per streamed LLM chunk, so a long
# tailor run accumulates thousands, and the cockpit is a KeepAlive service that
# never restarts on its own. The oldest events are dropped first: a browser
# reconnecting with Last-Event-ID wants the tail, and the run's own transcript is
# already on disk in the packet. Chosen to comfortably hold a full build's
# output, not calibrated against anything finer.
MAX_EVENTS_PER_RUN = 2000

# Cockpit runs that may make model calls at once when no limits are installed
# (`limits.cockpit_concurrency` otherwise).
DEFAULT_CONCURRENCY = 3

# After this long, a finished run is evicted even if an SSE consumer is still
# parked on it. A browser tab left open on a finished run used to pin that run
# and its whole event log for the life of the process; the waiter is woken by
# eviction and gets the standard "run is no longer available" frame, which the
# page already handles.
ABANDONED_WAITER_GRACE_SECONDS = RUN_RETENTION_SECONDS * 2

_GONE = (
    "This run is no longer available -- the cockpit restarted or the run aged out. Reload the page."
)


@dataclass
class Event:
    id: int
    kind: str  # "log" | "progress" | "done" | "error"
    data: str

    def progress(self) -> progress_mod.Progress | None:
        """This event's structured progress, or `None` if it isn't a
        "progress" kind or its payload doesn't parse (old free text, or a
        malformed payload)."""
        if self.kind != "progress":
            return None
        return progress_mod.parse(self.data)


@dataclass
class Run:
    id: str
    name: str
    key: str | None = None  # per-(name, key) single flight; None = not deduplicated
    status: str = "running"  # running | done | failed | cancelled
    # True while the run waits for a model slot (`limits.cockpit_concurrency`). The status stays
    # "running": single-flight, the banner and the SSE stream all key off it.
    queued: bool = False
    started_at: float = field(default_factory=time.monotonic)
    task: asyncio.Task[None] | None = None
    stop_requested: bool = False
    stop_requested_at: float = 0.0
    stopped_too_late: bool = False  # Stop was asked for, but the run finished normally
    events: list[Event] = field(default_factory=list)
    result: Any = None
    finished_at: float | None = None  # time.monotonic() when status left "running"
    _next_id: int = 1
    _waiters: list[asyncio.Event] = field(default_factory=list)

    def emit(self, kind: str, data: str) -> None:
        self.events.append(Event(self._next_id, kind, data))
        self._next_id += 1
        # Bounded: ids keep increasing, so Last-Event-ID replay still works —
        # a consumer that asks for an id older than the window simply gets
        # everything still held, which is the tail it needs.
        if len(self.events) > MAX_EVENTS_PER_RUN:
            del self.events[: len(self.events) - MAX_EVENTS_PER_RUN]
        for w in self._waiters:
            w.set()

    def display_event(self) -> Event | None:
        """The event a status line should show. See
        `progress.display_event` -- this exists so templates can call it
        directly on the `run` they're already given."""
        return progress_mod.display_event(self.events)

    def latest_progress(self) -> progress_mod.Progress | None:
        """The most recent structured progress reading, or `None` if no
        progress event has landed yet (or the newest one doesn't parse)."""
        return progress_mod.latest_progress(self.events)


class Runtime:
    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._seq = 0
        # The loop holds only a weak reference to a task, so a run whose task
        # object is dropped can be garbage-collected mid-flight -- silently
        # killing a discovery, matching or tailor job while the UI still says
        # "running". Hold a strong reference until it completes.
        self._tasks: set[asyncio.Task[None]] = set()
        # The concurrency gate, one per (event loop, size): an `asyncio.Semaphore` binds to the
        # loop it first waits on, and a settings edit can change the size.
        self._gate: tuple[asyncio.AbstractEventLoop, int, asyncio.Semaphore] | None = None

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def running_runs(self) -> list[Run]:
        """Runs still going (running or queued), oldest first, for the /runs page."""
        return [r for r in self._runs.values() if r.status == "running"]

    def stop(self, run_id: str) -> bool:
        """Cancel a run's task (the gateway's reap then kills the model child). Idempotent: a
        second Stop while the first is still unwinding does nothing, because a second
        `cancel()` would interrupt the reap's waits. False when there is nothing to stop."""
        run = self._runs.get(run_id)
        if run is None or run.status != "running" or run.task is None:
            return False
        if not run.stop_requested:
            run.stop_requested = True
            run.stop_requested_at = time.monotonic()
            run.task.cancel()
        return True

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        inst = ledger.installed()
        size = inst.limits.cockpit_concurrency if inst is not None else DEFAULT_CONCURRENCY
        if self._gate is None or self._gate[0] is not loop or self._gate[1] != size:
            self._gate = (loop, size, asyncio.Semaphore(size))
        return self._gate[2]

    @asynccontextmanager
    async def slot(self, run: Run | None = None) -> AsyncIterator[None]:
        """Hold one model slot for the block. A caller that has to wait is shown as queued; a
        run cancelled while it waits never releases a slot it did not hold."""
        sem = self._semaphore()
        if run is not None and sem.locked():
            run.queued = True
            run.emit("log", "Queued: waiting for a free model slot.")
        try:
            async with sem:
                if run is not None:
                    run.queued = False
                yield
        finally:
            if run is not None:
                run.queued = False

    def active(self, names: tuple[str, ...], recent_seconds: float = 300.0) -> Run | None:
        """The run the global status banner should show: the newest *running*
        run among `names`, else the newest one that finished within
        `recent_seconds` (so a "done — refresh" notice survives navigating
        between pages), else None."""
        candidates = [r for r in reversed(list(self._runs.values())) if r.name in names]
        for run in candidates:
            if run.status == "running":
                return run
        now = time.monotonic()
        for run in candidates:
            if run.finished_at is not None and now - run.finished_at <= recent_seconds:
                return run
        return None

    def _running(self, name: str) -> Run | None:
        group = FLIGHT_GROUPS.get(name, name)
        for run in reversed(list(self._runs.values())):
            if FLIGHT_GROUPS.get(run.name, run.name) == group and run.status == "running":
                return run
        return None

    def running_keyed(self, name: str, key: str) -> Run | None:
        """The run of `name` for `key` that is still going, if any."""
        return self._running_keyed(name, key)

    def _running_keyed(self, name: str, key: str) -> Run | None:
        for run in reversed(list(self._runs.values())):
            if run.name == name and run.key == key and run.status == "running":
                return run
        return None

    def _prune(self) -> None:
        """Drop finished runs (and their event logs) once nothing can still want
        them. Never a run that is still going, and never one an SSE consumer is
        parked on."""
        now = time.monotonic()

        def evictable(run: Run) -> bool:
            if run.status == "running":
                return False
            if not run._waiters:
                return True
            # A parked consumer normally protects a run. But a browser tab left
            # open forever is indistinguishable from an attentive one, and it
            # used to pin the run permanently, so a waiter only buys extra time.
            return (
                run.finished_at is not None
                and now - run.finished_at > ABANDONED_WAITER_GRACE_SECONDS
            )

        for rid, run in list(self._runs.items()):
            if (
                evictable(run)
                and run.finished_at is not None
                and now - run.finished_at > RUN_RETENTION_SECONDS
            ):
                del self._runs[rid]
        for rid, run in list(self._runs.items()):
            if len(self._runs) <= MAX_RUNS:
                break
            if evictable(run):
                del self._runs[rid]

    def start(
        self, name: str, work: Callable[[Run], Awaitable[Any]], key: str | None = None
    ) -> Run:
        """`start_run` without the scheduled flag, for callers that don't care."""
        return self.start_run(name, work, key)[0]

    def start_run(
        self, name: str, work: Callable[[Run], Awaitable[Any]], key: str | None = None
    ) -> tuple[Run, bool]:
        """Schedule `work(run)` on the event loop. `work` uses run.emit(...) for
        progress/log/output; its return value becomes run.result.

        For a SINGLE_FLIGHT name with a pass already in flight, `work` is NOT
        scheduled and the in-flight Run is handed back, so the caller's status
        panel shows the real run instead of hiding it behind a duplicate. The same
        holds per `(name, key)` when a `key` is given (a job id, a company key): a
        double-click on Prep for one job hands back its running run, while Prep for
        another job still starts. No key means no deduplication.

        Returns `(run, scheduled)`: `scheduled` is False when the in-flight run was
        handed back and `work` will never run, so a caller that spooled resources
        for `work` (a temp dir of uploads) knows to release them itself."""
        existing = self._running(name) if name in SINGLE_FLIGHT else None
        if existing is None and key is not None:
            existing = self._running_keyed(name, key)
        if existing is not None:
            return existing, False
        self._prune()
        self._seq += 1
        # Include a random suffix so run_id is unique across server restarts
        # (build_runs.run_id is UNIQUE; a bare counter resets to 1 on restart and
        # collides with rows from a prior session).
        run_id = f"{name}-{self._seq}-{uuid.uuid4().hex[:8]}"
        run = Run(id=run_id, name=name, key=key)
        self._runs[run_id] = run

        async def _driver() -> None:
            try:
                async with self.slot(run):
                    with (
                        gateway.origin_scope("clicked"),
                        gateway.caller_scope(run.name),
                        gateway.refusal_scope(),
                    ):
                        run.result = await work(run)
                run.status = "done"
                run.finished_at = time.monotonic()
                if run.stop_requested:
                    run.stopped_too_late = True
                    run.emit("log", "Stop arrived too late: the run had already finished.")
                run.emit("done", "")
            except asyncio.CancelledError:
                # Stop (or shutdown): say so honestly, then let the cancellation continue.
                run.status = "cancelled"
                run.finished_at = time.monotonic()
                run.emit("error", "Stopped.")
                raise
            except RunLockHeld as e:
                # Not a fault: another run holds the lock. Say so plainly, no class name.
                log.info("run %s refused: %s", run.name, e)
                run.status = "failed"
                run.finished_at = time.monotonic()
                run.emit("error", str(e))
            except Exception as e:  # noqa: BLE001 — surface, don't crash the server
                applog.log_exception(f"run {run.name}", e)
                run.status = "failed"
                run.finished_at = time.monotonic()
                run.emit("error", applog.redact(f"{type(e).__name__}: {e}"))

        task = asyncio.create_task(_driver())
        run.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        def _cancelled_before_it_began(t: asyncio.Task[None]) -> None:
            # A task cancelled before its first step never enters `_driver`'s try block.
            if t.cancelled() and run.status == "running":
                run.status = "cancelled"
                run.finished_at = time.monotonic()
                run.emit("error", "Stopped.")

        task.add_done_callback(_cancelled_before_it_began)
        return run, True

    async def stream(self, run_id: str, last_id: int = 0):
        """Async generator of Events after `last_id`, ending once the run is done
        and all events are delivered. Supports Last-Event-ID replay."""
        run = self._runs.get(run_id)
        if run is None:
            # Returning nothing made the SSE route answer 200 with an empty
            # body, which _build_panel.html cannot tell from "still connecting":
            # it reconnected every few seconds forever and the panel sat on
            # "Tailoring...". That is the state after a cockpit restart with a
            # panel open. An `error` frame WITH data closes the stream and says
            # why (a data-less error frame reads as a connection blip).
            yield Event(last_id + 1, "error", _GONE)
            return
        while True:
            pending = [e for e in run.events if e.id > last_id]
            for e in pending:
                last_id = e.id
                yield e
            if run.status != "running" and not [e for e in run.events if e.id > last_id]:
                return
            waiter = asyncio.Event()
            run._waiters.append(waiter)
            try:
                await asyncio.wait_for(waiter.wait(), timeout=15)
            except TimeoutError:
                pass
            finally:
                run._waiters.remove(waiter)


# Module-level singleton the app shares.
RUNTIME = Runtime()

"""The one door every model call goes through (docs/how-it-works/llm.md).

Nothing else in `src/` builds an SDK client or spawns the claude CLI
(`tests/test_model_gateway_guard.py` fails if it does). The gateway owns:

- the **absolute deadline**: a monotonic `ContextVar` set with `deadline_scope`; each call's
  limit is `min(its own timeout, time left)`, and nothing starts with under 30 s left;
- the **origin tag** (`scheduled` or `clicked`, default `clicked`), so a missing tag is never
  treated as free;
- **SDK clients** with `max_retries=0` and a bounded timeout;
- **the supervised CLI child** (`llm/supervise.py`): launch, and the reap that kills its group;
- `reserve` / `finish`: the call ledger and the atomic daily budget (`llm/ledger.py`), a no-op
  until `config.apply_runtime_settings` installs a database and the limits.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from blastjob.llm import ledger

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

Origin = Literal["scheduled", "clicked"]


class CLIError(RuntimeError):
    """A model call failed: non-zero exit, an `is_error` result, a timeout, a refused start.
    Fail-soft callers catch exactly this."""


class CallRefused(CLIError):
    """The gateway refused to start a call (deadline or budget). Nothing was contacted. Fail-soft
    callers treat it as "not run", never as a failure of the item they were working on."""


class DeadlineReached(CallRefused):
    """The run's deadline is too close (or past) to start another model call."""


class BudgetExceeded(CallRefused):
    """Today's call cap for this origin is reached (`limits.*_calls_per_day`)."""


class CallTimeout(CLIError):
    """A call ran past its time limit. A `CLIError` of its own so the ledger can say `timeout`."""


# Nothing starts with less than this left: a call cut off a few seconds in is pure waste.
MIN_START_SECONDS = 30.0
# Per-operation timeout for the Anthropic and OpenAI clients when no limits are installed
# (`limits.sdk_timeout_seconds` otherwise).
SDK_TIMEOUT_SECONDS = 300.0
# Ceiling for one streamed call's whole lifecycle on the SDK providers.
STREAM_TIMEOUT_SECONDS = 900.0
# The supervisor's own deadline sits this far past the parent's, as a backstop.
SUPERVISOR_GRACE_SECONDS = 5.0
# Long enough for the supervisor's own TERM, 2 s, KILL, 2 s sequence on its child's group
# plus slack, so SIGKILLing the supervisor (which orphans that group) is a last resort.
REAP_SECONDS = 10.0

_deadline: ContextVar[float | None] = ContextVar("blastjob_model_deadline", default=None)
_state: ContextVar[DeadlineState | None] = ContextVar("blastjob_model_deadline_state", default=None)
_origin: ContextVar[Origin] = ContextVar("blastjob_model_origin", default="clicked")
_caller: ContextVar[str] = ContextVar("blastjob_model_caller", default="")


# ---------------------------------------------------------------------------
# Deadline and origin
# ---------------------------------------------------------------------------


@dataclass
class DeadlineState:
    """What a deadline scope saw. Fail-soft callers swallow `DeadlineReached` (it is a
    `CLIError`), so the run reads this afterwards to report honestly that it was cut off."""

    hit: bool = False
    hit_at: str = ""  # local wall-clock time of the first refused call (ISO, seconds)
    reason: str = ""  # why the first call was refused: "deadline" or "budget"


@contextmanager
def deadline_scope(seconds: float) -> Iterator[DeadlineState]:
    """Bound every model call made inside the block to `seconds` from now. A nested scope
    can only tighten the outer deadline, and shares the outer scope's `DeadlineState`."""
    new = time.monotonic() + seconds
    current = _deadline.get()
    outer = _state.get()
    state = outer if outer is not None else DeadlineState()
    token = _deadline.set(new if current is None else min(current, new))
    state_token = _state.set(state)
    try:
        yield state
    finally:
        _deadline.reset(token)
        _state.reset(state_token)


def deadline_hit() -> bool:
    """True once a call in this scope was refused, for the deadline or the call budget. Right
    after an item fails, this says the failure was the refusal (nothing else is attempted after
    the first one)."""
    state = _state.get()
    return state is not None and state.hit


def refused() -> bool:
    """Alias of `deadline_hit`, named for what it now covers: a refused call of either kind."""
    return deadline_hit()


def deadline_reached() -> bool:
    """True once a call was refused (deadline or budget), or too little time is left to start
    another (the same `MIN_START_SECONDS` test `call_limit` applies). False with no scope."""
    state = _state.get()
    if state is not None and state.hit:
        return True
    left = remaining()
    return left is not None and left < MIN_START_SECONDS


def refusal_label() -> str:
    """ "daily call limit" after a budget refusal, else "time limit": the words for what kept a
    step from running ("Not run (time limit)")."""
    state = _state.get()
    return "daily call limit" if state is not None and state.reason == "budget" else "time limit"


def remaining() -> float | None:
    """Seconds left before the deadline, or None when no deadline is set."""
    deadline = _deadline.get()
    return None if deadline is None else deadline - time.monotonic()


def call_limit(per_call: float) -> float:
    """`min(per_call, time left)`. Raises `DeadlineReached` when under `MIN_START_SECONDS`
    is left. With no deadline set this is `per_call`, as before."""
    left = remaining()
    if left is None:
        return per_call
    if left < MIN_START_SECONDS:
        _mark_refused("deadline")
        raise DeadlineReached(
            f"not starting a model call: {max(left, 0):.0f}s left before the run's deadline"
        )
    return min(per_call, left)


def _mark_refused(reason: str) -> None:
    state = _state.get()
    if state is not None and not state.hit:
        state.hit = True
        state.hit_at = datetime.now().isoformat(timespec="seconds")
        state.reason = reason


@contextmanager
def refusal_scope() -> Iterator[DeadlineState]:
    """Record refusals (deadline or budget) for the block even with no deadline: a cockpit run
    or a hand-run command has none, and without a state the scorer would keep trying every
    batch after the first refused call. Shares an outer scope's state."""
    outer = _state.get()
    state = outer if outer is not None else DeadlineState()
    token = _state.set(state)
    try:
        yield state
    finally:
        _state.reset(token)


@contextmanager
def caller_scope(name: str) -> Iterator[None]:
    """Name what is calling (a run, a daily step) so the ledger can group by it. Without a
    scope the ledger records the gateway call site ("make_stream")."""
    token = _caller.set(name)
    try:
        yield
    finally:
        _caller.reset(token)


@contextmanager
def origin_scope(origin: Origin) -> Iterator[None]:
    token = _origin.set(origin)
    try:
        yield
    finally:
        _origin.reset(token)


def current_origin() -> Origin:
    return _origin.get()


# ---------------------------------------------------------------------------
# Ledger and budget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reservation:
    caller: str
    origin: Origin
    row_id: int | None = None  # None: the ledger is off


def reserve(caller: str) -> Reservation:
    """Called before any provider is contacted: counts today's calls for this origin and inserts
    the `started` row in one write transaction (`llm/ledger.py`). Over the cap, writes a
    `refused_budget` row and raises `BudgetExceeded`. If the ledger itself is unusable, the call
    is refused too (`CLIError`): an unmeasured call is never waved through. Blocking: async
    callers use `reserve_async`."""
    origin = current_origin()
    name = _caller.get() or caller
    try:
        admission = ledger.reserve(name, origin)
    except Exception as e:  # noqa: BLE001 - sqlite3.Error, OSError: fail closed
        raise CLIError(f"model call ledger unavailable: {type(e).__name__}: {e}"[:300]) from e
    return _admitted(admission, name, origin)


async def reserve_async(caller: str) -> Reservation:
    """`reserve` off the event loop: the write lock can take up to the busy timeout and the
    first use runs `ps` and a reconcile. If the caller is cancelled while the reservation is in
    flight, the row it makes is closed as `cancelled` when the worker finishes."""
    origin = current_origin()
    name = _caller.get() or caller
    work = asyncio.ensure_future(asyncio.to_thread(ledger.reserve, name, origin))
    try:
        admission = await asyncio.shield(work)
    except asyncio.CancelledError:

        def _close(f: asyncio.Future[ledger.Admission]) -> None:
            if not f.cancelled() and f.exception() is None:
                ledger.finish(f.result().row_id, "cancelled")

        work.add_done_callback(_close)
        raise
    except Exception as e:  # noqa: BLE001 - sqlite3.Error, OSError: fail closed
        raise CLIError(f"model call ledger unavailable: {type(e).__name__}: {e}"[:300]) from e
    return _admitted(admission, name, origin)


def _admitted(admission: ledger.Admission, name: str, origin: Origin) -> Reservation:
    if admission.refused:
        _mark_refused("budget")
        raise BudgetExceeded(
            f"daily model call limit reached: {admission.used} of {admission.cap} "
            f"{origin} calls today"
        )
    return Reservation(caller=name, origin=origin, row_id=admission.row_id)


def finish(reservation: Reservation, outcome: str, usage: Any = None) -> None:
    """Called once when a call ends. `usage` (a provider usage object) gives the tokens and the
    cost; without it both stay unknown, never zero."""
    inst = ledger.installed()
    tokens_in = tokens_out = None
    cost = None
    if usage is not None and inst is not None:
        from blastjob.llm.cost import cost_from_usage

        priced = cost_from_usage(usage, inst.pricing)
        tokens_in, tokens_out, cost = priced.input_tokens, priced.output_tokens, priced.cost_usd
    ledger.finish(
        reservation.row_id, outcome, input_tokens=tokens_in, output_tokens=tokens_out, cost_usd=cost
    )


def outcome_for(exc: BaseException | None) -> str:
    if exc is None:
        return "ok"
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(exc, (CallTimeout, TimeoutError)):
        return "timeout"
    return "error"


def sdk_timeout() -> float:
    """The SDK clients' per-operation timeout: the installed `limits.sdk_timeout_seconds`."""
    inst = ledger.installed()
    return float(inst.limits.sdk_timeout_seconds) if inst is not None else SDK_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# SDK clients
# ---------------------------------------------------------------------------


def anthropic_client(api_key: str | None) -> AsyncAnthropic:
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=api_key, max_retries=0, timeout=call_limit(sdk_timeout()))


def openai_client(api_key: str | None) -> Any:
    """Raises ImportError when the optional `openai` package is missing."""
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=api_key, max_retries=0, timeout=call_limit(sdk_timeout()))


def _sdk_error_types() -> tuple[type[BaseException], ...]:
    from anthropic import APIError

    types: tuple[type[BaseException], ...] = (APIError,)
    try:
        from openai import OpenAIError
    except ImportError:
        return types
    return (*types, OpenAIError)


@contextmanager
def sdk_errors() -> Iterator[None]:
    """Turn a provider SDK failure (API error, connection error, SDK-side timeout) into a
    `CLIError`. With retries off, these would otherwise escape callers that handle only
    `CLIError`."""
    try:
        yield
    except _sdk_error_types() as e:
        raise CLIError(f"model API error: {type(e).__name__}: {e}"[:300]) from e


class ClosingManager:
    """Wraps an SDK stream manager so the client that made it is closed when the stream ends
    (otherwise its httpx connection pool leaks, as each call builds a fresh client)."""

    def __init__(self, manager: Any, client: Any) -> None:
        self._manager = manager
        self._client = client

    async def __aenter__(self) -> Any:
        return await self._manager.__aenter__()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            return await self._manager.__aexit__(exc_type, exc, tb)
        finally:
            await self._client.close()


class BoundedStream:
    """Wraps any provider's stream so its whole lifecycle ends by `limit` seconds and the
    ledger hook sees one `finish`. Every wait is capped by an absolute loop-time deadline
    (no timeout scope is held across a `yield`, which would leak into the consumer)."""

    def __init__(self, inner: Any, limit: float, reservation: Reservation) -> None:
        self._inner = inner
        self._reservation = reservation
        self._end = asyncio.get_running_loop().time() + limit
        self._limit = limit
        self._live: Any = None
        self._usage: Any = None
        self._finished = False

    def __del__(self) -> None:
        # Obtained but never entered (or abandoned mid-way): do not leave a 'started' row
        # that its still-live owner would keep looking alive.
        if not self._finished:
            finish(self._reservation, "abandoned")

    def _close(self, outcome: str, usage: Any = None) -> None:
        self._finished = True
        finish(self._reservation, outcome, usage)

    def _left(self) -> float:
        return max(self._end - asyncio.get_running_loop().time(), 0.001)

    async def _bounded(self, awaitable: Any) -> Any:
        try:
            with sdk_errors():
                return await asyncio.wait_for(awaitable, self._left())
        except TimeoutError:
            raise CallTimeout(f"model call timed out after {self._limit:g}s") from None

    async def __aenter__(self) -> BoundedStream:
        try:
            self._live = await self._bounded(self._inner.__aenter__())
        except BaseException as e:
            # __aexit__ never runs when __aenter__ raises: close the ledger row here.
            self._close(outcome_for(e))
            raise
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            return await self._inner.__aexit__(exc_type, exc, tb)
        finally:
            self._close(outcome_for(exc), self._usage)

    @property
    def text_stream(self) -> AsyncIterator[str]:
        return self._iter_text()

    async def _iter_text(self) -> AsyncIterator[str]:
        source = aiter(self._live.text_stream)
        while True:
            try:
                chunk = await self._bounded(anext(source))
            except StopAsyncIteration:
                return
            yield chunk

    async def get_final_message(self) -> Any:
        message = await self._bounded(self._live.get_final_message())
        self._usage = getattr(message, "usage", None)
        return message


# ---------------------------------------------------------------------------
# The supervised CLI child
# ---------------------------------------------------------------------------


@dataclass
class SupervisedChild:
    """The supervisor process plus the write end of its parent-death pipe. The pipe stays
    open for the call's life; closing it (or this process dying) tells the supervisor the
    owner is gone."""

    proc: asyncio.subprocess.Process
    owner_fd: int | None

    def close_owner_fd(self) -> None:
        if self.owner_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.owner_fd)
            self.owner_fd = None


async def spawn_cli(
    args: list[str], *, cwd: str, env: dict[str, str], limit: float
) -> SupervisedChild:
    """Start `args` under `llm/supervise.py`. Raises OSError if the supervisor cannot start."""
    read_fd, write_fd = os.pipe()  # not inheritable: only `read_fd` is passed on
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",  # isolated: no cwd or PYTHONPATH on sys.path, so nothing can shadow stdlib
            "-m",
            "blastjob.llm.supervise",
            "--deadline-s",
            f"{limit + SUPERVISOR_GRACE_SECONDS:.1f}",
            "--parent-fd",
            str(read_fd),
            "--",
            *args,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=(read_fd,),
        )
    except BaseException:
        os.close(write_fd)
        raise
    finally:
        os.close(read_fd)
    return SupervisedChild(proc=proc, owner_fd=write_fd)


def _signal_group(pid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


async def _stop(proc: asyncio.subprocess.Process) -> None:
    # A finished supervisor has already confirmed its child's group is gone.
    if getattr(proc, "returncode", 0) is not None:
        return
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int):
        _signal_group(pid, signal.SIGTERM)  # the supervisor TERMs, then KILLs, its child
    else:
        proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), REAP_SECONDS)
    if proc.returncode is not None:
        return
    if isinstance(pid, int):
        _signal_group(pid, signal.SIGKILL)
    else:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), REAP_SECONDS)


async def reap(child: SupervisedChild) -> None:
    """Make sure the child tree is gone: runs on success, error, timeout, cancel and an early
    `aclose()` alike. The pipe closes last so the supervisor also has the owner-gone signal."""
    try:
        await _stop(child.proc)
    finally:
        child.close_owner_fd()

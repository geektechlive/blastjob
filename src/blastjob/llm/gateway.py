"""The one door every model call goes through (docs/how-it-works/llm.md).

Nothing else in `src/` builds an SDK client or spawns the claude CLI
(`tests/test_model_gateway_guard.py` fails if it does). The gateway owns:

- the **absolute deadline**: a monotonic `ContextVar` set with `deadline_scope`; each call's
  limit is `min(its own timeout, time left)`, and nothing starts with under 30 s left;
- the **origin tag** (`scheduled` or `clicked`, default `clicked`), so a missing tag is never
  treated as free;
- **SDK clients** with `max_retries=0` and a bounded timeout;
- **the supervised CLI child** (`llm/supervise.py`): launch, and the reap that kills its group;
- `reserve` / `finish`, the hook points for the call ledger and budget (no-ops until then).
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

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

Origin = Literal["scheduled", "clicked"]


class CLIError(RuntimeError):
    """A model call failed: non-zero exit, an `is_error` result, a timeout, a refused start.
    Fail-soft callers catch exactly this."""


class DeadlineReached(CLIError):
    """The run's deadline is too close (or past) to start another model call."""


# Nothing starts with less than this left: a call cut off a few seconds in is pure waste.
MIN_START_SECONDS = 30.0
# Per-operation timeout for the Anthropic and OpenAI clients.
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


# ---------------------------------------------------------------------------
# Deadline and origin
# ---------------------------------------------------------------------------


@dataclass
class DeadlineState:
    """What a deadline scope saw. Fail-soft callers swallow `DeadlineReached` (it is a
    `CLIError`), so the run reads this afterwards to report honestly that it was cut off."""

    hit: bool = False
    hit_at: str = ""  # local wall-clock time of the first refused call (ISO, seconds)


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
    """True once a call in this scope was refused for the deadline. Right after an item fails,
    this says the failure was the refusal (nothing else is attempted after the first one)."""
    state = _state.get()
    return state is not None and state.hit


def deadline_reached() -> bool:
    """True once a call was refused for the deadline, or too little time is left to start
    another (the same `MIN_START_SECONDS` test `call_limit` applies). False with no scope."""
    state = _state.get()
    if state is not None and state.hit:
        return True
    left = remaining()
    return left is not None and left < MIN_START_SECONDS


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
        state = _state.get()
        if state is not None and not state.hit:
            state.hit = True
            state.hit_at = datetime.now().isoformat(timespec="seconds")
        raise DeadlineReached(
            f"not starting a model call: {max(left, 0):.0f}s left before the run's deadline"
        )
    return min(per_call, left)


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
# Ledger and budget hook points (filled in by the call ledger)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reservation:
    caller: str
    origin: Origin


def reserve(caller: str) -> Reservation:
    """Called before any provider is contacted. A no-op until the budget exists."""
    return Reservation(caller=caller, origin=current_origin())


def finish(reservation: Reservation, outcome: str) -> None:
    """Called once when a call ends. A no-op until the ledger exists."""
    del reservation, outcome


# ---------------------------------------------------------------------------
# SDK clients
# ---------------------------------------------------------------------------


def anthropic_client(api_key: str | None) -> AsyncAnthropic:
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=api_key, max_retries=0, timeout=call_limit(SDK_TIMEOUT_SECONDS))


def openai_client(api_key: str | None) -> Any:
    """Raises ImportError when the optional `openai` package is missing."""
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=api_key, max_retries=0, timeout=call_limit(SDK_TIMEOUT_SECONDS))


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

    def _left(self) -> float:
        return max(self._end - asyncio.get_running_loop().time(), 0.001)

    async def _bounded(self, awaitable: Any) -> Any:
        try:
            with sdk_errors():
                return await asyncio.wait_for(awaitable, self._left())
        except TimeoutError:
            raise CLIError(f"model call timed out after {self._limit:g}s") from None

    async def __aenter__(self) -> BoundedStream:
        self._live = await self._bounded(self._inner.__aenter__())
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            return await self._inner.__aexit__(exc_type, exc, tb)
        finally:
            finish(self._reservation, "ok" if exc_type is None else "error")

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
        return await self._bounded(self._live.get_final_message())


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

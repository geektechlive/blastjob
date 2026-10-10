"""The guard that keeps tests off the real LLM provider, and the check that
keeps the guard complete.

`tests/conftest.py::_no_real_llm_calls` patches `make_stream` in every module
that binds it. The drift risk is obvious: someone adds a new module with
`from blastjob.llm.providers import make_stream`, forgets the conftest list, and
that module's tests quietly start billing real calls again — exactly the leak
this guard was written for, which hid in `test_cost_truth.py` for weeks because
three fail-soft callers swallowed the evidence.

So the second test here reads the source and asserts the list is exhaustive.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from tests.conftest import _STREAM_BINDINGS

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "blastjob"


async def test_guard_blocks_an_unpatched_provider_call():
    """A module that has not patched `make_stream` must fail loudly, naming
    itself, rather than reaching the network."""
    from blastjob.core import scoring

    with pytest.raises(AssertionError, match="real LLM provider"):
        await scoring.make_stream(None, "sys", [])


async def test_guard_names_the_module_to_patch():
    from blastjob.core import research_facts

    with pytest.raises(AssertionError, match="blastjob.core.research_facts"):
        await research_facts.make_stream(None, "sys", [])


def _modules_binding_make_stream() -> set[str]:
    """Every `blastjob.*` module with a module-level `make_stream` name bound
    from `blastjob.llm.providers`. Parsed, not grepped: a comment or a docstring
    mentioning make_stream must not count as a binding."""
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "blastjob.llm.providers":
                continue
            if any(a.name == "make_stream" for a in node.names):
                rel = path.relative_to(SRC).with_suffix("")
                found.add("blastjob." + ".".join(rel.parts))
    return found


def test_stream_bindings_list_is_exhaustive():
    """If this fails, a new module imports `make_stream` and is NOT guarded:
    add it to `_STREAM_BINDINGS` in tests/conftest.py. `llm.providers` itself is
    excluded — it defines the function rather than binding a copy of it."""
    actual = _modules_binding_make_stream() - {"blastjob.llm.providers"}
    listed = set(_STREAM_BINDINGS)
    assert actual - listed == set(), (
        f"unguarded modules bind make_stream: {sorted(actual - listed)}"
    )
    assert listed - actual == set(), (
        f"_STREAM_BINDINGS lists modules that no longer bind make_stream: {sorted(listed - actual)}"
    )

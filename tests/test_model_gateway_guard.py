"""Every model call goes through `llm/gateway.py` (docs/how-it-works/llm.md).

Parsed with `ast`, so a comment or docstring that names a constructor does not count."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "blastjob"
GATEWAY = SRC / "llm" / "gateway.py"
GUARDED_CALLS = {"AsyncAnthropic", "AsyncOpenAI", "create_subprocess_exec"}
SDK_PACKAGES = {"anthropic", "openai"}

# Modules that may start a process, each with the reason. Anything else that wants a
# subprocess must join this list in review.
PROCESS_ALLOWLIST = {
    "llm/gateway.py": "launches the supervised model CLI",
    "llm/supervise.py": "the supervisor starts the model CLI's child",
    "llm/ledger.py": "runs `ps -o lstart=` to read a process start time, no model call",
    "setup_doctor.py": "runs `claude --version`, no model call",
    "web/routes_packets.py": "runs `open <folder>` to reveal a packet in Finder",
}
PROCESS_CALLS = {"system", "popen", "spawnv", "spawnl", "spawnvp", "execv", "execvp"}


def _rel(path: Path) -> str:
    return str(path.relative_to(SRC))


def _guarded_calls(path: Path) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name in GUARDED_CALLS:
            found.append((node.lineno, name))
    return found


def _imported_roots(path: Path) -> list[tuple[int, str]]:
    """Top-level package of every import, aliases included, in any scope."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found += [(node.lineno, a.name.split(".")[0]) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module.split(".")[0]))
    return found


def _process_uses(path: Path) -> list[tuple[int, str]]:
    found = [(line, root) for line, root in _imported_roots(path) if root == "subprocess"]
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name.startswith("create_subprocess_") or name in PROCESS_CALLS:
            found.append((node.lineno, name))
    return found


def _all_modules() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_only_the_gateway_builds_sdk_clients_or_spawns_the_cli():
    offenders = [
        f"{_rel(path)}:{line} {name}"
        for path in _all_modules()
        if path != GATEWAY
        for line, name in _guarded_calls(path)
    ]
    assert not offenders, (
        f"build model clients and spawn the CLI only in llm/gateway.py: {offenders}"
    )


def test_only_the_gateway_imports_a_model_sdk_at_runtime():
    # `providers.py` may import SDK *types* for annotations; those are under TYPE_CHECKING.
    offenders = []
    for path in _all_modules():
        if path == GATEWAY:
            continue
        tree = ast.parse(path.read_text())
        type_only = {
            id(n)
            for top in ast.walk(tree)
            if isinstance(top, ast.If) and "TYPE_CHECKING" in ast.unparse(top.test)
            for n in ast.walk(top)
        }
        for node in ast.walk(tree):
            if id(node) in type_only:
                continue
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                roots = [node.module.split(".")[0]]
            else:
                continue
            offenders += [f"{_rel(path)}:{node.lineno} {r}" for r in roots if r in SDK_PACKAGES]
    assert not offenders, f"import anthropic/openai only in llm/gateway.py: {offenders}"


def test_only_allowlisted_modules_start_processes():
    offenders = [
        f"{_rel(path)}:{line} {name}"
        for path in _all_modules()
        if _rel(path) not in PROCESS_ALLOWLIST
        for line, name in _process_uses(path)
    ]
    assert not offenders, f"start processes only in {sorted(PROCESS_ALLOWLIST)}: {offenders}"


def test_the_process_allowlist_has_no_stale_entries():
    stale = [rel for rel in PROCESS_ALLOWLIST if not _process_uses(SRC / rel)]
    assert not stale, f"allowlisted modules that start no process: {stale}"


def test_the_gateway_itself_uses_all_three():
    names = {name for _, name in _guarded_calls(GATEWAY)}
    assert names == GUARDED_CALLS

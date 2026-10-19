"""The checks behind `tests/test_docs_drift.py` (issue #61).

Every check takes a repo root and returns a list of problems, each formatted
`file:line: reason: ref`. Taking a root (rather than reading this checkout) is
what lets the mutation tests build a tiny fake repo in `tmp_path` and prove a
check fails on an injected defect and passes once it is fixed.

Nothing here imports or runs blastjob: code, CLIs and config are read by AST.
"""

from __future__ import annotations

import ast
import functools
import html
import re
import subprocess
import tomllib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

TOP_DOCS = ("AGENTS.md", "CLAUDE.md", "README.md", "SETUP.md", "CONTRIBUTING.md", "SECURITY.md")
EXCLUDED_DOC_DIRS = ("docs/plans",)
# Never resolve a reference into these: they are private, generated or not in a clone.
PRIVATE_TREES = frozenset(
    {"local", ".git", ".venv", ".claude", "node_modules", "__pycache__"}
    | {".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build"}
)
DRIFT_FILES = ("tests/test_docs_drift.py", "tests/docs_drift_checks.py")
REVERSE_EXCLUDED = ("local/", "CLAUDE.local.md")

AGENTS_MAX_LINES = 150
AGENTS_MAX_BYTES = 12 * 1024

# Backticked text a doc may cite that the code-reference check cannot see:
# files that live in the user's data dir or output folder, not the repo.
#
# A bare filename (no directory part) that resolves nowhere is skipped, not
# listed here: `work_history.md`, `blastjob.db`, `metadata.json` and the like
# name runtime files. Only multi-segment runtime paths need an entry.
CODE_REF_ALLOWLIST: dict[str, str] = {
    "templates/standard.md": "data-dir file, not in the repo",
    "templates/ats.md": "data-dir file, not in the repo",
    "backups/work_history-YYYYMMDD-HHMMSS.md": "data-dir backup name pattern",
    ".gemini/settings.json": "a contributor's own tool config, not in the repo",
}
# Bold spans in docs/using/ that are not UI labels.
LABEL_ALLOWLIST: dict[str, str] = {}


# --- shared helpers -------------------------------------------------------------


def rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def doc_files(root: Path) -> list[Path]:
    """The docs set, from the working tree (not git), so new docs are seen."""
    found = [root / name for name in TOP_DOCS if (root / name).is_file()]
    docs = root / "docs"
    if docs.is_dir():
        for path in sorted(docs.rglob("*.md")):
            r = rel(root, path)
            if not any(r.startswith(d + "/") for d in EXCLUDED_DOC_DIRS):
                found.append(path)
    return found


def git_lines(root: Path, *args: str) -> list[str] | None:
    """`git ls-files`-style output, or None when git or the repo is missing."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, check=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return [p for p in proc.stdout.decode("utf-8").split("\0") if p]


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


@dataclass(frozen=True)
class Markdown:
    prose: str  # fenced blocks blanked, newlines kept so offsets give line numbers
    fenced: tuple[tuple[int, str], ...]  # (line number, text) of lines inside fences


def parse_markdown(text: str) -> Markdown:
    prose: list[str] = []
    fenced: list[tuple[int, str]] = []
    fence = ""
    for n, line in enumerate(text.split("\n"), start=1):
        m = _FENCE.match(line)
        if not fence and m:
            fence = m.group(1)
            prose.append("")
        elif fence:
            closing = m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence)
            if closing and not line.strip().strip(fence[0]):
                fence = ""
            else:
                fenced.append((n, line))
            prose.append("")
        else:
            prose.append(line)
    return Markdown("\n".join(prose), tuple(fenced))


_INLINE = re.compile(r"(?<!`)(`+)(?!`)((?:(?!\n[ \t]*\n).)+?)(?<!`)\1(?!`)", re.S)


def inline_spans(prose: str) -> Iterator[tuple[int, str]]:
    """(line, content) of every inline code span; a span may wrap a line."""
    for m in _INLINE.finditer(prose):
        yield line_of(prose, m.start()), " ".join(m.group(2).split())


def blank_inline_code(prose: str) -> str:
    return _INLINE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), prose)


def _parse(path: Path) -> ast.Module | None:
    try:
        return _parse_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


@functools.cache
def _parse_text(source: str) -> ast.Module | None:
    """Keyed on the text, not the path, so a file edited mid-test is re-read."""
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _stale(allow: dict[str, str], seen: set[str], what: str) -> list[str]:
    source = Path(__file__).read_text(encoding="utf-8").split("\n")

    def where(key: str) -> int:
        needle = f'    "{key}":'
        return next((n for n, s in enumerate(source, 1) if s.startswith(needle)), 1)

    return [
        f"tests/docs_drift_checks.py:{where(key)}: stale {what} allowlist entry, "
        f"cited by no doc: `{key}`"
        for key in sorted(set(allow) - seen)
    ]


# --- check 1: docs are tracked ------------------------------------------------------


def check_docs_tracked(root: Path) -> tuple[list[str], list[str]] | None:
    """(failures, warnings), or None without git. An untracked doc is a failure
    unless it is a new, not-ignored file under docs/ (a warning until added)."""
    tracked = git_lines(root, "ls-files", "-z")
    others = git_lines(root, "ls-files", "-z", "--others", "--exclude-standard")
    if tracked is None or others is None:
        return None
    tracked_set, new_set = set(tracked), set(others)
    failures: list[str] = []
    warnings: list[str] = []
    for path in doc_files(root):
        r = rel(root, path)
        if r in tracked_set:
            continue
        if r.startswith("docs/") and r in new_set:
            warnings.append(f"{r}:1: new doc not yet tracked (git add it): `{r}`")
        else:
            why = "untracked doc" if r in new_set else "doc is ignored by git"
            failures.append(f"{r}:1: {why}: `{r}`")
    return failures, warnings


# --- check 2: code references ---------------------------------------------------------

_PATH = re.compile(r"^[\w./-]+\.(py|html|toml|sh|template|md|json|yml|yaml)$")
_DOTTED = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")
_SKIP_CHARS = frozenset(" <*{~=$")
_FILE_EXTS = frozenset(
    {"env", "db", "txt", "log", "lock", "plist", "js", "css", "png", "csv", "zip"}
    | {"pdf", "docx", "ini", "cfg", "com", "org", "net", "io"}
)


@dataclass(frozen=True)
class ModuleIndex:
    by_dotted: dict[str, Path]  # "core.closure" -> file; a package -> its __init__.py
    by_leaf: dict[str, tuple[str, ...]]  # "closure" -> ("core.closure",)


def module_index(root: Path) -> ModuleIndex:
    src = root / "src" / "blastjob"
    by_dotted: dict[str, Path] = {}
    leaves: dict[str, list[str]] = {}
    for path in sorted(src.rglob("*.py")) if src.is_dir() else []:
        parts = path.relative_to(src).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        dotted = ".".join(parts)
        by_dotted[dotted] = path
        leaves.setdefault(parts[-1], []).append(dotted)
    return ModuleIndex(by_dotted, {k: tuple(v) for k, v in leaves.items()})


def config_keys(root: Path) -> dict[str, frozenset[str]]:
    """Settings section -> its field and method names, from models/config.py."""
    tree = _parse(root / "src" / "blastjob" / "models" / "config.py")
    if tree is None:
        return {}
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    keys: dict[str, frozenset[str]] = {}
    for cls in classes.values():
        for node in cls.body:
            if not (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)):
                continue
            ann = node.annotation
            if isinstance(ann, ast.Name) and ann.id in classes:
                keys[node.target.id] = frozenset(_class_members(classes[ann.id]))
    return keys


def _class_members(cls: ast.ClassDef) -> set[str]:
    names: set[str] = set()
    for node in cls.body:
        names |= _bound_names(node)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Store):
                if isinstance(sub.value, ast.Name) and sub.value.id == "self":
                    names.add(sub.attr)
    return names


def _bound_names(node: ast.stmt) -> set[str]:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return {node.name}
    if isinstance(node, ast.Assign):
        return {t.id for t in node.targets if isinstance(t, ast.Name)}
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return {node.target.id}
    if isinstance(node, ast.Import):
        return {(a.asname or a.name).split(".")[0] for a in node.names}
    if isinstance(node, ast.ImportFrom):
        return {a.asname or a.name for a in node.names}
    if isinstance(node, ast.If | ast.Try):
        found: set[str] = set()
        for body in (node.body, node.orelse, getattr(node, "finalbody", [])):
            for child in body:
                found |= _bound_names(child)
        for handler in getattr(node, "handlers", []):
            for child in handler.body:
                found |= _bound_names(child)
        return found
    return set()


def name_defined(path: Path, names: list[str]) -> bool:
    """`names[0]` is bound at module level; `names[1]`, if any, is a member of
    that class. Past a non-class binding (an import, an assignment) we accept."""
    tree = _parse(path)
    if tree is None:
        return False
    top: dict[str, ast.stmt] = {}
    for node in tree.body:
        for name in _bound_names(node):
            top[name] = node
    binding = top.get(names[0])
    if binding is None:
        return False
    if len(names) == 1 or not isinstance(binding, ast.ClassDef) or binding.name != names[0]:
        return True
    return names[1] in _class_members(binding)


def _module_options(parts: list[str], idx: ModuleIndex) -> list[tuple[Path, list[str]]]:
    """(module file, remaining names) for every way `parts` can start with a module."""
    starts = [parts[0]] if parts[0] in idx.by_dotted else []
    starts += [m for m in idx.by_leaf.get(parts[0], ()) if m != parts[0]]
    options: list[tuple[Path, list[str]]] = []
    for start in starts:
        mod, rest = start, parts[1:]
        while rest and f"{mod}.{rest[0]}" in idx.by_dotted:
            mod, rest = f"{mod}.{rest[0]}", rest[1:]
        options.append((idx.by_dotted[mod], rest))
    return options


def dotted_problem(name: str, idx: ModuleIndex, keys: dict[str, frozenset[str]]) -> str | None:
    """Why a dotted span is a dead reference, or None (valid, or not a reference).

    A real settings key (`discovery.towns`, `web.host`) is not code. A span
    whose first segment is a settings section but which is neither a real key
    nor a real code name is a stale setting name and fails."""
    name = name.removeprefix("blastjob.")
    parts = name.split(".")
    if parts[-1] in _FILE_EXTS:
        return None
    if len(parts) >= 2 and parts[1] in keys.get(parts[0], frozenset()):
        return None
    options = _module_options(parts, idx)
    if any(not rest or name_defined(path, rest) for path, rest in options):
        return None
    if parts[0] in keys:
        return "neither a settings key nor a blastjob name"
    return "name not defined in that blastjob module" if options else None


def _basenames(root: Path) -> frozenset[str]:
    names: set[str] = set()
    for path in root.rglob("*"):
        parts = path.relative_to(root).parts
        if path.is_file() and not PRIVATE_TREES & set(parts):
            names.add(path.name)
    return frozenset(names)


def path_ok(ref: str, root: Path, doc: Path, basenames: frozenset[str]) -> bool:
    ref = ref.removeprefix("./")
    bases = (root / "src" / "blastjob", root, root / "tests", doc.parent)
    for base in bases:
        target = (base / ref).resolve()
        if not target.exists() or not target.is_relative_to(root.resolve()):
            continue
        if not PRIVATE_TREES & set(target.relative_to(root.resolve()).parts):
            return True
    return "/" not in ref and ref in basenames


def _span_problem(
    span: str,
    root: Path,
    doc: Path,
    idx: ModuleIndex,
    keys: dict[str, frozenset[str]],
    basenames: frozenset[str],
) -> str | None:
    if "://" in span or span.startswith(("www.", "mailto:", "-")):
        return None
    span = re.sub(r"\([^ ]*\)$", "", span)
    if not span or _SKIP_CHARS & set(span):
        return None
    if "::" in span:
        file_part, _, member = span.partition("::")
        if not _PATH.match(file_part):
            return None
        if not path_ok(file_part, root, doc, basenames):
            return "file not found"
        target = next(
            (
                b / file_part
                for b in (root, root / "src" / "blastjob", root / "tests")
                if (b / file_part).is_file()
            ),
            None,
        )
        ok = target is None or name_defined(target, member.split("::"))
        return None if ok else "name not defined in that file"
    if _PATH.match(span):
        if "/" not in span.removeprefix("./"):
            return None  # bare filename: resolves, or names a runtime file
        return None if path_ok(span, root, doc, basenames) else "file not found"
    if _DOTTED.match(span):
        return dotted_problem(span, idx, keys)
    return None


def check_code_refs(root: Path, allowlist: dict[str, str] = CODE_REF_ALLOWLIST) -> list[str]:
    idx, keys, basenames = module_index(root), config_keys(root), _basenames(root)
    problems: list[str] = []
    seen: set[str] = set()
    for doc in doc_files(root):
        md = parse_markdown(doc.read_text(encoding="utf-8"))
        for line, span in inline_spans(md.prose):
            if span in allowlist:
                seen.add(span)
                continue
            why = _span_problem(span, root, doc, idx, keys, basenames)
            if why:
                problems.append(f"{rel(root, doc)}:{line}: {why}: `{span}`")
    return problems + _stale(allowlist, seen, "code-reference")


# --- check 3: command flags -------------------------------------------------------------

_PROG = re.compile(r"(?:^|(?<=[\s\"'(]))(blastjob(?:-daily|-web)?)(?=\s|$)")
_STOP = re.compile(r"(?:^|\s)#|\||&&|;|>")
_FLAG = re.compile(r"(?<![\w-])--([a-z][a-z0-9-]*)(\[-[a-z0-9-]+\])?")


def entry_points(root: Path) -> dict[str, tuple[str, str]]:
    """console script -> (module dotted name without `blastjob.`, function)."""
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data.get("project", {}).get("scripts", {})
    out: dict[str, tuple[str, str]] = {}
    for name, target in scripts.items():
        module, _, func = str(target).partition(":")
        out[name] = (module.removeprefix("blastjob."), func)
    return out


def _blastjob_imports(tree: ast.Module, idx: ModuleIndex) -> dict[str, tuple[str, str | None]]:
    """local name -> (module, function or None) for names imported from blastjob."""
    out: dict[str, tuple[str, str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("blastjob"):
            base = (node.module or "").removeprefix("blastjob").lstrip(".")
            for a in node.names:
                full = f"{base}.{a.name}".lstrip(".")
                if full in idx.by_dotted:
                    out[a.asname or a.name] = (full, None)
                elif base in idx.by_dotted:
                    out[a.asname or a.name] = (base, a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                mod = a.name.removeprefix("blastjob.")
                if a.name.startswith("blastjob.") and a.asname and mod in idx.by_dotted:
                    out[a.asname] = (mod, None)
    return out


def _is_flag_call(call: ast.Call) -> bool:
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name in {"add_argument", "option"}


def _parsers(scope: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.Call) and _is_flag_call(node):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                names.add(func.value.id)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            attr = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if attr in {"ArgumentParser", "add_parser", "add_subparsers"}:
                names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        if isinstance(node, ast.FunctionDef):
            names |= {a.arg for a in node.args.args if "parser" in a.arg or a.arg == "ap"}
    return names


def defined_flags(root: Path, module: str, func: str | None = None, depth: int = 0) -> set[str]:
    """Every `--flag` an entry point's argparse/click setup defines, following
    the parser into helpers called with it (`daily_tools.add_arguments(ap)`)."""
    idx = module_index(root)
    path = idx.by_dotted.get(module)
    tree = _parse(path) if path else None
    if tree is None:
        return set()
    scope: ast.AST = tree
    if func is not None:
        found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == func]
        if not found:
            return set()
        scope = found[0]
    flags = {"--help"}
    imports, parsers = _blastjob_imports(tree, idx), _parsers(scope)
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        if _is_flag_call(node):
            flags |= {
                a.value
                for a in node.args
                if isinstance(a, ast.Constant)
                and isinstance(a.value, str)
                and a.value.startswith("--")
            }
            continue
        if depth > 3 or not any(isinstance(a, ast.Name) and a.id in parsers for a in node.args):
            continue
        target = _call_target(node.func, imports)
        if target:
            flags |= defined_flags(root, target[0], target[1], depth + 1)
    return flags


def _call_target(
    func: ast.expr, imports: dict[str, tuple[str, str | None]]
) -> tuple[str, str] | None:
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        hit = imports.get(func.value.id)
        if hit and hit[1] is None:
            return hit[0], func.attr
    if isinstance(func, ast.Name):
        hit = imports.get(func.id)
        if hit and hit[1] is not None:
            return hit[0], hit[1]
    return None


def invocations(text: str) -> Iterator[tuple[str, list[str]]]:
    """(program, flags) for each blastjob CLI invocation in one command line."""
    for m in _PROG.finditer(text):
        rest = text[m.end() :]
        nxt = _PROG.search(rest)
        rest = rest[: nxt.start()] if nxt else rest
        stop = _STOP.search(rest)
        rest = rest[: stop.start()] if stop else rest
        flags: list[str] = []
        for f in _FLAG.finditer(rest):
            flags.append(f"--{f.group(1)}")
            if f.group(2):
                flags.append(f"--{f.group(1)}{f.group(2)[1:-1]}")
        yield m.group(1), flags


def _command_lines(md: Markdown) -> Iterator[tuple[int, str]]:
    yield from inline_spans(md.prose)
    pending, start = "", 0
    for n, line in md.fenced:
        if not pending:
            start = n
        if line.rstrip().endswith("\\"):
            pending += line.rstrip()[:-1] + " "
            continue
        yield start, pending + line
        pending = ""
    if pending:
        yield start, pending


def check_flags(root: Path) -> list[str]:
    known = {name: defined_flags(root, mod) for name, (mod, _) in entry_points(root).items()}
    problems: list[str] = []
    for doc in doc_files(root):
        md = parse_markdown(doc.read_text(encoding="utf-8"))
        for line, text in _command_lines(md):
            for prog, flags in invocations(text):
                for flag in flags:
                    if prog in known and flag not in known[prog]:
                        problems.append(
                            f"{rel(root, doc)}:{line}: {prog} defines no such flag: `{flag}`"
                        )
    return problems


# --- check 4: UI labels ----------------------------------------------------------------

_BOLD = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*", re.S)
_TAGS = re.compile(r"<[^>]*>|\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", re.S)


def _collapse(text: str) -> str:
    return " ".join(text.split())


_ATTR_TEXT = re.compile(r"\b(?:placeholder|title|value|aria-label|label)=\"([^\"]*)\"")
_EDGE = " :.,;!?…·|→←"


def _label_key(text: str) -> str:
    return _collapse(html.unescape(text)).strip(_EDGE)


_JINJA = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.S)
_QUOTED = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"")
# Splits "Fatal — 3", "More ▾", "Send anyway — say why:" into their label part.
_LABEL_SEP = re.compile(r"\s*(?:—|–|:|\(|\)|·|▾|▸|\|)\s*")


def _label_forms(text: str) -> set[str]:
    whole = _label_key(text)
    return {whole} | {_label_key(seg) for seg in _LABEL_SEP.split(whole)}


def _python_literals(web: Path) -> set[str]:
    """String constants in web/*.py (labels built in Python, e.g. the stage nav),
    whole literals only, docstrings excluded."""
    found: set[str] = set()
    for path in sorted(web.rglob("*.py")) if web.is_dir() else []:
        tree = _parse(path)
        if tree is None:
            continue
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in docstrings:
                    found.add(_label_key(node.value))
    return found


def template_labels(root: Path) -> frozenset[str]:
    """Every text node, user-visible attribute value and Jinja string literal
    in the templates (each also split at a separator such as " — "), plus
    whole string literals in web/*.py. A label must EQUAL one of them, so a
    fragment ("Mark" of "Mark applied") does not pass."""
    web = root / "src" / "blastjob" / "web"
    tdir = web / "templates"
    labels: set[str] = set()
    for path in sorted(tdir.rglob("*")) if tdir.is_dir() else []:
        if not path.is_file():
            continue
        raw = path.read_text(encoding="utf-8")
        texts = [*_TAGS.split(raw), *_ATTR_TEXT.findall(raw)]
        for block in _JINJA.findall(raw):
            texts += [a or b for a, b in _QUOTED.findall(block)]
        for text in texts:
            labels |= _label_forms(text)
    labels |= _python_literals(web)
    return frozenset(labels - {""})


def check_ui_labels(root: Path, allowlist: dict[str, str] = LABEL_ALLOWLIST) -> list[str]:
    known = template_labels(root)
    problems: list[str] = []
    seen: set[str] = set()
    using = root / "docs" / "using"
    for doc in sorted(using.glob("*.md")) if using.is_dir() else []:
        prose = parse_markdown(doc.read_text(encoding="utf-8")).prose
        for m in _BOLD.finditer(prose):
            label = _collapse(m.group(1).replace("`", ""))
            line_start = prose.rfind("\n", 0, m.start()) + 1
            if label.endswith(":") and not prose[line_start : m.start()].strip():
                continue  # a lead-in such as "**What this covers:**", not a label
            if label in allowlist:
                seen.add(label)
            elif _label_key(label) not in known:
                problems.append(
                    f"{rel(root, doc)}:{line_of(prose, m.start())}: "
                    f"bold label in no template: `{label}`"
                )
    return problems + _stale(allowlist, seen, "UI-label")


# --- check 5: links ---------------------------------------------------------------------

_LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_REFDEF = re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)", re.M)
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.*?)\s*#*\s*$", re.M)
_HTML_ANCHOR = re.compile(r"<a\s[^>]*(?:name|id)=\"([^\"]+)\"")


def slugify(heading: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>|[`*]", "", text).strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    prose = parse_markdown(path.read_text(encoding="utf-8")).prose
    seen: Counter[str] = Counter()
    out: set[str] = set(_HTML_ANCHOR.findall(prose))
    for m in _HEADING.finditer(prose):
        slug = slugify(m.group(1))
        out.add(slug if not seen[slug] else f"{slug}-{seen[slug]}")
        seen[slug] += 1
    return out


def _link_problem(target: str, root: Path, doc: Path) -> str | None:
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("//"):
        return None
    path_part, _, anchor = unquote(target).partition("#")
    dest = (doc.parent / path_part).resolve() if path_part else doc.resolve()
    real_root = root.resolve()
    if not dest.exists() or not dest.is_relative_to(real_root):
        return "link target not found"
    if PRIVATE_TREES & set(dest.relative_to(real_root).parts):
        return "link into a private or generated folder"
    if anchor and dest.suffix == ".md" and anchor.lower() not in anchors(dest):
        return "no heading with that anchor"
    return None


def check_links(root: Path) -> list[str]:
    problems: list[str] = []
    for doc in doc_files(root):
        prose = blank_inline_code(parse_markdown(doc.read_text(encoding="utf-8")).prose)
        for m in [*_LINK.finditer(prose), *_REFDEF.finditer(prose)]:
            why = _link_problem(m.group(1), root, doc)
            if why:
                problems.append(
                    f"{rel(root, doc)}:{line_of(prose, m.start())}: {why}: `{m.group(1)}`"
                )
    return problems


# --- check 6: the agent entry point ---------------------------------------------------


def check_entry_point(root: Path) -> list[str]:
    problems: list[str] = []
    claude = root / "CLAUDE.md"
    if not claude.is_file():
        problems.append("CLAUDE.md:1: missing: `CLAUDE.md`")
    else:
        lines = [s.strip() for s in claude.read_text(encoding="utf-8").splitlines()]
        first = next((s for s in lines if s), "")
        if first != "@AGENTS.md":
            problems.append(f"CLAUDE.md:1: first line must be `@AGENTS.md`, is: `{first}`")
    agents = root / "AGENTS.md"
    if not agents.is_file():
        problems.append("AGENTS.md:1: missing: `AGENTS.md`")
    else:
        data = agents.read_bytes()
        n_lines = data.decode("utf-8").count("\n") + (0 if data.endswith(b"\n") else 1)
        if n_lines > AGENTS_MAX_LINES:
            problems.append(f"AGENTS.md:1: {n_lines} lines, max {AGENTS_MAX_LINES}: `AGENTS.md`")
        if len(data) >= AGENTS_MAX_BYTES:
            problems.append(f"AGENTS.md:1: {len(data)} bytes, must be < 12 KiB: `AGENTS.md`")
    if not (root / "SETUP.md").is_file():
        problems.append("SETUP.md:1: missing (the cockpit's base.html names it): `SETUP.md`")
    return problems


# --- check 7: reverse references from code to docs ------------------------------------

_DOC_NAME = r"(?:docs/[\w./-]+\.md|(?:AGENTS|CLAUDE|SETUP|ARCHITECTURE)\.md)"
_REVERSE = re.compile(rf"(?<![\w./~-]){_DOC_NAME}")
_HEADING_CITE = re.compile(
    rf"(?<![\w./~-])({_DOC_NAME})(?:['’]s)?[\s,(#]*?[\"“]([A-Z][^\"”]{{2,120}}?)"
    r"[\"”]"
)


def _non_doc_files(root: Path) -> Iterator[tuple[str, str]]:
    listed = git_lines(root, "ls-files", "-z")
    if listed is None:  # no git (an extracted archive): walk the tree instead
        listed = [
            rel(root, p)
            for p in root.rglob("*")
            if p.is_file() and not PRIVATE_TREES & set(p.relative_to(root).parts)
        ]
    docs = {rel(root, d) for d in doc_files(root)}
    for r in sorted(listed):
        if r in docs or r in DRIFT_FILES or r.startswith(REVERSE_EXCLUDED):
            continue
        path = root / r
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        try:
            yield r, data.decode("utf-8")
        except UnicodeDecodeError:
            continue


def check_reverse_refs(root: Path) -> list[str]:
    problems: list[str] = []
    for r, text in _non_doc_files(root):
        for m in _REVERSE.finditer(text):
            if not (root / m.group(0)).is_file():
                problems.append(
                    f"{r}:{line_of(text, m.start())}: cites a missing doc: `{m.group(0)}`"
                )
        for m in _HEADING_CITE.finditer(text):
            problems.append(
                f"{r}:{line_of(text, m.start())}: cites a doc by heading (cite the file "
                f'only): `{m.group(1)} "{" ".join(m.group(2).split())}"`'
            )
    return problems

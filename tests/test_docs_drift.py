"""Docs drift guard (issue #61): the docs must not cite code, flags, labels,
links or files that do not exist, and code must not cite docs that do not.

The checks live in `tests/docs_drift_checks.py` and take a repo root. The
first block runs them on this checkout; the second (mutation tests) builds a
tiny fake repo per check and proves the check FAILS on an injected defect and
PASSES once it is fixed, so a check that silently matches nothing is caught.

Scope, stated honestly: these are reference checks. Whether a doc's
*behaviour* claims are true is left to review against the code.
"""

from __future__ import annotations

import shutil
import subprocess
import warnings
from pathlib import Path

import pytest

from tests import docs_drift_checks as dd

REPO = Path(__file__).resolve().parents[1]


def _report(problems: list[str]) -> str:
    return f"{len(problems)} problem(s):\n" + "\n".join(problems)


# --- the real checks, on this checkout ------------------------------------------------


def test_docs_are_tracked_by_git():
    result = dd.check_docs_tracked(REPO)
    if result is None:
        pytest.skip("git or the repo is not available")
    failures, warns = result
    for w in warns:
        warnings.warn(w, stacklevel=1)
    assert failures == [], _report(failures)


def test_docs_cite_only_code_that_exists():
    problems = dd.check_code_refs(REPO)
    assert problems == [], _report(problems)


def test_docs_show_only_cli_flags_that_exist():
    problems = dd.check_flags(REPO)
    assert problems == [], _report(problems)


def test_user_guide_bold_labels_appear_in_templates():
    problems = dd.check_ui_labels(REPO)
    assert problems == [], _report(problems)


def test_doc_links_and_anchors_resolve():
    problems = dd.check_links(REPO)
    assert problems == [], _report(problems)


def test_agent_entry_point_is_agents_md():
    problems = dd.check_entry_point(REPO)
    assert problems == [], _report(problems)


def test_code_cites_only_docs_that_exist_and_never_by_heading():
    problems = dd.check_reverse_refs(REPO)
    assert problems == [], _report(problems)


# --- mutation tests: a fake repo per check -------------------------------------------

PYPROJECT = '[project]\nname = "fake"\n[project.scripts]\nblastjob-daily = "blastjob.cli:main"\n'
CONFIG = """
from pydantic import BaseModel

class WebConfig(BaseModel):
    host: str = "127.0.0.1"

class CandidateConfig(BaseModel):
    target_titles: list[str] = []

class BlastJobConfig(BaseModel):
    web: WebConfig = WebConfig()
    candidate: CandidateConfig = CandidateConfig()
"""
CLOSURE = """
from blastjob.core.helpers import reexported

def close_transition():
    pass

class Gate:
    threshold = 78

    def decide(self):
        self.verdict = 1
"""
CLI = """
import argparse
from blastjob import tools

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    tools.add_arguments(ap)
"""
TOOLS = """
def add_arguments(ap):
    ap.add_argument("--recluster-report", action="store_true")
    ap.add_argument("--recluster", action="store_true")
"""
TEMPLATE = "<button>Mark applied</button>\n<a>Keep &amp;\n  tailor</a> {{ n }}\n"
HOW = "# How it works\n\n## Same role, different route\n\nSee `core/closure.py`.\n"


def _write(root: Path, relpath: str, text: str) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def fake(tmp_path: Path) -> Path:
    files = {
        "pyproject.toml": PYPROJECT,
        "src/blastjob/__init__.py": "",
        "src/blastjob/core/__init__.py": "",
        "src/blastjob/core/helpers.py": "def reexported():\n    pass\n",
        "src/blastjob/core/closure.py": CLOSURE,
        "src/blastjob/models/__init__.py": "",
        "src/blastjob/models/config.py": CONFIG,
        "src/blastjob/web/__init__.py": "",
        "src/blastjob/web/templates/base.html": TEMPLATE,
        "src/blastjob/cli.py": CLI,
        "src/blastjob/tools.py": TOOLS,
        "tests/conftest.py": "x = 1\n",
        "CLAUDE.md": "@AGENTS.md\n",
        "AGENTS.md": "# Agents\n\nRead [the overview](docs/how-it-works/overview.md).\n",
        "SETUP.md": "# Setup\n",
        "docs/how-it-works/overview.md": HOW,
        "docs/using/stages.md": "**What this covers:** stages.\n\nClick **Mark applied**.\n",
    }
    for relpath, text in files.items():
        _write(tmp_path, relpath, text)
    return tmp_path


def _assert_mutation(check, root: Path, doc: str, bad: str, good: str, needle: str) -> None:
    """Clean passes, the injected defect fails naming `needle`, the fix passes."""
    assert check(root) == [], "baseline must be clean"
    _write(root, doc, bad)
    problems = check(root)
    assert problems and any(needle in p for p in problems), problems
    assert all(p.startswith(f"{doc}:") for p in problems), problems
    _write(root, doc, good)
    assert check(root) == [], "the fixed version must pass"


def _code_refs(root: Path) -> list[str]:
    return dd.check_code_refs(root, allowlist={})


@pytest.mark.parametrize(
    ("bad", "good"),
    [
        ("`core/closurex.py`", "`core/closure.py`"),
        ("`core.closure.reopen`", "`core.closure.close_transition`"),
        ("`closure.reopen()`", "`closure.close_transition()`"),
        ("`blastjob.core.closure.Gate.missing`", "`blastjob.core.closure.Gate.decide`"),
        ("`closure.Gate.nope`", "`closure.Gate.verdict`"),
        ("`tests/conftest.py::y`", "`tests/conftest.py::x`"),
        ("`docs/how-it-works/gone.md`", "`docs/how-it-works/overview.md`"),
        ("`web.nothing_here`", "`web.host` and `closure.reexported`"),
        ("`candidate.target_titlez`", "`candidate.target_titles`"),
        ("`core/nope/x.py`", "`web/templates/base.html`"),
    ],
)
def test_code_ref_check_catches_a_dead_reference(fake: Path, bad: str, good: str):
    doc = "docs/how-it-works/x.md"
    _assert_mutation(_code_refs, fake, doc, f"See {bad}.\n", f"See {good}.\n", bad.strip("`"))


def test_code_ref_check_ignores_non_references(fake: Path):
    _write(
        fake,
        "docs/x.md",
        "`e.g.` `example.com` `daily.log` `--x` `a b.py` `~/x.md` `work_history.md`\n"
        "`blastjob.db` `RELEASED.txt` `discovery.*` `web.host` `candidate.target_titles`\n"
        "```\n`core/nope.py`\n```\n`{role}.md` `https://x/y.py` `secrets.env`\n",
    )
    assert _code_refs(fake) == []


def test_code_ref_allowlist_suppresses_and_goes_stale(fake: Path):
    _write(fake, "docs/x.md", "Edit `templates/standard.md`.\n")
    allow = {"templates/standard.md": "data-dir file"}
    assert dd.check_code_refs(fake, allowlist=allow) == []
    _write(fake, "docs/x.md", "nothing\n")
    problems = dd.check_code_refs(fake, allowlist=allow)
    assert len(problems) == 1 and "stale" in problems[0] and "templates/standard.md" in problems[0]
    assert not problems[0].startswith("tests/docs_drift_checks.py:1:")  # the entry's own line


@pytest.mark.parametrize(
    ("bad", "good", "needle"),
    [
        (
            "```sh\nuv run blastjob-daily --dry-runn\n```\n",
            "```sh\nuv run blastjob-daily --dry-run   # --not-a-flag in a comment\n```\n",
            "--dry-runn",
        ),
        (
            "Run `blastjob-daily --recluster-reprt`.\n",
            "Run `blastjob-daily --recluster-report` (helper-module flag).\n",
            "--recluster-reprt",
        ),
        (
            "Run `blastjob-daily --reclustr[-report]`.\n",
            "Run `blastjob-daily --recluster[-report]`.\n",
            "--reclustr",
        ),
        (
            "```\nblastjob-daily \\\n  --bogus\n```\n",
            "```\nblastjob-daily \\\n  --help\n```\n",
            "--bogus",
        ),
    ],
)
def test_flag_check_catches_an_undefined_flag(fake: Path, bad: str, good: str, needle: str):
    _assert_mutation(dd.check_flags, fake, "README.md", bad, good, needle)


def test_flag_check_ignores_other_programs(fake: Path):
    _write(
        fake,
        "README.md",
        "`uv run --extra dev pytest` `python -m blastjob.daily --zz`\n`ls ~/.blastjob --zz`\n",
    )
    assert dd.check_flags(fake) == []


def test_ui_label_check_catches_a_missing_label(fake: Path):
    doc = "docs/using/stages.md"
    _assert_mutation(
        dd.check_ui_labels,
        fake,
        doc,
        "**What this covers:** x.\n\nClick **Mark as applied**.\n",
        "**What this covers:** x.\n\nClick **Mark applied** or **Keep & tailor**.\n",
        "Mark as applied",
    )


def test_ui_label_check_reads_separators_jinja_and_python_labels(fake: Path):
    _write(
        fake,
        "src/blastjob/web/templates/r.html",
        "<h4>Fatal &mdash; {{ n }}</h4>{% set s = [('a', 'Not tailored')] %}"
        '<input placeholder="Search jobs">\n',
    )
    _write(fake, "src/blastjob/web/stages.py", '"""Ghost label."""\nNAV = ["Queue"]\n')
    _write(
        fake, "docs/using/stages.md", "**Fatal**, **Not tailored**, **Search jobs**, **Queue**.\n"
    )
    assert dd.check_ui_labels(fake) == []
    _write(fake, "docs/using/stages.md", "**Ghost label**\n")
    assert len(dd.check_ui_labels(fake)) == 1


def test_ui_label_check_rejects_a_fragment_of_a_real_label(fake: Path):
    _write(fake, "docs/using/stages.md", "Click **Mark**.\n")
    problems = dd.check_ui_labels(fake)
    assert len(problems) == 1 and "`Mark`" in problems[0]


def test_ui_label_allowlist_goes_stale(fake: Path):
    problems = dd.check_ui_labels(fake, allowlist={"Retired label": "built in Python"})
    assert len(problems) == 1 and "stale" in problems[0]


@pytest.mark.parametrize(
    ("bad", "good", "needle"),
    [
        ("[x](../how-it-works/missing.md)\n", "[x](../how-it-works/overview.md)\n", "missing.md"),
        (
            "[x](../how-it-works/overview.md#same-role)\n",
            "[x](../how-it-works/overview.md#same-role-different-route)\n",
            "#same-role",
        ),
        ("# Top\n\n[x](#bottom)\n", "# Top\n\n[x](#top)\n", "#bottom"),
        ("[x]: ../../src/nope.py\n", "[x]: ../../src/blastjob/cli.py\n", "nope.py"),
    ],
)
def test_link_check_catches_a_dead_link(fake: Path, bad: str, good: str, needle: str):
    _assert_mutation(dd.check_links, fake, "docs/using/links.md", bad, good, needle)


def test_link_check_ignores_external_and_code(fake: Path):
    _write(
        fake, "docs/x.md", "[a](https://example.com/x.md) `[b](nope.md)`\n```\n[c](nope.md)\n```\n"
    )
    assert dd.check_links(fake) == []


def test_slugify_matches_github():
    assert dd.slugify("Same role, different route") == "same-role-different-route"
    assert dd.slugify("`web/` & the **cockpit**") == "web--the-cockpit"


def test_entry_point_check_catches_each_defect(fake: Path):
    assert dd.check_entry_point(fake) == []
    _write(fake, "CLAUDE.md", "# CLAUDE.md\n\n@AGENTS.md\n")
    assert any("@AGENTS.md" in p for p in dd.check_entry_point(fake))
    _write(fake, "CLAUDE.md", "\n@AGENTS.md\n")
    _write(fake, "AGENTS.md", "x\n" * (dd.AGENTS_MAX_LINES + 1))
    assert any("lines" in p for p in dd.check_entry_point(fake))
    _write(fake, "AGENTS.md", "x" * dd.AGENTS_MAX_BYTES)
    assert any("12 KiB" in p for p in dd.check_entry_point(fake))
    _write(fake, "AGENTS.md", "short\n")
    (fake / "SETUP.md").unlink()
    assert any("SETUP.md" in p for p in dd.check_entry_point(fake))
    _write(fake, "SETUP.md", "# Setup\n")
    assert dd.check_entry_point(fake) == []


@pytest.mark.parametrize(
    ("bad", "good", "needle"),
    [
        (
            '"""See docs/ARCHITECTURE.md for why."""\n',
            '"""See SETUP.md for why."""\n',
            "docs/ARCHITECTURE.md",
        ),
        (
            '"""See CLAUDE.md\'s "Truth guarantees"."""\n',
            '"""See CLAUDE.md."""\n',
            "Truth guarantees",
        ),
        (
            '# see docs/how-it-works/overview.md\n# ("Same role, different route")\n',
            "# see docs/how-it-works/overview.md\n",
            "Same role",
        ),
        (
            '"""See docs/how-it-works/overview.md ("Same role,\n    different route")."""\n',
            '"""See docs/how-it-works/overview.md."""\n',
            "Same role, different route",
        ),
    ],
)
def test_reverse_check_catches_dead_and_heading_citations(
    fake: Path, bad: str, good: str, needle: str
):
    _assert_mutation(dd.check_reverse_refs, fake, "src/blastjob/core/gate.py", bad, good, needle)


def test_reverse_check_skips_outside_paths_and_its_own_files(fake: Path):
    _write(fake, "src/blastjob/a.py", '# ~/Source/CLAUDE.md and .claude/CLAUDE.md, "SETUP.md"\n')
    _write(fake, "tests/test_docs_drift.py", "# docs/gone.md\n")
    _write(fake, "local/notes.txt", "docs/gone.md\n")
    assert dd.check_reverse_refs(fake) == []


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_tracked_check_fails_untracked_and_ignored_docs(fake: Path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    _git(fake, "init", "-q")
    _git(fake, "add", ".")
    assert dd.check_docs_tracked(fake) == ([], [])
    _write(fake, "docs/new.md", "# New\n")
    _write(fake, "CONTRIBUTING.md", "# Contributing\n")
    _write(fake, ".gitignore", "docs/hidden.md\n")
    _write(fake, "docs/hidden.md", "# Hidden\n")
    result = dd.check_docs_tracked(fake)
    assert result is not None
    failures, warns = result
    assert sorted(f.split(":")[0] for f in failures) == ["CONTRIBUTING.md", "docs/hidden.md"]
    assert [w.split(":")[0] for w in warns] == ["docs/new.md"]
    _git(fake, "add", "-f", ".")
    assert dd.check_docs_tracked(fake) == ([], [])


def test_tracked_check_skips_without_a_repo(tmp_path: Path):
    assert dd.check_docs_tracked(tmp_path / "nowhere") is None

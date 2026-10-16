#!/bin/sh
# The local gate: every check, one command. Run it before landing a change.
#   sh scripts/verify.sh
# mypy runs on the Python files changed against origin/main (CONTRIBUTING:
# "zero new mypy errors in the modules you touched"); the whole tree still
# carries older errors, so a full-tree mypy would fail on day one.
set -eu

echo "== ruff check"
uv run --extra dev ruff check src tests
echo "== ruff format --check"
uv run --extra dev ruff format --check src tests

echo "== mypy (changed files)"
changed=$(git diff --name-only --diff-filter=d origin/main -- 'src/*.py' || true)
if [ -n "$changed" ]; then
  # shellcheck disable=SC2086  # word-splitting the file list is intended
  uv run --extra dev mypy $changed
else
  echo "no changed source files"
fi

echo "== pytest"
uv run --extra dev pytest -q

echo "== personal-data check"
uv run python scripts/check_no_personal_data.py

echo "gate: all checks passed"

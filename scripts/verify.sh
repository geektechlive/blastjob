#!/bin/sh
# The local gate: every check, one command. Run it before landing a change.
#   sh scripts/verify.sh
set -eu

echo "== ruff check"
uv run --extra dev ruff check src tests
echo "== ruff format --check"
uv run --extra dev ruff format --check src tests

echo "== mypy"
uv run --extra dev mypy

echo "== pytest"
uv run --extra dev pytest -q

echo "== personal-data check"
uv run python scripts/check_no_personal_data.py

echo "gate: all checks passed"

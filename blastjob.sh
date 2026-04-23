#!/usr/bin/env bash
# Launch blastjob — activates the venv and loads .env automatically.
# Usage: ./blastjob.sh
# Or symlink it somewhere on your PATH: ln -s "$PWD/blastjob.sh" ~/.local/bin/blastjob

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Activate venv if not already inside one
if [ -z "${VIRTUAL_ENV:-}" ]; then
    source "$SCRIPT_DIR/.venv/bin/activate"
fi

# Load .env if present (sets ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.)
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -o allexport
    source "$SCRIPT_DIR/.env"
    set +o allexport
fi

exec python -m blastjob "$@"

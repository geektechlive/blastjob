#!/bin/zsh
# launchd entry point for blastjob-daily. Explicit PATH (launchd has no login
# shell), project venv, breadcrumbs to $BLASTJOB_DATA_DIR/daily.log
# (default ~/.blastjob). The claude CLI
# must be logged in for this user (LLM scoring runs through it).
set -u
: "${HOME:?HOME must be set}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${BLASTJOB_DATA_DIR:-$HOME/.blastjob}"
case "$DATA_DIR" in "~"|"~/"*) DATA_DIR="$HOME${DATA_DIR#\~}" ;; esac
LOG="$DATA_DIR/daily.log"
(umask 077 && mkdir -p "$DATA_DIR")
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') start"
  cd "$REPO" && "$REPO/.venv/bin/blastjob-daily" "$@"
  rc=$?
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') finish rc=$rc"
} >> "$LOG" 2>&1

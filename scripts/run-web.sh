#!/bin/zsh
# launchd entry point for blastjob-web. Explicit PATH (launchd has no login
# shell), project venv. No breadcrumb wrapper here (unlike run-daily.sh):
# launchd's KeepAlive needs the server to stay the foreground process, so we
# exec straight into it; launchd captures stdout/stderr via the plist paths.
set -u
: "${HOME:?HOME must be set}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
exec "$REPO/.venv/bin/blastjob-web" --no-browser "$@"

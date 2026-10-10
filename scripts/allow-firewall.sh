#!/bin/sh
# Allow the cockpit's Python through the macOS application firewall.
#
# Needs sudo:   sudo sh scripts/allow-firewall.sh
#
# The firewall allows /usr/bin/python3 but the cockpit runs from this project's
# virtualenv, which resolves to a uv-managed binary with a very long path. That
# path wraps when pasted into a terminal, which silently turns the command into
# `socketfilterfw --add` with no argument followed by an attempt to RUN python.
# So the path is computed here rather than typed.
set -eu

FW=/usr/libexec/ApplicationFirewall/socketfilterfw
REPO=$(cd "$(dirname "$0")/.." && pwd)
VENV_PY="$REPO/.venv/bin/python3"

if [ ! -e "$VENV_PY" ]; then
  echo "No venv python at $VENV_PY" >&2
  exit 1
fi

# The firewall wants the real file, not the symlink.
BIN=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$VENV_PY")
echo "Binary: $BIN"

if [ "$(id -u)" -ne 0 ]; then
  echo >&2
  echo "Re-run with sudo:  sudo sh $0" >&2
  exit 1
fi

"$FW" --add "$BIN"
"$FW" --unblockapp "$BIN"

echo
echo "Firewall state for it now:"
"$FW" --getappblocked "$BIN"

echo
echo "Next:  sh $REPO/scripts/verify-lan-access.sh"

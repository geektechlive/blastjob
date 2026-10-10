#!/bin/sh
# Render the launchd templates for THIS checkout and user, then load them.
#
#   sh scripts/install-launchd.sh                   # web cockpit + 07:30 daily run
#   sh scripts/install-launchd.sh web               # only the cockpit service
#   sh scripts/install-launchd.sh daily             # only the daily run
#   sh scripts/install-launchd.sh --replace [...]   # also unload + remove OTHER
#                                                   # blastjob agents (other labels)
#
# LABEL_PREFIX defaults to "local.$USER"; override it with the env var.
# Another blastjob agent with a different label (an older install, another
# prefix) would run a second cockpit / a second daily run against the same data,
# so the installer refuses until you unload it or pass --replace. It never
# unloads anything silently.
# BLASTJOB_DATA_DIR, when set, is passed to the services (EnvironmentVariables)
# and their logs go there; unset, both default to ~/.blastjob as before.
# RENDER_ONLY=1 prints the rendered plists and touches nothing (no files, no
# launchctl, no conflict check).
# The cockpit binds whatever the web.host setting says (default 127.0.0.1). To
# reach it from another device on your LAN: `blastjob setup set web.host 0.0.0.0`,
# then restart the service -- and read the warning in src/blastjob/web/launch.py
# first: the cockpit has no login.
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
PREFIX="${LABEL_PREFIX:-local.${USER:-$(id -un)}}"
AGENTS="$HOME/Library/LaunchAgents"
RENDER_ONLY="${RENDER_ONLY:-}"

REPLACE=""
WHICH=""
for arg in "$@"; do
  case "$arg" in
    --replace) REPLACE=1 ;;
    web|daily) WHICH="$WHICH $arg" ;;
    *) echo "unknown argument: $arg (expected web, daily or --replace)" >&2; exit 1 ;;
  esac
done
[ -n "$WHICH" ] || WHICH="web daily"

DATA_DIR="${BLASTJOB_DATA_DIR:-}"
case "$DATA_DIR" in
  "~") DATA_DIR="$HOME" ;;
  "~/"*) DATA_DIR="$HOME/${DATA_DIR#\~/}" ;;
esac
if [ -n "$DATA_DIR" ]; then
  KEEP_ENV='/<!--\/*ENV-->/d'                # keep the block, drop the markers
  LOG_DIR="$DATA_DIR"
else
  KEEP_ENV='/<!--ENV-->/,/<!--\/ENV-->/d'    # no env var: drop the whole block
  LOG_DIR="$HOME/.blastjob"
fi

# A value safe to use as a sed replacement with | as the delimiter.
sed_escape() {
  printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

# ...and safe inside a plist <string> (XML text).
xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

value() {
  sed_escape "$(xml_escape "$1")"
}

render() {
  sed -e "$KEEP_ENV" \
    -e "s|__REPO__|$(value "$REPO")|g" \
    -e "s|__LOG_DIR__|$(value "$LOG_DIR")|g" \
    -e "s|__DATA_DIR__|$(value "$DATA_DIR")|g" \
    -e "s|__LABEL_PREFIX__|$(value "$PREFIX")|g" "$1"
}

# Other blastjob agents for this service: installed plists and loaded labels.
others() {
  name="$1"
  mine="$PREFIX.blastjob.$name"
  for plist in "$AGENTS"/*.blastjob."$name".plist; do
    [ -e "$plist" ] || continue
    label=$(basename "$plist" .plist)
    [ "$label" = "$mine" ] || echo "$label"
  done
  launchctl list 2>/dev/null | awk '{print $3}' | grep "\.blastjob\.$name\$" | grep -v -x -F "$mine" || true
}

if [ -n "$RENDER_ONLY" ]; then
  for name in $WHICH; do
    src="$REPO/launchd/blastjob.$name.plist.template"
    [ -f "$src" ] || { echo "unknown service: $name" >&2; exit 1; }
    render "$src"
  done
  exit 0
fi

conflicts=""
for name in $WHICH; do
  conflicts="$conflicts $(others "$name" | sort -u | tr '\n' ' ')"
done
conflicts=$(echo $conflicts)
if [ -n "$conflicts" ]; then
  if [ -z "$REPLACE" ]; then
    echo "other blastjob agents are installed or loaded: $conflicts" >&2
    echo "they would run alongside the new ones. Unload them first, e.g.:" >&2
    for label in $conflicts; do
      echo "  launchctl unload $AGENTS/$label.plist && rm $AGENTS/$label.plist" >&2
    done
    echo "or re-run with --replace to have this script unload and remove them." >&2
    exit 1
  fi
  for label in $conflicts; do
    plist="$AGENTS/$label.plist"
    if [ -e "$plist" ]; then
      launchctl unload "$plist" 2>/dev/null || true
      rm -f "$plist"
      echo "replaced: unloaded and removed $plist"
    else
      launchctl remove "$label" 2>/dev/null || true
      echo "replaced: removed loaded agent $label"
    fi
  done
fi

mkdir -p "$AGENTS"
(umask 077 && mkdir -p "$LOG_DIR")   # a new data dir is owner-only
for name in $WHICH; do
  src="$REPO/launchd/blastjob.$name.plist.template"
  dest="$AGENTS/$PREFIX.blastjob.$name.plist"
  [ -f "$src" ] || { echo "unknown service: $name" >&2; exit 1; }
  render "$src" > "$dest"
  launchctl unload "$dest" 2>/dev/null || true
  launchctl load "$dest"
  echo "loaded $dest"
done

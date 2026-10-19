#!/bin/sh
# Install a git pre-push check that runs scripts/check_no_personal_data.py and
# blocks the push on any FAIL. Local to this clone; nothing is committed.
#   sh scripts/install-git-hooks.sh
# Skip it once (after reading the findings): git push --no-verify
#
# The check lives in its own file, hooks/blastjob-personal-data-check. If a
# pre-push hook already exists (a git template may install one), one line
# calling the check is added right after its first line, so it runs before
# anything that hook does. Running this again changes nothing.
set -eu

hooks="$(git rev-parse --path-format=absolute --git-common-dir)/hooks"
check="$hooks/blastjob-personal-data-check"
hook="$hooks/pre-push"
call='"$(dirname "$0")/blastjob-personal-data-check" || exit 1'

mkdir -p "$hooks"
cat > "$check" <<'EOF'
#!/bin/sh
# Installed by scripts/install-git-hooks.sh. Exits 1 if the personal-data
# check finds the owner's data in tracked files.
top="$(git rev-parse --show-toplevel)"
# The allowlist lives in the main checkout's gitignored local/, which a
# worktree does not have.
allow="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")/local/leak_allowlist.txt"
set -- --repo "$top" --summary
if [ -f "$allow" ]; then set -- "$@" --allowlist "$allow"; fi
cd "$top" || exit 1
if ! uv run python scripts/check_no_personal_data.py "$@"; then
  echo "" >&2
  echo "Push blocked: the personal-data check found your data in tracked files." >&2
  echo "Full report: uv run python scripts/check_no_personal_data.py" >&2
  exit 1
fi
EOF
chmod +x "$check"

if [ ! -e "$hook" ]; then
  printf '#!/bin/sh\n%s\n' "$call" > "$hook"
elif ! grep -qF "$call" "$hook"; then
  tmp="$hook.tmp.$$"
  { head -n 1 "$hook"; echo "$call"; tail -n +2 "$hook"; } > "$tmp"
  mv "$tmp" "$hook"
fi
chmod +x "$hook"
echo "Installed: $hook runs $check"

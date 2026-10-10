#!/bin/sh
# End-to-end check that the cockpit is genuinely reachable from the LAN.
#
# Written because a unit test suite cannot answer the only question that matters
# here: can another device on the network actually load and use the cockpit? The
# code change passed 523 unit tests while the feature was still completely
# broken, because the macOS firewall was dropping the packets.
#
# Every probe below is read-only or deliberately non-mutating.
#   ./scripts/verify-lan-access.sh [remote-ssh-host]
set -u

PORT="${PORT:-8765}"
REMOTE="${1:-}"  # optional: an ssh host on your LAN to probe from
IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
fails=0

ok()   { printf '  PASS  %s\n' "$1"; }
bad()  { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }
note() { printf '        %s\n' "$1"; }

printf '\nCockpit LAN reachability — %s:%s\n\n' "$IP" "$PORT"

# 1. Listening on every interface, not just loopback.
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | grep -q '\*:'"$PORT"; then
  ok "bound to all interfaces"
else
  bad "not bound to all interfaces (still loopback-only?)"
  note "$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | tail -1)"
fi

# 2. Loopback still works, so a regression here is obvious.
code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:$PORT/" 2>/dev/null)
[ "$code" = "200" ] && ok "loopback serves (200)" || bad "loopback returned $code"

# 3. The LAN address, from this machine. A TIMEOUT here means a firewall is
#    dropping packets; "connection refused" would mean nothing is listening.
code=$(curl -s -o /dev/null -m 6 -w '%{http_code}' "http://$IP:$PORT/" 2>/dev/null)
if [ "$code" = "200" ]; then
  ok "LAN address serves from this host (200)"
else
  bad "LAN address returned $code from this host"
  note "a timeout here is the macOS firewall, not the app"
fi

# 4. The real test: another machine on the network. Nothing else proves this.
if [ -z "$REMOTE" ]; then
  note "skipped the external check: pass an ssh host on your LAN as the first argument"
elif ssh -o BatchMode=yes -o ConnectTimeout=6 "$REMOTE" true 2>/dev/null; then
  code=$(ssh -o BatchMode=yes -o ConnectTimeout=8 "$REMOTE" \
    "curl -s -o /dev/null -m 6 -w '%{http_code}' http://$IP:$PORT/" 2>/dev/null)
  if [ "$code" = "200" ]; then
    ok "reachable from $REMOTE (200) — this is the one that counts"
  else
    bad "NOT reachable from $REMOTE (got $code)"
  fi
else
  bad "could not ssh to $REMOTE to run the external check"
fi

# 5. A write from a LAN origin must be accepted, or another device can read but every
#    button fails. Probed against a job id that cannot exist, so nothing changes:
#    403 means the origin was rejected, 404 means it was accepted.
token=$(curl -s -m 5 "http://127.0.0.1:$PORT/" 2>/dev/null \
  | sed -n "s/.*hx-headers='{\"x-blastjob-token\": \"\([^\"]*\)\".*/\1/p" | head -1)
if [ -n "$token" ]; then
  code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' -X POST \
    -H "x-blastjob-token: $token" -H "Origin: http://$IP:$PORT" \
    "http://127.0.0.1:$PORT/jobs/99999999/keep" 2>/dev/null)
  case "$code" in
    404) ok "a write from a LAN origin is accepted (404 = origin OK, job absent)" ;;
    403) bad "LAN origin REJECTED (403) — buttons would fail on another device" ;;
    *)   bad "unexpected $code probing the LAN origin" ;;
  esac
  code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' -X POST \
    -H "x-blastjob-token: $token" -H "Origin: https://evil.example.com" \
    "http://127.0.0.1:$PORT/jobs/99999999/keep" 2>/dev/null)
  [ "$code" = "403" ] && ok "a public origin is still rejected (403)" \
    || bad "public origin got $code, expected 403"
else
  bad "could not read the CSRF token from the page"
fi

printf '\n'
if [ "$fails" -eq 0 ]; then
  printf 'ALL PASS — open http://%s:%s on another device.\n\n' "$IP" "$PORT"
  exit 0
fi
printf '%d check(s) FAILED.\n' "$fails"
printf 'If only the reachability checks failed, the firewall is blocking the venv\n'
printf 'python. Run these, then re-run this script:\n\n'
BIN=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1)
BIN=$(ps -p "${BIN:-0}" -o comm= 2>/dev/null)
[ -n "$BIN" ] && BIN=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$BIN" 2>/dev/null)
printf '  sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add %s\n' "${BIN:-<venv python>}"
printf '  sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp %s\n\n' "${BIN:-<venv python>}"
exit 1

#!/usr/bin/env bash
# Check that this machine is set up safely for the swarm. Run it after joining the tailnet:
#
#   swarm/check_security.sh <board-url>            # on a machine that runs agents
#   swarm/check_security.sh <board-url> --board    # on the board machine (after starting the board)
#
# Needs SWARM_END in the environment, the same value the agents use.
# Exit code 0 means every check passed; failures are printed with a fix.
set -uo pipefail

BOARD="${1:?usage: swarm/check_security.sh <board-url> [--board]}"
ROLE="agent"; [ "${2:-}" = "--board" ] && ROLE="board"
HOST=$(python3 -c "import sys, urllib.parse; print(urllib.parse.urlparse(sys.argv[1]).hostname)" "$BOARD")
fails=0
ok()   { echo "  ok    $*"; }
fail() { echo "  FAIL  $*"; fails=$((fails + 1)); }
warn() { echo "  warn  $*"; }

echo "Tailscale"
if ! command -v tailscale >/dev/null; then
  fail "tailscale is not installed (https://tailscale.com/download)"
else
  status=$(tailscale status --json 2>/dev/null || echo '{}')
  read -r state tailnet tags <<<"$(python3 -c '
import json, sys
s = json.loads(sys.stdin.read() or "{}")
print(s.get("BackendState", "?"), (s.get("CurrentTailnet") or {}).get("Name", "?"), ",".join((s.get("Self") or {}).get("Tags") or []) or "-")
' <<<"$status")"
  [ "$state" = "Running" ] && ok "connected to tailnet $tailnet" || fail "tailscale is not connected (state $state); run: sudo tailscale up --authkey=<key>"
  want="tag:swarm-$ROLE"
  [[ ",$tags," == *",$want,"* ]] && ok "this machine is tagged $want" \
    || fail "this machine has tags '$tags', expected $want; the access policy only protects tagged machines. Rejoin with a $want auth key"
  if tailscale funnel status 2>/dev/null | grep -qi "funnel on\|https://"; then
    fail "Tailscale Funnel/Serve is exposing something from this machine to the internet; turn it off: tailscale funnel reset"
  else
    ok "nothing on this machine is published to the internet with Funnel"
  fi
fi

echo "Board"
addr=$(getent ahosts "$HOST" | awk 'NR==1 {print $1}')
if [[ "$addr" == 100.* || "$addr" == fd7a:115c:a1e0:* ]]; then
  ok "$HOST resolves to tailnet address $addr"
else
  fail "$HOST resolves to '${addr:-nothing}', not a tailnet address; use the board's tailnet name (e.g. http://<machine>.<tailnet>.ts.net:8000)"
fi
if curl -fsS --max-time 5 "${BOARD%/}/healthz" >/dev/null 2>&1; then ok "board reachable at $BOARD"; else fail "board not reachable at $BOARD"; fi

if [ "$ROLE" = "agent" ] && [[ "$addr" == 100.* || "$addr" == fd7a:115c:a1e0:* ]]; then
  # Tailscale drops packets the policy doesn't allow, so a blocked port times out; a closed but
  # allowed port answers at once with "connection refused".
  timeout 4 bash -c "exec 3<>/dev/tcp/$HOST/22" 2>/dev/null; rc=$?
  if [ $rc -eq 124 ]; then ok "other ports on the board machine are blocked (port 22 timed out)"
  else fail "port 22 on the board machine answered (rc=$rc), so the access policy isn't applied; paste swarm/tailscale-policy.hujson into the admin console"; fi
elif [ "$ROLE" = "agent" ]; then
  fail "skipped the blocked-port check: the board isn't on the tailnet yet"
else
  bad=$(ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E ':(8000|8090|8100)$' | grep -vE '^(100\.|\[fd7a:115c:a1e0:|127\.0\.0\.1:|\[::1\]:)' || true)
  listening=$(ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E ':(8000|8090|8100)$' | tr '\n' ' ')
  if [ -n "$bad" ]; then
    fail "the board or referee listens on $bad, which is reachable outside the tailnet; restart with HOST=\$(tailscale ip -4)"
  elif [ -n "$listening" ]; then
    ok "board/referee listen only on tailnet or loopback addresses ($listening)"
  else
    warn "nothing is listening on 8000/8090/8100 yet; start the board, then re-run this check"
  fi
  pgrep -f -- "-m runner" >/dev/null && warn "agents also run on this machine (fine: they're trusted not to touch the board's database or the referee's answers)"
fi

echo "End time"
if [ -z "${SWARM_END:-}" ]; then
  fail "SWARM_END is not set; export it, e.g. SWARM_END=2026-11-01T08:00:00-05:00"
else
  end=$(date -d "$SWARM_END" +%s 2>/dev/null || echo "")
  if [ -z "$end" ]; then fail "SWARM_END=$SWARM_END is not a valid time"
  elif [ "$end" -le "$(date +%s)" ]; then fail "SWARM_END=$SWARM_END is already in the past"
  else ok "SWARM_END=$SWARM_END ($(( (end - $(date +%s)) / 3600 )) h from now): agents, referee and autopull stop then"; fi
fi

echo
[ $fails -eq 0 ] && echo "All checks passed." || echo "$fails check(s) failed."
exit $(( fails > 0 ))

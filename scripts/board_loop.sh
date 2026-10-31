#!/usr/bin/env bash
# Keep the board up for good: run `make live`, and restart it when it exits, when it stops
# answering /healthz, or when a leftover server (e.g. a reload worker whose supervisor died)
# holds the port. Runs from the repo it lives in; HOST, PORT, SWARM_END and DATABASE_URL pass
# through to the board.
#
#   HOST=<tailnet ip> SWARM_END=<iso time> scripts/board_loop.sh
#
# Tuning (env): STARTUP_WAIT (20 s), CHECK_EVERY (15 s), MAX_FAILS (3 failed checks in a row).
cd "$(dirname "$0")/.."
PORT="${PORT:-8000}"
export PORT
STARTUP_WAIT="${STARTUP_WAIT:-20}" CHECK_EVERY="${CHECK_EVERY:-15}" MAX_FAILS="${MAX_FAILS:-3}"
HEALTH="http://${HOST:-127.0.0.1}:$PORT/healthz"
[ "${HOST:-}" = "0.0.0.0" ] && HEALTH="http://127.0.0.1:$PORT/healthz"
log() { echo "board-loop $(date '+%H:%M:%S'): $*"; }

free_port() {
  # Our own processes still listening on the port (ss shows pids only for the current user).
  pids=$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u)
  if [ -n "$pids" ]; then
    log "port $PORT still held by $pids; stopping them"
    kill $pids 2>/dev/null; sleep 2; kill -9 $pids 2>/dev/null
  fi
}

while true; do
  free_port
  setsid make live &   # its own process group, so a hung board can be stopped as a whole
  group=$!
  log "started board (process group $group)"
  sleep "$STARTUP_WAIT"
  fails=0
  while kill -0 "$group" 2>/dev/null; do
    if curl -fs -o /dev/null --max-time 5 "$HEALTH"; then fails=0; else fails=$((fails + 1)); fi
    if [ "$fails" -ge "$MAX_FAILS" ]; then
      log "no answer from $HEALTH $fails times in a row; restarting"
      kill -TERM -- "-$group" 2>/dev/null; sleep 3; kill -KILL -- "-$group" 2>/dev/null
      break
    fi
    sleep "$CHECK_EVERY"
  done
  log "board stopped; restarting in 5 s"
  sleep 5
done

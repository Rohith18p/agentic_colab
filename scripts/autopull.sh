#!/usr/bin/env bash
# Fast-forward this checkout from its upstream every N seconds (default 30).
# Used by `make live` on the always-on board machine; with --reload the server
# then restarts by itself. Local edits or diverged history make the pull a
# no-op (it only fast-forwards), so nothing is ever overwritten.
# Stops for good once the swarm's end time passes: the board's own setting (data/swarm_end.json, set by a human
# with `board.py swarm-end`) if there is one, else SWARM_END (ISO time with offset), else never.
cd "$(dirname "$0")/.."
interval="${1:-30}"
end_epoch() {
  if [ -f data/swarm_end.json ]; then
    t=$(python3 -c 'import json; print(json.load(open("data/swarm_end.json")).get("swarm_end") or "")' 2>/dev/null)
  else
    t="${SWARM_END:-}"
  fi
  [ -n "$t" ] && date -d "$t" +%s
}
while true; do
  e=$(end_epoch)
  if [ -n "$e" ] && [ "$(date +%s)" -ge "$e" ]; then
    echo "autopull: SWARM_END reached; no more updates"
    exit 0
  fi
  before=$(git rev-parse HEAD)
  if git pull --ff-only -q 2>/dev/null; then
    after=$(git rev-parse HEAD)
    [ "$before" != "$after" ] && echo "autopull: $(git log --oneline -1)"
  fi
  sleep "$interval"
done

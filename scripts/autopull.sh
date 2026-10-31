#!/usr/bin/env bash
# Fast-forward this checkout from its upstream every N seconds (default 30).
# Used by `make live` on the always-on board machine; with --reload the server
# then restarts by itself. Local edits or diverged history make the pull a
# no-op (it only fast-forwards), so nothing is ever overwritten.
# Stops for good after SWARM_END (ISO time with offset), if set.
cd "$(dirname "$0")/.."
interval="${1:-30}"
end_epoch=""
[ -n "${SWARM_END:-}" ] && end_epoch=$(date -d "$SWARM_END" +%s)
while true; do
  if [ -n "$end_epoch" ] && [ "$(date +%s)" -ge "$end_epoch" ]; then
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

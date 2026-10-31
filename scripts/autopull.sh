#!/usr/bin/env bash
# Fast-forward this checkout from its upstream every N seconds (default 30).
# Used by `make live` on the always-on board machine; with --reload the server
# then restarts by itself. Local edits or diverged history make the pull a
# no-op (it only fast-forwards), so nothing is ever overwritten.
cd "$(dirname "$0")/.."
interval="${1:-30}"
while true; do
  before=$(git rev-parse HEAD)
  if git pull --ff-only -q 2>/dev/null; then
    after=$(git rev-parse HEAD)
    [ "$before" != "$after" ] && echo "autopull: $(git log --oneline -1)"
  fi
  sleep "$interval"
done

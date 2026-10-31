#!/usr/bin/env bash
# Chat with the swarm's explainer in your terminal: ask what the agents are doing and why,
# and steer them. Every change it makes is shown to you first (Claude Code asks before it
# runs anything) and is posted as "<your name> (via explainer)".
#
#   swarm/explain.sh <board-url> "<your name>" ["first question"]
set -euo pipefail
BOARD="${1:?usage: swarm/explain.sh <board-url> \"<your name>\" [\"first question\"]}"
WHO="${2:?usage: swarm/explain.sh <board-url> \"<your name>\" [\"first question\"]}"
FIRST="${3:-Give me a short status of the swarm: who is doing what, what has been found and how solid it is, and anything stuck or waiting on a human.}"
BOARD="${BOARD%/}"
cd "$(dirname "$0")/.."

curl -fsS --max-time 5 "$BOARD/healthz" >/dev/null || { echo "Cannot reach the board at $BOARD"; exit 1; }
command -v claude >/dev/null || { echo "Install Claude Code first."; exit 1; }

cfg_dir="${XDG_CACHE_HOME:-$HOME/.cache}/swarm-explain"
mkdir -p "$cfg_dir"
printf '{"mcpServers": {"board": {"type": "http", "url": "%s/mcp"}}}\n' "$BOARD" > "$cfg_dir/mcp.json"
system="$(cat swarm/EXPLAINER_PROMPT.md)

You are in a terminal chat with $WHO. Attribute every change to \"$WHO (via explainer)\".
The board is at $BOARD; the board CLI is scripts/board.py in the current directory."

export WORKSPACE_URL="$BOARD"
# Reading the board is pre-approved; anything that writes goes through Claude Code's permission prompt.
# Use the `claude` login (your plan's usage), never an API key from the environment.
strip="-u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN"; [ "${ALLOW_API_BILLING:-0}" = "1" ] && strip=""
exec env -u PYTHONPATH $strip claude --mcp-config "$cfg_dir/mcp.json" --strict-mcp-config --append-system-prompt "$system" \
  --allowedTools "mcp__board__read_charter mcp__board__list_agents mcp__board__list_messages mcp__board__list_tasks Read Glob Grep" \
  "$FIRST"

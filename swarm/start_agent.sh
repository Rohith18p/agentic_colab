#!/usr/bin/env bash
# Start one swarm agent on this machine, connected to the board on another machine.
#
#   swarm/start_agent.sh <board-url> <agent-name> [skills]
#   e.g. swarm/start_agent.sh http://board-box.tail1234.ts.net:8000 alice-1 cosmos-gpu
#
# Checks that the board is reachable and Claude Code is installed, creates a small
# venv with the one runner dependency, then runs the agent until you stop it
# (Ctrl-C). Run it inside tmux so it survives SSH disconnects:
#   tmux new -s alice-1 'swarm/start_agent.sh http://board-box...:8000 alice-1 cosmos-gpu'
# Optional overrides (env): AGENT_OWNER, AGENT_MODEL, STEP_SECONDS, MAX_SHIFTS, AGENT_WORKDIR,
# AUTO_UPDATE (see runner/claude_code.py). There are no spend caps.
#
# Hot reload: the agent pulls this repo before every shift (AUTO_UPDATE=1) and
# restarts itself if runner code changed; the playbook is re-read every shift;
# ~/swarm-work/<name>/agent.env (KEY=VALUE) changes the model or budgets live.
# Requires SWARM_END (the agent stops for good then) and runs swarm/check_security.sh first.
set -euo pipefail

BOARD="${1:?usage: swarm/start_agent.sh <board-url> <agent-name> [skills]}"
NAME="${2:?usage: swarm/start_agent.sh <board-url> <agent-name> [skills]}"
SKILLS="${3:-}"
BOARD="${BOARD%/}"
cd "$(dirname "$0")/.."

if [ -z "${SWARM_END:-}" ]; then
  echo "Set SWARM_END first (the time the swarm stops for good), e.g.:"
  echo "  export SWARM_END=2026-11-01T08:00:00-05:00"
  exit 1
fi
if [ "${SKIP_SECURITY_CHECK:-0}" != "1" ] && ! swarm/check_security.sh "$BOARD"; then
  echo "Not starting: fix the failed checks above (or SKIP_SECURITY_CHECK=1 for a local test only)."
  exit 1
fi
if ! curl -fsS --max-time 5 "$BOARD/api/state" >/dev/null; then
  echo "Cannot reach the board at $BOARD."
  echo "Check that this machine is on the tailnet (tailscale status) and the board is running with HOST=0.0.0.0."
  exit 1
fi
if ! command -v claude >/dev/null; then
  echo "Claude Code is not installed on this machine. Install it, then log in once by running 'claude'."
  exit 1
fi
# Shifts run on your `claude` login, so they use your plan's included usage. The harness removes
# ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN from every shift unless ALLOW_API_BILLING=1, because in
# `claude -p` a key in the environment always wins and bills API credits instead.
if [ "${ALLOW_API_BILLING:-0}" != "1" ]; then
  auth=$(env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN claude auth status --json 2>/dev/null || echo '{}')
  read -r logged method plan <<<"$(python3 -c '
import json, sys
d = json.loads(sys.stdin.read() or "{}")
print(d.get("loggedIn", False), d.get("authMethod", "?"), d.get("subscriptionType") or "?")' <<<"$auth")"
  if [ "$logged" != "True" ] || [ "$method" != "claude.ai" ]; then
    echo "Not starting: Claude Code isn't logged in with a Claude account on this machine (auth: $method)."
    echo "Run 'claude', log in with your Pro/Max/Team/Enterprise account, then try again."
    exit 1
  fi
  echo "Billing: Claude account login, $plan plan (included usage; no API credits)."
  [ -n "${ANTHROPIC_API_KEY:-}${ANTHROPIC_AUTH_TOKEN:-}" ] && echo "note: ANTHROPIC_API_KEY/AUTH_TOKEN are set in this shell; shifts won't use them."
fi
# Research MCP servers every shift gets (copied from your Claude Code config; see swarm/README.md).
# Informational only: a missing one just means agents on this machine run without it.
for server in $(echo "${EXTRA_MCP_SERVERS-alphaxiv,hf-mcp-server}" | tr ',' ' '); do
  status=$(claude mcp get "$server" 2>/dev/null | sed -n 's/^ *Status: *//p' || true)
  case "$status" in
    *onnected*) echo "Research tool $server: connected" ;;
    "") echo "Research tool $server: not set up, so agents here run without it (to add it: swarm/README.md, \"Research tools\")" ;;
    *) echo "Research tool $server: $status. Run 'claude', type /mcp and log in to $server." ;;
  esac
done

if [ ! -x .venv/bin/python ] || ! env -u PYTHONPATH .venv/bin/python -c "import mcp" 2>/dev/null; then
  echo "Creating .venv with the runner dependency (mcp==2.3.0)..."
  if command -v uv >/dev/null; then
    uv venv --python 3.12 --allow-existing .venv && uv pip install --python .venv/bin/python mcp==2.3.0
  else
    python3.12 -m venv .venv && .venv/bin/pip install -q mcp==2.3.0
  fi
fi

export WORKSPACE_URL="$BOARD" AGENT_NAME="$NAME" AGENT_SKILLS="$SKILLS" RUNNER_AGENT=claude
export AGENT_OWNER="${AGENT_OWNER:-$USER}"
export STEP_SECONDS="${STEP_SECONDS:-120}" AUTO_UPDATE="${AUTO_UPDATE:-1}" SWARM_END
echo "Starting $NAME (skills: ${SKILLS:-none}) against $BOARD. Work dir: ${AGENT_WORKDIR:-$HOME/swarm-work/$NAME}"
exec env -u PYTHONPATH .venv/bin/python -m runner

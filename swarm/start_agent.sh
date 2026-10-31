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
# Optional overrides (env): AGENT_OWNER, AGENT_MODEL, STEP_SECONDS, SHIFT_BUDGET_USD,
# TOTAL_BUDGET_USD, MAX_SHIFTS, AGENT_WORKDIR, AUTO_UPDATE (see runner/claude_code.py).
#
# Hot reload: the agent pulls this repo before every shift (AUTO_UPDATE=1) and
# restarts itself if runner code changed; the playbook is re-read every shift;
# ~/swarm-work/<name>/agent.env (KEY=VALUE) changes the model or budgets live.
set -euo pipefail

BOARD="${1:?usage: swarm/start_agent.sh <board-url> <agent-name> [skills]}"
NAME="${2:?usage: swarm/start_agent.sh <board-url> <agent-name> [skills]}"
SKILLS="${3:-}"
BOARD="${BOARD%/}"
cd "$(dirname "$0")/.."

if ! curl -fsS --max-time 5 "$BOARD/api/state" >/dev/null; then
  echo "Cannot reach the board at $BOARD."
  echo "Check that this machine is on the tailnet (tailscale status) and the board is running with HOST=0.0.0.0."
  exit 1
fi
if ! command -v claude >/dev/null; then
  echo "Claude Code is not installed on this machine. Install it, then authenticate it"
  echo "(ANTHROPIC_API_KEY in the environment, or log in once with 'claude')."
  exit 1
fi
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "note: ANTHROPIC_API_KEY is not set; using Claude Code's own login."
fi

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
export STEP_SECONDS="${STEP_SECONDS:-120}" SHIFT_BUDGET_USD="${SHIFT_BUDGET_USD:-1.5}"
export TOTAL_BUDGET_USD="${TOTAL_BUDGET_USD:-60}" AUTO_UPDATE="${AUTO_UPDATE:-1}"
echo "Starting $NAME (skills: ${SKILLS:-none}) against $BOARD. Work dir: ${AGENT_WORKDIR:-$HOME/swarm-work/$NAME}"
exec env -u PYTHONPATH .venv/bin/python -m runner

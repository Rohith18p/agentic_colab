#!/usr/bin/env bash
# Run the dashboard explainer: an agent that answers whenever someone @mentions it (the
# dashboard's Ask tab does this for you). It runs a short shift only when asked, so it
# costs nothing while idle. Same safety checks as any agent (SWARM_END, check_security.sh).
#
#   swarm/start_explainer.sh <board-url> [name]      # name defaults to "explainer"
set -euo pipefail
BOARD="${1:?usage: swarm/start_explainer.sh <board-url> [name]}"
NAME="${2:-explainer}"
cd "$(dirname "$0")/.."
export AGENT_PROMPT_FILE="$PWD/swarm/EXPLAINER_PROMPT.md" ON_MENTION=1 SHIFT_REPORTS=0
export STEP_SECONDS="${STEP_SECONDS:-5}" SHIFT_BUDGET_USD="${SHIFT_BUDGET_USD:-1.5}" TOTAL_BUDGET_USD="${TOTAL_BUDGET_USD:-40}"
exec swarm/start_agent.sh "$BOARD" "$NAME" explainer

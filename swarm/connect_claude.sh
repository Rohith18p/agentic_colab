#!/usr/bin/env bash
# Plug your own Claude Code sessions into the swarm.
#
#   swarm/connect_claude.sh <board-url> "<your name>"     # install
#   swarm/connect_claude.sh --remove                     # undo (offboard.sh also does this)
#
# Adds the board as an MCP server for all your Claude Code sessions (user scope) and
# installs two skills:
#   /swarm-explainer   ask what the swarm is doing and steer it (or just ask about the swarm)
#   /swarm-agent <name> make this session a research agent; `/loop /swarm-agent <name>` keeps it going
# The skills point at the playbooks in this checkout, so pulled updates apply right away.
# Sessions use your `claude` login (your plan's usage), like any other Claude Code session.
set -euo pipefail
SKILLS_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
cd "$(dirname "$0")/.."
REPO="$PWD"

if [ "${1:-}" = "--remove" ]; then
  claude mcp remove --scope user board >/dev/null 2>&1 && echo "removed the board MCP server" || echo "no board MCP server to remove"
  rm -rf "$SKILLS_DIR/swarm-agent" "$SKILLS_DIR/swarm-explainer" && echo "removed the swarm skills"
  exit 0
fi

BOARD="${1:?usage: swarm/connect_claude.sh <board-url> \"<your name>\"   (or --remove)}"
WHO="${2:?usage: swarm/connect_claude.sh <board-url> \"<your name>\"}"
BOARD="${BOARD%/}"
curl -fsS --max-time 5 "$BOARD/healthz" >/dev/null || { echo "Cannot reach the board at $BOARD"; exit 1; }
command -v claude >/dev/null || { echo "Install Claude Code first."; exit 1; }

claude mcp remove --scope user board >/dev/null 2>&1 || true
claude mcp add --scope user --transport http board "$BOARD/mcp" >/dev/null
echo "added MCP server 'board' -> $BOARD/mcp (all your sessions)"

mkdir -p "$SKILLS_DIR/swarm-explainer" "$SKILLS_DIR/swarm-agent"
cat > "$SKILLS_DIR/swarm-explainer/SKILL.md" <<EOF
---
name: swarm-explainer
description: Explain what the hackathon agent swarm is doing and steer it, using the shared board (the "board" MCP server). Use when the user asks about the swarm, its agents, tasks, findings or proposals, or wants to change what the swarm is doing.
---

# Swarm explainer

Read \`$REPO/swarm/EXPLAINER_PROMPT.md\` now and follow it in **terminal chat** mode.

- You are talking with **$WHO**. Attribute every change to "$WHO (via explainer)".
- The board is at $BOARD. For changes, use the board CLI:
  \`python3 $REPO/scripts/board.py --url $BOARD --as "$WHO (via explainer)" <steer|post|task|pause|resume|answer> ...\`
- Show the exact action first. The user approves it in Claude Code's permission prompt.
EOF

cat > "$SKILLS_DIR/swarm-agent/SKILL.md" <<EOF
---
name: swarm-agent
description: Work as a research agent in the hackathon swarm through this Claude Code session, coordinating on the shared board (the "board" MCP server). Use when the user asks this session to join the swarm, work as a swarm agent, or do a swarm shift. Takes the agent name as its argument, e.g. /swarm-agent alice-cc-1.
---

# Work as a swarm agent

Your agent name is: \$ARGUMENTS. If that's empty, ask the user for a unique name such as \`<owner>-cc-1\`.

1. **Read the playbook** \`$REPO/swarm/AGENT_PROMPT.md\` now, and again at the start of every unit of work, because it changes. Follow it. This skill only adds what differs in an interactive session.
2. **Register** once per session: \`register_agent\` with your name, owner "$WHO", this machine (\`hostname\` plus GPUs from \`nvidia-smi\`), and skills describing the machine.
3. **Keep your notebook** in \`~/swarm-work/<name>/NOTEBOOK.md\`. Read it first.
4. **What differs from unattended agents** (the runner harness does these for them):
   - **Leases:** nothing heartbeats for you. Call \`heartbeat\` (with a one-line \`approach\`) at the start and end of each unit of work, and before anything that runs over a minute. Run long jobs in the background.
   - **Shift report:** after each unit of work, post the playbook's 3–6 line report yourself with \`post_message(channel="activity", ...)\`, starting \`Shift (interactive) ·\` plus the task you hold.
   - **Asking:** the user is here, so ask them quick questions directly. Anything that affects other agents still goes on the board.
   - **Usage:** this session draws on the plan allowance of whoever is logged in. Keep context small, and suggest \`/clear\` between units of work; the board and your notebook are your memory.
   - **Stopping:** if a tool returns \`agent_paused\` or \`swarm_ended\`, stop.
5. **To keep working without prompts,** the user can run \`/loop /swarm-agent <name>\`.
EOF
echo "installed skills /swarm-explainer and /swarm-agent in $SKILLS_DIR"
if [ -n "${ANTHROPIC_API_KEY:-}${ANTHROPIC_AUTH_TOKEN:-}" ]; then
  echo "warning: ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is set in this shell. Claude Code sessions started"
  echo "         from it may bill API usage instead of your plan. Unset it, and check /status."
fi
echo "Open any Claude Code session and ask about the swarm, or type /swarm-explainer or /swarm-agent <name>."

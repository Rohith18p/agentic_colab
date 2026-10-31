#!/usr/bin/env bash
# Take this machine off the swarm when the hackathon is over.
#
#   swarm/offboard.sh          # show what would be stopped
#   swarm/offboard.sh --yes    # stop it all and log this machine out of the tailnet
#   swarm/offboard.sh --yes --keep-tailscale
#
# Stops: agent runners, anything still running from an agent work directory
# (~/swarm-work/*), Docker containers labelled "swarm", and the board, referee and
# autopull if they run here. Then logs out of Tailscale, which removes this machine
# from the tailnet. Agent work directories are kept; delete them yourself when
# you've copied what you need.
set -uo pipefail
YES=0; KEEP_TS=0
for arg in "$@"; do
  case "$arg" in --yes) YES=1 ;; --keep-tailscale) KEEP_TS=1 ;; *) echo "unknown option $arg"; exit 2 ;; esac
done
WORK="${SWARM_WORK_ROOT:-$HOME/swarm-work}"

pids=()
for proc in /proc/[0-9]*; do
  pid=${proc#/proc/}
  [ "$pid" = "$$" ] && continue
  cmd=$(tr '\0' ' ' <"$proc/cmdline" 2>/dev/null) || continue
  cwd=$(readlink "$proc/cwd" 2>/dev/null || true)
  if [[ "$cmd" == *"-m runner"* || "$cmd" == *"-m agentic_colab"* || "$cmd" == *"autopull.sh"* \
        || "$cmd" == *"referee.py"* || "$cwd" == "$WORK"/* ]]; then
    pids+=("$pid")
    printf '  %-8s %s\n' "$pid" "${cmd:0:120}"
  fi
done
containers=$(command -v docker >/dev/null && docker ps -q --filter label=swarm 2>/dev/null || true)
[ -n "$containers" ] && docker ps --filter label=swarm --format '  container {{.ID}} {{.Image}} {{.Names}}'
[ ${#pids[@]} -eq 0 ] && [ -z "$containers" ] && echo "  no swarm processes or containers running"

if [ $YES -eq 0 ]; then
  echo
  echo "Dry run. Re-run with --yes to stop the above$([ $KEEP_TS -eq 0 ] && echo " and log out of Tailscale")."
  exit 0
fi

[ ${#pids[@]} -gt 0 ] && kill -TERM "${pids[@]}" 2>/dev/null
[ -n "$containers" ] && docker stop $containers >/dev/null
sleep 5
for pid in "${pids[@]}"; do kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null; done
echo "Stopped ${#pids[@]} process(es)$([ -n "$containers" ] && echo " and $(wc -w <<<"$containers") container(s)")."

if command -v claude >/dev/null; then
  swarm_dir="$(cd "$(dirname "$0")" && pwd)"
  "$swarm_dir/connect_claude.sh" --remove >/dev/null 2>&1 && echo "Removed the board MCP server and swarm skills from Claude Code."
fi

if [ $KEEP_TS -eq 0 ] && command -v tailscale >/dev/null; then
  sudo tailscale logout && echo "Logged out of the tailnet; this machine is no longer reachable from it."
fi

cat <<'EOF'

Left to do (admin, once for the whole team):
  - Tailscale admin console: revoke the swarm auth keys, remove any devices still listed (or delete the tailnet).
  - Rotate any keys the agents could read (Gemini, Hugging Face, GitHub tokens): agents had a shell with them.
  - When you've copied what you need, delete the agents' work directories: rm -rf ~/swarm-work
EOF

#!/usr/bin/env bash
# Share this machine's compute with the swarm. Agents on any tailnet machine can then run
#   ssh swarm@<this-machine>.<tailnet>.ts.net
# and do anything there as the unprivileged `swarm` user: run GPU jobs, simulations,
# fine-tunes, install packages in its home. Not as you, not as root, so they can't touch
# your files (or, on the board machine, the board's database).
#
#   swarm/share_compute.sh <board-url>            # share; announces the machine in #compute
#   swarm/share_compute.sh <board-url> --docker   # also let swarm use Docker (root-equivalent here)
#   swarm/share_compute.sh <board-url> --admin    # Docker plus passwordless sudo: agents can run and fix
#                                                 # anything here (for a machine nobody can log in to later)
#   swarm/share_compute.sh --stop                 # stop: kill swarm's processes, remove the user
#                                                 # and its files, turn off Tailscale SSH
# Needs sudo, and the tailnet policy from swarm/tailscale-policy.hujson (its "ssh" rule).
set -euo pipefail

if [ "${1:-}" = "--stop" ]; then
  sudo tailscale set --ssh=false || true
  if id swarm >/dev/null 2>&1; then
    sudo pkill -u swarm || true
    sleep 2
    sudo pkill -9 -u swarm || true
    sudo userdel -r swarm 2>/dev/null || sudo userdel swarm
    echo "Removed the swarm user, its processes and its files."
  fi
  sudo rm -f /etc/sudoers.d/swarm
  echo "This machine no longer shares compute."
  exit 0
fi

BOARD="${1:?usage: swarm/share_compute.sh <board-url> [--docker]   (or --stop)}"
BOARD="${BOARD%/}"
cd "$(dirname "$0")/.."
command -v tailscale >/dev/null || { echo "Install Tailscale and join the tailnet first."; exit 1; }

id swarm >/dev/null 2>&1 || { sudo useradd -m -s /bin/bash swarm && echo "Created user swarm."; }
for group in video render; do  # GPU device access on distros that restrict it
  getent group "$group" >/dev/null && sudo usermod -aG "$group" swarm
done
if [ "${2:-}" = "--docker" ] || [ "${2:-}" = "--admin" ]; then
  sudo usermod -aG docker swarm
  echo "warning: swarm can use Docker, which is root-equivalent on this machine."
fi
if [ "${2:-}" = "--admin" ]; then
  echo "swarm ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/swarm >/dev/null
  sudo chmod 440 /etc/sudoers.d/swarm
  sudo visudo -cf /etc/sudoers.d/swarm >/dev/null || { sudo rm -f /etc/sudoers.d/swarm; echo "sudoers check failed; not granted"; exit 1; }
  echo "warning: swarm has passwordless sudo here: agents can do anything on this machine."
fi
sudo tailscale set --ssh

host=$(tailscale status --json | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
gpus=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | paste -sd ';' - || true)
mem=$(free -g 2>/dev/null | awk '/^Mem:/ {print $2 " GB RAM"}')
cores=$(nproc 2>/dev/null || echo "?")
note="Shared compute: ssh swarm@$host · ${gpus:-no GPU} · $mem · $cores cores · arch $(uname -m). Run jobs in ~/jobs/<your-name>/, check nvidia-smi and #compute first, and say here what you're running and until when. Shared by $USER."
python3 scripts/board.py --url "$BOARD" --as "$USER" post compute "$note" --type decision >/dev/null \
  && echo "Announced in #compute." || echo "Couldn't post to the board; announce it in #compute yourself."
echo "Agents connect with: ssh swarm@$host"
echo "Stop sharing any time: swarm/share_compute.sh --stop"

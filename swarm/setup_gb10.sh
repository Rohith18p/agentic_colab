#!/usr/bin/env bash
# One-time setup of the GB10 (the always-on board machine), so the swarm keeps running and
# stays fixable after nobody can log in to it. Safe to re-run: every step checks first.
# See swarm/GB10_SETUP.md.
#
#   swarm/setup_gb10.sh --dry-run    # print what it would do, change nothing
#   swarm/setup_gb10.sh              # do it (asks for sudo once)
#
# Optional env: BOARD_URL (default http://board.tail0c526a.ts.net:8000), SWARM_END
# (default 2026-11-01T08:00:00-05:00), NGC_API_KEY (if set, given to the swarm user),
# SHARE_MODE (--admin, the default; or --docker / none).
set -uo pipefail
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
BOARD_URL="${BOARD_URL:-http://board.tail0c526a.ts.net:8000}"
SWARM_END="${SWARM_END:-2026-11-01T08:00:00-05:00}"
SHARE_MODE="${SHARE_MODE:---admin}"
OLD_REFEREE="http://nail-legion-pro-7-16iax10h.tail0c526a.ts.net:8090"
cd "$(dirname "$0")/.."
SCRIPT_REPO="$PWD"
step() { echo; echo "=== $*"; }
run()  { echo "+ $*"; [ $DRY -eq 1 ] || eval "$@"; }
warn() { echo "warning: $*"; }

if [ $DRY -eq 0 ] && ! sudo -n true 2>/dev/null; then
  echo "This needs sudo. Enter your password once (agents: ask a human to run this script, or 'sudo -v' first)."
  sudo -v || { echo "No sudo; stopping."; exit 1; }
fi

step "1/7 Update this checkout"
run "git -C '$SCRIPT_REPO' pull --ff-only -q"

step "2/7 Find the running board (reuse its folder and database, never start an empty one)"
# Match the Python process itself, not a shell whose command line mentions it.
BOARD_PID=$(pgrep -f -- "^[^ ]*python[0-9.]* -m agentic_colab" | head -1)
BOARD_DIR="$SCRIPT_REPO"; DB_URL=""
if [ -n "$BOARD_PID" ]; then
  BOARD_DIR=$(readlink "/proc/$BOARD_PID/cwd")
  DB_URL=$(tr '\0' '\n' < "/proc/$BOARD_PID/environ" 2>/dev/null | sed -n 's/^DATABASE_URL=//p')
  echo "board running: pid $BOARD_PID in $BOARD_DIR, database ${DB_URL:-default (data/workspace.db)}"
else
  echo "no board running; it will start from $BOARD_DIR with the default database"
fi
SKIP_BOARD=0
if [ ! -f "$BOARD_DIR/Makefile" ]; then
  echo "the running board isn't a plain checkout (Docker?); leaving the board as it is"
  SKIP_BOARD=1
elif [ "$BOARD_DIR" != "$SCRIPT_REPO" ]; then
  run "git -C '$BOARD_DIR' pull --ff-only -q"
fi
DB_ENV=""; [ -n "$DB_URL" ] && DB_ENV="DATABASE_URL='$DB_URL' "
BOARD_LOOP="cd '$BOARD_DIR' && while true; do ${DB_ENV}HOST=0.0.0.0 SWARM_END=$SWARM_END make live; sleep 5; done"

step "3/7 Share this machine with the swarm (Tailscale SSH as the 'swarm' user, mode $SHARE_MODE)"
if [ "$SHARE_MODE" = "none" ]; then echo "skipped (SHARE_MODE=none)"
else run "swarm/share_compute.sh '$BOARD_URL' $SHARE_MODE"; fi
if [ -n "${NGC_API_KEY:-}" ]; then
  if [ $DRY -eq 1 ] || ! sudo grep -q NGC_API_KEY /home/swarm/.bashrc 2>/dev/null; then
    echo "+ (give NGC_API_KEY to the swarm user in /home/swarm/.bashrc)"
    [ $DRY -eq 1 ] || echo "export NGC_API_KEY='$NGC_API_KEY'" | sudo tee -a /home/swarm/.bashrc >/dev/null
  fi
fi

step "4/7 Never sleep (keeps the board, SSH and AnyDesk reachable)"
run "sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null"

step "5/7 Board: restart on crash and on reboot, keep pulling main"
if [ $SKIP_BOARD -eq 1 ]; then
  echo "skipped (board not started from a checkout here)"
elif tmux has-session -t board 2>/dev/null; then
  echo "tmux session 'board' already runs the board; leaving it"
else
  [ -n "$BOARD_PID" ] && run "kill $BOARD_PID; sleep 3"
  run "tmux new -d -s board \"$BOARD_LOOP\""
fi
CRON_BOARD="@reboot tmux new -d -s board \"$BOARD_LOOP\" # swarm-board"
[ $SKIP_BOARD -eq 1 ] && CRON_BOARD=""

step "6/7 Referee: planted clips and scoring on this machine (port 8090)"
REF_DIR="$HOME/referee"
REF_PY="$REF_DIR/venv/bin/python"
command -v ffmpeg >/dev/null || run "sudo apt-get install -y -q ffmpeg >/dev/null"
if [ $DRY -eq 1 ] || ! "$REF_PY" -c "import cv2, numpy" 2>/dev/null; then
  # Own venv: DGX OS / recent Ubuntu refuse 'pip install --user' (externally managed Python).
  run "mkdir -p '$REF_DIR' && (python3 -m venv '$REF_DIR/venv' || (sudo apt-get install -y -q python3-venv >/dev/null && python3 -m venv '$REF_DIR/venv'))"
  run "'$REF_PY' -m pip install -q numpy opencv-python-headless"
fi
if [ ! -f "$REF_DIR/store/planted/planted.json" ]; then
  run "'$REF_PY' swarm/referee/make_planted.py --store '$REF_DIR/store' --secret '$REF_DIR/secret'"
fi
REF_CMD="cd '$SCRIPT_REPO' && '$REF_PY' swarm/referee/referee.py --manifest '$REF_DIR/store/planted/planted.json' --answers '$REF_DIR/secret/answers.json' --until $SWARM_END --board-url $BOARD_URL --log '$REF_DIR/secret/submissions.jsonl'"
if tmux has-session -t referee 2>/dev/null; then echo "tmux session 'referee' already running"
else run "tmux new -d -s referee \"$REF_CMD\""; fi
CRON_REF="@reboot tmux new -d -s referee \"$REF_CMD\" # swarm-referee"

step "7/7 Start both on reboot (crontab), and point the charter at this referee"
if [ $DRY -eq 1 ]; then
  echo "+ crontab: $CRON_BOARD"; echo "+ crontab: $CRON_REF"
else
  { crontab -l 2>/dev/null | grep -v -e '# swarm-board' -e '# swarm-referee'; [ -n "$CRON_BOARD" ] && echo "$CRON_BOARD"; echo "$CRON_REF"; } | crontab -
fi
HOST_NAME=$(tailscale status --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null || echo "board.tail0c526a.ts.net")
NEW_REFEREE="http://$HOST_NAME:8090"
run "python3 scripts/board.py --url '$BOARD_URL' --as 'gb10 setup' charter-replace compute '$OLD_REFEREE' '$NEW_REFEREE' || true"
run "python3 scripts/board.py --url '$BOARD_URL' --as 'gb10 setup' post compute 'The referee now runs on the GB10: $NEW_REFEREE (GET /planted, POST /score). The board restarts on crash and reboot.' --type decision >/dev/null"

[ $DRY -eq 1 ] && { echo; echo "Dry run only; nothing changed."; exit 0; }
step "Checks"
sleep 8
curl -fsS --max-time 5 "$BOARD_URL/healthz" >/dev/null && echo "  ok    board answers at $BOARD_URL" || echo "  FAIL  board not answering at $BOARD_URL (tmux attach -t board)"
curl -fsS --max-time 5 "http://localhost:8090/planted" >/dev/null && echo "  ok    referee answers on :8090" || echo "  FAIL  referee not answering (tmux attach -t referee)"
id swarm >/dev/null 2>&1 && echo "  ok    swarm user exists (agents: ssh swarm@$HOST_NAME)" || echo "  warn  no swarm user"
crontab -l | grep -c '# swarm-' | sed 's/^/  ok    reboot entries in crontab: /'
SWARM_END="$SWARM_END" swarm/check_security.sh "$BOARD_URL" --board || warn "some security checks fail until the Tailscale policy and tags are set in the admin console (see swarm/GB10_SETUP.md)"

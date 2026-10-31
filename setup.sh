#!/usr/bin/env bash
# =============================================================================
# ScienceClaw machine setup
#   - Cosmos3 Nano generator (NVIDIA NIM container, unmodified)
#   - Our worker container (Warp, OpenCV, ffmpeg, numpy, scipy... + our code)
#
# Host only needs: NVIDIA driver, Docker, NVIDIA Container Toolkit.
# Everything else (Warp, OpenCV, ...) is inside the worker image built below.
#
# Usage:
#   export NGC_API_KEY=...                      # secret: keep in env, not on the command line
#   ./setup.sh <machine_name> <board_url> [gpu_index] [cosmos_port]
#
# Examples:
#   ./setup.sh brev-1 http://1.2.3.4:9000                  # 1-GPU Brev box
#   ./setup.sh lab-pc-g1 http://1.2.3.4:9000 1 8001         # 2nd GPU on a multi-GPU machine
#   SKIP_COSMOS=1 ./setup.sh small-pc http://1.2.3.4:9000   # GPU too small for Cosmos: Warp/worker only
#
# Multi-GPU machine: run GPU 0 first and wait until it's ready (it downloads the
# weights into the shared cache), then run the other GPUs with their own ports.
#
# Optional: put extra worker settings (shared storage keys, etc.) in a file named
# .env.worker next to this script, one VAR=value per line. It's passed to the worker.
# =============================================================================
set -euo pipefail

USAGE="usage: ./setup.sh <machine_name> <board_url> [gpu_index] [cosmos_port]"
MACHINE_NAME="${1:?$USAGE}"
BOARD_URL="${2:?$USAGE}"
GPU="${3:-0}"
PORT="${4:-8000}"
SKIP_COSMOS="${SKIP_COSMOS:-0}"

NIM_IMAGE="nvcr.io/nim/nvidia/cosmos3:2.1.0"
WORKER_IMAGE="scienceclaw-worker:latest"
CACHE="$HOME/.cache/nim/cosmos3"
DATA_DIR="$HOME/scienceclaw_data"
COSMOS_NAME="cosmos3-gpu${GPU}"
WORKER_NAME="worker-gpu${GPU}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
READY_TIMEOUT_MIN=60
WARP_VERSION="1.19.0"

log() { echo -e "\n=== $* ==="; }
warn() { echo "WARNING: $*" >&2; }
die() { echo "ERROR: $*" >&2; exit 1; }

if [ "$SKIP_COSMOS" != "1" ]; then
  : "${NGC_API_KEY:?export NGC_API_KEY first (from ngc.nvidia.com -> Setup -> Generate API Key)}"
fi

# -----------------------------------------------------------------------------
log "1/8 Checking host"
command -v nvidia-smi >/dev/null || die "nvidia-smi not found (NVIDIA driver missing)"
command -v docker >/dev/null || die "docker not installed"
if ! docker info 2>/dev/null | grep -qi nvidia; then
  warn "NVIDIA runtime not listed in 'docker info'. If GPU containers fail, install the NVIDIA Container Toolkit."
fi
[ -f "$SCRIPT_DIR/Dockerfile.worker" ] || die "Dockerfile.worker not found next to setup.sh"

nvidia-smi --query-gpu=index,name,memory.total,memory.used,driver_version --format=csv
nvidia-smi -i "$GPU" >/dev/null 2>&1 || die "GPU index $GPU not found on this machine"

DRIVER_MAJOR=$(nvidia-smi -i "$GPU" --query-gpu=driver_version --format=csv,noheader | cut -d. -f1 | tr -d ' ')
RAM_GB=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
ARCH=$(uname -m)
echo "Machine: $MACHINE_NAME | GPU: $GPU | Driver: $DRIVER_MAJOR | Host RAM: ${RAM_GB} GB | Arch: $ARCH"

if [ "$SKIP_COSMOS" != "1" ] && [ "$RAM_GB" -lt 40 ]; then
  warn "Cosmos Nano generator wants ~35-40 GB of host RAM. This machine may fail; the preflight will tell."
fi

# -----------------------------------------------------------------------------
if [ "$SKIP_COSMOS" != "1" ]; then
  log "2/8 Checking port $PORT and GPU $GPU are free"
  BUSY=$(docker ps --format '{{.Names}} {{.Ports}}' | grep -E ":${PORT}->" | awk '{print $1}' | grep -vx "$COSMOS_NAME" || true)
  if [ -n "$BUSY" ]; then
    die "port $PORT is already used by container(s): $BUSY
       If that's an old Cosmos container, stop it first:  docker rm -f $BUSY"
  fi
  USED_MB=$(nvidia-smi -i "$GPU" --query-gpu=memory.used --format=csv,noheader,nounits | tr -d ' ')
  if [ "$USED_MB" -gt 4000 ] && ! docker ps --format '{{.Names}}' | grep -qx "$COSMOS_NAME"; then
    warn "GPU $GPU already has ${USED_MB} MiB in use (another Cosmos/old container?). Check 'docker ps' and 'nvidia-smi'."
    warn "Cosmos may fail to fit. Continuing in 10 s, Ctrl+C to stop."
    sleep 10
  fi

  # ---------------------------------------------------------------------------
  log "3/8 Logging in to NGC and pulling Cosmos image (big, first time takes a while)"
  echo "$NGC_API_KEY" | docker login nvcr.io --username '$oauthtoken' --password-stdin
  docker pull "$NIM_IMAGE"

  # ---------------------------------------------------------------------------
  log "4/8 Preflight: does Cosmos Nano generator fit GPU $GPU? (no model download)"
  if ! docker run --rm --gpus "device=${GPU}" \
      -e NIM_MODEL_TYPE=generator -e NIM_MODEL_VARIANT=nano -e NIM_PERF_PROFILE=latency \
      --entrypoint /bin/bash "$NIM_IMAGE" \
      -lc '/opt/nim/.venv/bin/python -m profile_selection.startup --output /tmp/preflight.env && echo "Preflight passed"'; then
    die "Cosmos Nano doesn't fit GPU $GPU. Rerun with SKIP_COSMOS=1 to use this machine for Warp/worker jobs only."
  fi

  # ---------------------------------------------------------------------------
  log "5/8 Starting Cosmos generator on GPU $GPU, port $PORT (first start downloads weights in the background)"
  mkdir -p "$CACHE"
  chmod -R a+rwX "$CACHE"
  docker rm -f "$COSMOS_NAME" >/dev/null 2>&1 || true
  docker run -d --name "$COSMOS_NAME" --restart unless-stopped \
    --gpus "device=${GPU}" \
    --shm-size 16g \
    --ulimit memlock=-1 --ulimit stack=67108864 --ulimit nofile=65536:65536 \
    -p "127.0.0.1:${PORT}:8000" \
    -e NGC_API_KEY \
    -e NIM_MODEL_TYPE=generator \
    -e NIM_MODEL_VARIANT=nano \
    -e NIM_PERF_PROFILE=latency \
    -v "$CACHE:/opt/nim/.cache" \
    "$NIM_IMAGE" >/dev/null
  echo "Cosmos container '$COSMOS_NAME' started. Follow it with: docker logs -f $COSMOS_NAME"
  COSMOS_URL="http://localhost:${PORT}"
else
  log "2-5/8 SKIP_COSMOS=1: no Cosmos on this machine (Warp/worker only)"
  COSMOS_URL=""
fi

# -----------------------------------------------------------------------------
log "6/8 Building worker image (Warp, OpenCV, ffmpeg, numpy, scipy, our code)"
# Default Warp wheel needs driver R580+. Older drivers get the CUDA 12 wheel,
# otherwise Warp silently falls back to CPU.
if [ "$DRIVER_MAJOR" -ge 580 ]; then
  WARP_SPEC="warp-lang==${WARP_VERSION}"
else
  if [ "$ARCH" = "aarch64" ]; then
    WHEEL_TAG="manylinux_2_34_aarch64"
  else
    WHEEL_TAG="manylinux_2_28_x86_64"
  fi
  WARP_SPEC="https://github.com/NVIDIA/warp/releases/download/v${WARP_VERSION}/warp_lang-${WARP_VERSION}+cu12-py3-none-${WHEEL_TAG}.whl"
  echo "Driver $DRIVER_MAJOR < 580 -> using CUDA 12 Warp wheel"
fi
docker build -t "$WORKER_IMAGE" \
  --build-arg WARP_SPEC="$WARP_SPEC" \
  -f "$SCRIPT_DIR/Dockerfile.worker" "$SCRIPT_DIR"

log "6b/8 Checking worker image: Warp sees the GPU, OpenCV and ffmpeg load"
docker run --rm --gpus "device=${GPU}" "$WORKER_IMAGE" python -c "
import warp as wp, cv2, numpy, scipy
wp.init()
n = wp.get_cuda_device_count()
assert n > 0, 'Warp sees NO GPU (would run on CPU, very slow). Check driver / Container Toolkit.'
print('Warp GPU OK |', 'OpenCV', cv2.__version__, '| numpy', numpy.__version__)
" || die "worker image check failed"
docker run --rm "$WORKER_IMAGE" ffmpeg -hide_banner -version | head -1

# -----------------------------------------------------------------------------
log "7/8 Starting worker"
mkdir -p "$DATA_DIR"
ENV_FILE_ARGS=()
if [ -f "$SCRIPT_DIR/.env.worker" ]; then
  ENV_FILE_ARGS=(--env-file "$SCRIPT_DIR/.env.worker")
  echo "Passing extra settings from .env.worker"
fi
docker rm -f "$WORKER_NAME" >/dev/null 2>&1 || true
docker run -d --name "$WORKER_NAME" --restart unless-stopped \
  --gpus "device=${GPU}" \
  --network host \
  --memory 24g --pids-limit 1024 \
  -e MACHINE_NAME="$MACHINE_NAME" \
  -e BOARD_URL="$BOARD_URL" \
  -e COSMOS_URL="$COSMOS_URL" \
  -e GPU_INDEX="$GPU" \
  ${ENV_FILE_ARGS[@]+"${ENV_FILE_ARGS[@]}"} \
  -v "$DATA_DIR:/data" \
  "$WORKER_IMAGE" >/dev/null
echo "Worker '$WORKER_NAME' started. Follow it with: docker logs -f $WORKER_NAME"

# -----------------------------------------------------------------------------
if [ "$SKIP_COSMOS" != "1" ]; then
  log "8/8 Waiting for Cosmos to be ready (first start can take a long time; timeout ${READY_TIMEOUT_MIN} min)"
  READY=0
  for _ in $(seq 1 $((READY_TIMEOUT_MIN * 6))); do
    if curl -fsS "${COSMOS_URL}/v1/health/ready" >/dev/null 2>&1; then
      READY=1
      break
    fi
    RESTARTS=$(docker inspect -f '{{.RestartCount}}' "$COSMOS_NAME" 2>/dev/null || echo 0)
    if [ "$RESTARTS" -ge 2 ]; then
      docker logs --tail 60 "$COSMOS_NAME"
      die "Cosmos container keeps crashing (restarted $RESTARTS times). See logs above."
    fi
    sleep 10
  done
  if [ "$READY" != "1" ]; then
    docker logs --tail 60 "$COSMOS_NAME"
    die "Cosmos not ready after ${READY_TIMEOUT_MIN} min. It may still be downloading: docker logs -f $COSMOS_NAME"
  fi

  META=$(curl -fsS "${COSMOS_URL}/v1/metadata")
  echo "$META" | python3 -m json.tool 2>/dev/null | head -40 || echo "$META"
  echo "$META" | grep -qi generator || warn "metadata doesn't say 'generator'. Check before sending jobs."
  echo "$META" | grep -qi nano || warn "metadata doesn't mention 'nano'. Check which variant loaded."
else
  log "8/8 Skipped (no Cosmos on this machine)"
fi

# -----------------------------------------------------------------------------
log "DONE: $MACHINE_NAME"
echo "  GPU:        $GPU"
echo "  Cosmos:     ${COSMOS_URL:-none}  (container: ${COSMOS_NAME})"
echo "  Worker:     $WORKER_NAME -> board $BOARD_URL"
echo "  Data dir:   $DATA_DIR (mounted at /data in the worker)"
echo
echo "Useful:"
echo "  docker logs -f $WORKER_NAME"
[ "$SKIP_COSMOS" != "1" ] && echo "  docker logs -f $COSMOS_NAME"
echo "  docker ps"
exit 0

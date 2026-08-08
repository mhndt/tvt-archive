#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$(readlink -f "$0")")"
GPU_MODE="auto"
BUILD_LOCAL=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --gpu) GPU_MODE="${2:?--gpu requires auto, dri, nvidia, or cpu}"; shift 2 ;;
    --gpu=*) GPU_MODE="${1#*=}"; shift ;;
    --build-local) BUILD_LOCAL=1; shift ;;
    -h|--help)
      echo "Usage: ./setup.sh [--gpu auto|dri|nvidia|cpu] [--build-local]"
      echo "Normal installs pull the published GHCR image. --build-local is for developers/tests."
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$GPU_MODE" in auto|dri|nvidia|cpu) ;; *) echo "Invalid GPU mode: $GPU_MODE" >&2; exit 2 ;; esac

command -v docker >/dev/null || { echo "ERROR: Docker is not installed." >&2; exit 1; }
docker compose version >/dev/null || { echo "ERROR: Docker Compose v2 is required." >&2; exit 1; }

MODE="$GPU_MODE"
if [[ "$MODE" == auto ]]; then
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    MODE=nvidia
  elif compgen -G '/dev/dri/renderD*' >/dev/null; then
    MODE=dri
  else
    MODE=cpu
  fi
fi

HOST_BIND="${TVT_ARCHIVE_HOST:-0.0.0.0}"
HOST_PORT="${TVT_ARCHIVE_PORT:-8099}"
IMAGE="${TVT_ARCHIVE_IMAGE:-ghcr.io/mhndt/tvt-archive:0.8.3}"
LOCAL_IMAGE="${TVT_ARCHIVE_LOCAL_IMAGE:-tvt-archive:0.8.3-local}"
TOKEN="${TVT_ARCHIVE_TOKEN:-}"
AUDIO_DELAY="${TVT_ARCHIVE_STREAM_AUDIO_DELAY_MS:-0}"
COMPOSE_FILES=(compose/compose.yaml)
ACCELERATOR="software"
RENDER_DEVICE="/dev/dri/renderD128"
RENDER_GID="109"
VIDEO_GID="44"

if (( BUILD_LOCAL )); then
  COMPOSE_FILES+=(compose/build-local.yaml)
fi
if [[ "$MODE" == dri ]]; then
  RENDER_DEVICE="$(find /dev/dri -maxdepth 1 -type c -name 'renderD*' 2>/dev/null | sort | head -n1 || true)"
  [[ -n "$RENDER_DEVICE" ]] || { echo "ERROR: No DRM render node was found under /dev/dri." >&2; exit 1; }
  RENDER_GID="$(stat -c '%g' "$RENDER_DEVICE")"
  CARD_DEVICE="$(find /dev/dri -maxdepth 1 -type c -name 'card*' 2>/dev/null | sort | head -n1 || true)"
  [[ -n "$CARD_DEVICE" ]] && VIDEO_GID="$(stat -c '%g' "$CARD_DEVICE")"
  COMPOSE_FILES+=(compose/intel-amd.yaml)
  ACCELERATOR="auto"
elif [[ "$MODE" == nvidia ]]; then
  command -v nvidia-smi >/dev/null 2>&1 || { echo "ERROR: nvidia-smi is unavailable." >&2; exit 1; }
  COMPOSE_FILES+=(compose/nvidia.yaml)
  ACCELERATOR="nvidia"
fi

COMPOSE_FILE_VALUE="$(IFS=:; echo "${COMPOSE_FILES[*]}")"
cat > .env <<ENV
COMPOSE_FILE=$COMPOSE_FILE_VALUE
TVT_ARCHIVE_HOST=$HOST_BIND
TVT_ARCHIVE_PORT=$HOST_PORT
TVT_ARCHIVE_IMAGE=$IMAGE
TVT_ARCHIVE_LOCAL_IMAGE=$LOCAL_IMAGE
TVT_ARCHIVE_TOKEN=$TOKEN
TVT_ARCHIVE_ACCELERATOR=$ACCELERATOR
TVT_ARCHIVE_DRI_DEVICE=$RENDER_DEVICE
TVT_ARCHIVE_VAAPI_DRIVER=auto
TVT_ARCHIVE_RENDER_GID=$RENDER_GID
TVT_ARCHIVE_VIDEO_GID=$VIDEO_GID
TVT_ARCHIVE_STREAM_AUDIO_DELAY_MS=$AUDIO_DELAY
ENV
chmod 0600 .env

echo "Selected acceleration setup: $MODE"
if (( BUILD_LOCAL )); then
  docker compose up -d --build
else
  docker compose pull
  docker compose up -d
fi

for _ in $(seq 1 120); do
  status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' tvt-archive 2>/dev/null || true)"
  [[ "$status" == healthy ]] && break
  [[ "$status" == exited || "$status" == dead ]] && { docker compose logs --tail=200 tvt-archive; exit 1; }
  sleep 2
done
[[ "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' tvt-archive 2>/dev/null || true)" == healthy ]] || { echo "ERROR: TVT Archive did not become healthy." >&2; docker compose logs --tail=200 tvt-archive; exit 1; }

TOKEN="$(docker compose exec -T tvt-archive /opt/tvt-archive/entrypoint.sh show-token)"
LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}')"
[[ -n "$LAN_IP" ]] || LAN_IP="<docker-host-ip>"

echo
echo "TVT Archive 0.8.3 is ready."
echo "Bridge URL: http://$LAN_IP:$HOST_PORT"
echo "Access token: $TOKEN"
echo "Retrieve it later: docker compose exec tvt-archive /opt/tvt-archive/entrypoint.sh show-token"
echo
docker compose exec -T tvt-archive /opt/tvt-archive/entrypoint.sh accelerator-info || true

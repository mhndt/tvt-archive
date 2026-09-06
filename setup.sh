#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$(readlink -f "$0")")"
VAAPI=0
BUILD_LOCAL=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --vaapi) VAAPI=1; shift ;;
    --build-local) BUILD_LOCAL=1; shift ;;
    -h|--help) echo "Usage: ./setup.sh [--vaapi] [--build-local]"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v docker >/dev/null || { echo "docker is not installed" >&2; exit 1; }
docker compose version >/dev/null || { echo "docker compose v2 is required" >&2; exit 1; }

COMPOSE_FILES=(compose/compose.yaml)
ENCODER=software
RENDER_DEVICE=/dev/dri/renderD128
RENDER_GID=109
VIDEO_GID=44
if (( BUILD_LOCAL )); then
  COMPOSE_FILES+=(compose/build-local.yaml)
fi
if (( VAAPI )); then
  RENDER_DEVICE="$(find /dev/dri -maxdepth 1 -type c -name 'renderD*' 2>/dev/null | sort | head -n1 || true)"
  [[ -n "$RENDER_DEVICE" ]] || { echo "no render node under /dev/dri" >&2; exit 1; }
  RENDER_GID="$(stat -c '%g' "$RENDER_DEVICE")"
  CARD="$(find /dev/dri -maxdepth 1 -type c -name 'card*' 2>/dev/null | sort | head -n1 || true)"
  [[ -n "$CARD" ]] && VIDEO_GID="$(stat -c '%g' "$CARD")"
  COMPOSE_FILES+=(compose/vaapi.yaml)
  ENCODER=vaapi
fi

COMPOSE_FILE_VALUE="$(IFS=:; echo "${COMPOSE_FILES[*]}")"
cat > .env <<ENV
COMPOSE_FILE=$COMPOSE_FILE_VALUE
TVT_ARCHIVE_HOST=${TVT_ARCHIVE_HOST:-0.0.0.0}
TVT_ARCHIVE_PORT=${TVT_ARCHIVE_PORT:-8099}
TVT_ARCHIVE_IMAGE=${TVT_ARCHIVE_IMAGE:-ghcr.io/mhndt/tvt-archive:0.9.1}
TVT_ARCHIVE_LOCAL_IMAGE=${TVT_ARCHIVE_LOCAL_IMAGE:-tvt-archive:0.9.1-local}
TVT_ARCHIVE_TOKEN=${TVT_ARCHIVE_TOKEN:-}
TVT_ARCHIVE_ENCODER=$ENCODER
TVT_ARCHIVE_DRI_DEVICE=$RENDER_DEVICE
TVT_ARCHIVE_RENDER_GID=$RENDER_GID
TVT_ARCHIVE_VIDEO_GID=$VIDEO_GID
TVT_ARCHIVE_STREAM_AUDIO_DELAY_MS=${TVT_ARCHIVE_STREAM_AUDIO_DELAY_MS:-0}
ENV
chmod 0600 .env

if (( BUILD_LOCAL )); then
  docker compose up -d --build
else
  docker compose pull
  docker compose up -d
fi

status=""
for _ in $(seq 1 120); do
  status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' tvt-archive 2>/dev/null || true)"
  [[ "$status" == healthy ]] && break
  [[ "$status" == exited || "$status" == dead ]] && { docker compose logs --tail=200 tvt-archive; exit 1; }
  sleep 2
done
[[ "$status" == healthy ]] || { echo "tvt-archive did not become healthy" >&2; docker compose logs --tail=200 tvt-archive; exit 1; }

LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}')"
echo "Bridge URL: http://${LAN_IP:-<docker-host-ip>}:${TVT_ARCHIVE_PORT:-8099}"
echo "Access token: $(docker compose exec -T tvt-archive tvt-archive show-token)"

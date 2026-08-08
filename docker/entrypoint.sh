#!/usr/bin/env bash
set -Eeuo pipefail

CONFIG_DIRECTORY="${TVT_ARCHIVE_CONFIG_DIRECTORY:-${CREDENTIALS_DIRECTORY:-/config}}"
CONFIG_PATH="$CONFIG_DIRECTORY/config.json"

ensure_config() {
  mkdir -p "$(dirname "$CONFIG_PATH")"
  if [[ ! -f "$CONFIG_PATH" ]]; then
    TVT_ARCHIVE_TOKEN="${TVT_ARCHIVE_TOKEN:-}" python3 - "$CONFIG_PATH" <<'PY'
import json, os, secrets, sys
path = sys.argv[1]
token = os.environ.get("TVT_ARCHIVE_TOKEN") or secrets.token_urlsafe(32)
config = {
    "server": {"bind": "0.0.0.0", "port": 8099, "token": token},
    "processing": {
        "cache_hours": 6,
        "default_gain_db": 0,
        "playback_max_seconds": 900,
        "download_max_seconds": 3600,
        "availability_days": 45,
        "max_parallel_jobs": 1,
        "max_parallel_playback_sessions": 2,
        "hls_segment_seconds": 1,
        "hls_start_buffer_seconds": 2,
        "hls_timing_sample_frames": 8,
        "hls_first_media_timeout_seconds": 30.0,
        "hls_first_media_retries": 1,
        "hls_timing_max_seconds": 2.5,
        "hls_audio_detect_seconds": 1.0,
        "hls_idle_seconds": 300,
        "hls_retain_seconds": 1800,
        "accelerator": os.environ.get("TVT_ARCHIVE_ACCELERATOR", "auto"),
        "vaapi_driver": os.environ.get("TVT_ARCHIVE_VAAPI_DRIVER", "auto"),
        "dri_device": os.environ.get("TVT_ARCHIVE_DRI_DEVICE", "/dev/dri/renderD128"),
        "stream_audio_delay_ms": int(os.environ.get("TVT_ARCHIVE_STREAM_AUDIO_DELAY_MS", "0")),
    },
    "cameras": [],
}
with open(path, "x", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2)
    handle.write("\n")
os.chmod(path, 0o600)
print("============================================================")
print("TVT Archive created a new bridge configuration.")
print(f"Access token: {token}")
print("Save this token for Home Assistant.")
print("Retrieve it later with:")
print("docker compose exec tvt-archive /opt/tvt-archive/entrypoint.sh show-token")
print("============================================================")
print("Add the TVT Archive integration in Home Assistant, then manage cameras from its Configure menu.")
PY
  else
    python3 - "$CONFIG_PATH" <<'PY'
import json, os, re, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
processing = data.setdefault("processing", {})
changed = False
for key, value in {
    "accelerator": os.environ.get("TVT_ARCHIVE_ACCELERATOR", processing.get("accelerator", "auto")),
    "vaapi_driver": os.environ.get("TVT_ARCHIVE_VAAPI_DRIVER", processing.get("vaapi_driver", "auto")),
    "dri_device": os.environ.get("TVT_ARCHIVE_DRI_DEVICE", processing.get("dri_device", processing.get("qsv_device", "/dev/dri/renderD128"))),
    "stream_audio_delay_ms": int(os.environ.get("TVT_ARCHIVE_STREAM_AUDIO_DELAY_MS", str(processing.get("stream_audio_delay_ms", 0)))),
}.items():
    if processing.get(key) != value:
        processing[key] = value
        changed = True
if "qsv_device" in processing:
    processing.pop("qsv_device", None)
    changed = True
if processing.get("hls_start_buffer_seconds") != 2:
    processing["hls_start_buffer_seconds"] = 2
    changed = True
if processing.get("hls_timing_sample_frames") != 8:
    processing["hls_timing_sample_frames"] = 8
    changed = True
for key, value in {
    "hls_first_media_timeout_seconds": 30.0,
    "hls_first_media_retries": 1,
    "hls_timing_max_seconds": 2.5,
    "hls_audio_detect_seconds": 1.0,
}.items():
    if processing.get(key) != value:
        processing[key] = value
        changed = True

def slug(value):
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value)).strip("_").lower()
    return value[:48] or "live"

for camera in data.get("cameras", []):
    defaults = {
        "archive_backend": "native_9008",
        "port": 9008,
        "rtsp_port": 554,
        "rtsp_stream_type": "main",
        "rtsp_transport": "tcp",
        "rtsp_fps": 25.0,
    }
    for key, value in defaults.items():
        if key not in camera:
            camera[key] = value
            changed = True
    profiles = camera.get("live_profiles")
    if not isinstance(profiles, list):
        streams = camera.get("live_streams", {})
        profiles = []
        if isinstance(streams, dict):
            for key, name in (("high", "High"), ("balanced", "Balanced"), ("data_saver", "Data Saver")):
                entity = streams.get(key)
                if entity:
                    profiles.append({"id": key, "name": name, "entity_id": entity, "default": key == "high"})
        camera["live_profiles"] = profiles
        changed = True
    seen = set()
    default_seen = False
    cleaned = []
    for index, raw in enumerate(profiles):
        if not isinstance(raw, dict) or not raw.get("entity_id"):
            changed = True
            continue
        profile_id = str(raw.get("id") or slug(raw.get("name") or f"live_{index + 1}"))
        if profile_id in seen:
            profile_id = f"live_{index + 1}"
            changed = True
        seen.add(profile_id)
        is_default = bool(raw.get("default")) and not default_seen
        default_seen = default_seen or is_default
        item = {
            "id": profile_id,
            "name": str(raw.get("name") or profile_id),
            "entity_id": str(raw.get("entity_id")),
            "default": is_default,
        }
        if item != raw:
            changed = True
        cleaned.append(item)
    if cleaned and not default_seen:
        cleaned[0]["default"] = True
        changed = True
    if camera.get("live_profiles") != cleaned:
        camera["live_profiles"] = cleaned
        changed = True
    if "live_streams" in camera:
        camera.pop("live_streams", None)
        changed = True
if changed:
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
PY
  fi
}

show_token() {
  python3 - "$CONFIG_PATH" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle)["server"]["token"])
PY
}

accelerator_info() {
  python3 - <<'PY'
import json, urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:8099/api/health", timeout=5) as response:
        data = json.load(response)
    accelerator = data.get("accelerator", {})
    print("Playback accelerator:", accelerator.get("selected", "unknown"))
    print("Available encoders:", ", ".join(accelerator.get("available", [])) or "software/original only")
except Exception as error:
    print("Accelerator information unavailable:", error)
PY
}

ensure_config
case "${1:-run}" in
  show-token) show_token ;;
  accelerator-info) accelerator_info ;;
  run) exec /usr/bin/python3 /opt/tvt-archive/app/bridge.py ;;
  *) exec "$@" ;;
esac

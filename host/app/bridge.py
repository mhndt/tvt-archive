#!/usr/bin/env python3
from __future__ import annotations

import collections
import concurrent.futures
import dataclasses
import datetime as dt
import errno
import functools
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from native9008 import (
    KIND_PLAYBACK_CONTINUE,
    KIND_PLAYBACK_START,
    KIND_PLAYBACK_START_RESPONSE,
    KIND_RECORDED_MEDIA,
    MEDIA_HEADER_SIZE,
    TVT9008Client,
)

APP_VERSION = "0.8.4"
BASE = Path(os.environ.get("TVT_ARCHIVE_BASE", "/opt/tvt-archive"))
CONFIG_DIRECTORY = Path(
    os.environ.get(
        "TVT_ARCHIVE_CONFIG_DIRECTORY",
        os.environ.get("CREDENTIALS_DIRECTORY", "/etc/tvt-archive"),
    )
)
CONFIG_PATH = CONFIG_DIRECTORY / "config.json"
STATE = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/tvt-archive"))
CACHE = Path(os.environ.get("CACHE_DIRECTORY", "/var/cache/tvt-archive"))
WORK = STATE / "work"
INDEX = STATE / "index"
LOGS = STATE / "logs"
CAPTURE_HELPER = BASE / "app" / "archive_capture.py"
HLS_JS_PATH = BASE / "static" / "hls.min.js"
OPTIONS_PATH = Path("/data/options.json")


def drop_privileges(uid: int = 10001, gid: int = 10001) -> None:
    """When started as root (Home Assistant add-on), own the data and become uid 10001."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    for directory in (CONFIG_DIRECTORY, STATE, CACHE):
        directory.mkdir(parents=True, exist_ok=True)
        os.chown(directory, uid, gid)
        for root, dirs, files in os.walk(directory):
            for name in dirs + files:
                os.chown(os.path.join(root, name), uid, gid)
    groups = set()
    for device in (
        os.environ.get("TVT_ARCHIVE_DRI_DEVICE", "/dev/dri/renderD128"),
        "/dev/dri/card0",
    ):
        try:
            groups.add(os.stat(device).st_gid)
        except OSError:
            pass
    os.setgroups(sorted(groups))
    os.setgid(gid)
    os.setuid(uid)


for directory in (CACHE, WORK, INDEX, LOGS):
    directory.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s %(message)s"
)
LOG = logging.getLogger("tvt-archive")

CAMERA_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def validated_camera_id(value: Any) -> str:
    camera_id = str(value or "")
    if not CAMERA_ID_RE.fullmatch(camera_id):
        raise ValueError("Camera ID must contain only letters, numbers, underscores, or hyphens")
    return camera_id


def camera_index_directory(camera_id: str) -> Path:
    return INDEX / validated_camera_id(camera_id)


def json_dump_atomic(path: Path, value: Any, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    if mode is not None:
        os.chmod(temporary, mode)
    os.replace(temporary, path)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        token = os.environ.get("TVT_ARCHIVE_TOKEN") or secrets.token_urlsafe(32)
        config = {
            "server": {"bind": "0.0.0.0", "port": 8099, "token": token},
            "processing": {},
            "cameras": [],
        }
        json_dump_atomic(path, config, mode=0o600)
        LOG.info("Created %s", path)
        return config
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    processing = config.setdefault("processing", {})
    old = processing.pop("accelerator", None)
    if old is not None and "encoder" not in processing:
        processing["encoder"] = "vaapi" if "vaapi" in str(old) or "qsv" in str(old) else "software"
    for key in ("vaapi_driver", "qsv_device", "hls_audio_detect_seconds"):
        processing.pop(key, None)
    for item in config.get("cameras", []):
        if isinstance(item, dict):
            for key in (
                "archive_backend",
                "channel",
                "rtsp_port",
                "rtsp_stream_type",
                "rtsp_transport",
                "rtsp_fps",
            ):
                item.pop(key, None)
    return config


CONFIG_LOCK = threading.RLock()
CAPABILITIES_LOCK = threading.RLock()
CONFIG = load_config(CONFIG_PATH)

SERVER = CONFIG.get("server", {})
PROCESSING = CONFIG.get("processing", {})
OPTIONS: dict[str, Any] = {}
if OPTIONS_PATH.is_file():
    OPTIONS = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
_configured_cameras = CONFIG.get("cameras", [])
if not isinstance(_configured_cameras, list):
    raise ValueError("Configured cameras must be a list")
CAMERAS: dict[str, dict[str, Any]] = {}
for _camera_item in _configured_cameras:
    if not isinstance(_camera_item, dict):
        raise ValueError("Every configured camera must be an object")
    _camera_id = validated_camera_id(_camera_item.get("id"))
    if _camera_id in CAMERAS:
        raise ValueError(f"Duplicate configured camera ID: {_camera_id}")
    CAMERAS[_camera_id] = dict(_camera_item)

TOKEN = str(SERVER["token"])
BIND = str(SERVER.get("bind", "0.0.0.0"))
PORT = int(SERVER.get("port", 8099))
CACHE_HOURS = int(PROCESSING.get("cache_hours", 6))
DEFAULT_GAIN = int(PROCESSING.get("default_gain_db", 0))
PLAYBACK_MAX_SECONDS = int(PROCESSING.get("playback_max_seconds", 900))
DOWNLOAD_MAX_SECONDS = int(PROCESSING.get("download_max_seconds", 3600))
ENCODER = str(
    os.environ.get(
        "TVT_ARCHIVE_ENCODER", OPTIONS.get("encoder", PROCESSING.get("encoder", "software"))
    )
).lower()
if ENCODER not in ("software", "vaapi"):
    raise ValueError("encoder must be software or vaapi")
DRI_DEVICE = str(
    os.environ.get("TVT_ARCHIVE_DRI_DEVICE", PROCESSING.get("dri_device", "/dev/dri/renderD128"))
)
STREAM_AUDIO_DELAY_MS = int(
    os.environ.get("TVT_ARCHIVE_STREAM_AUDIO_DELAY_MS", PROCESSING.get("stream_audio_delay_ms", 0))
)
MAX_WORKERS = max(1, min(int(PROCESSING.get("max_parallel_jobs", 1)), 4))
NATIVE_SESSION_LIMIT = max(1, min(int(PROCESSING.get("max_native_sessions_per_camera", 1)), 4))

CAMERA_LOCKS: dict[str, threading.Lock] = {camera_id: threading.Lock() for camera_id in CAMERAS}
CAMERA_SESSION_SLOTS: dict[str, threading.BoundedSemaphore] = {
    camera_id: threading.BoundedSemaphore(NATIVE_SESSION_LIMIT) for camera_id in CAMERAS
}
METADATA_LOCKS: dict[str, threading.Lock] = {camera_id: threading.Lock() for camera_id in CAMERAS}
JOBS_LOCK = threading.Lock()
JOBS: dict[str, Job] = {}
CACHE_TO_JOB: dict[str, str] = {}
EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=MAX_WORKERS, thread_name_prefix="camera-job"
)
PLAYBACK_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=max(1, min(int(PROCESSING.get("max_parallel_playback_sessions", 2)), 4)),
    thread_name_prefix="playback-session",
)
SESSIONS_LOCK = threading.RLock()
SESSIONS: dict[str, PlaybackSession] = {}
HLS_IDLE_SECONDS = max(60, int(PROCESSING.get("hls_idle_seconds", 300)))
HLS_RETAIN_SECONDS = max(300, int(PROCESSING.get("hls_retain_seconds", 1800)))
HLS_SEGMENT_SECONDS = max(1, min(int(PROCESSING.get("hls_segment_seconds", 1)), 6))
COPY_MAX_GOP_SECONDS = 2.0 * HLS_SEGMENT_SECONDS
QUALITIES = ("original", "low")
HLS_START_BUFFER_SECONDS = max(
    2,
    min(
        int(
            os.environ.get(
                "TVT_ARCHIVE_HLS_START_BUFFER_SECONDS",
                PROCESSING.get("hls_start_buffer_seconds", 2),
            )
        ),
        30,
    ),
)
HLS_TIMING_SAMPLE_FRAMES = max(
    8,
    min(
        int(
            os.environ.get(
                "TVT_ARCHIVE_HLS_TIMING_SAMPLE_FRAMES",
                PROCESSING.get("hls_timing_sample_frames", 8),
            )
        ),
        120,
    ),
)
HLS_FIRST_MEDIA_TIMEOUT_SECONDS = max(
    5.0,
    min(
        float(
            os.environ.get(
                "TVT_ARCHIVE_HLS_FIRST_MEDIA_TIMEOUT_SECONDS",
                PROCESSING.get("hls_first_media_timeout_seconds", 30.0),
            )
        ),
        90.0,
    ),
)
HLS_FIRST_MEDIA_RETRIES = max(
    0,
    min(
        int(
            os.environ.get(
                "TVT_ARCHIVE_HLS_FIRST_MEDIA_RETRIES",
                PROCESSING.get("hls_first_media_retries", 1),
            )
        ),
        3,
    ),
)
HLS_TIMING_MAX_SECONDS = max(0.75, min(float(PROCESSING.get("hls_timing_max_seconds", 2.5)), 10.0))
HLS_AUDIO_PROBE_MEDIA_SECONDS = max(
    0.25,
    min(float(PROCESSING.get("hls_audio_probe_media_seconds", HLS_START_BUFFER_SECONDS)), 10.0),
)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}$")


@dataclasses.dataclass
class Job:
    id: str
    camera_id: str
    cache_key: str
    request: dict[str, Any]
    status: str = "queued"
    phase: str = "Waiting for the camera"
    created_at: float = dataclasses.field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    output_path: str | None = None
    output_name: str | None = None
    error: str | None = None
    video: str = "copy"
    video_frames: int = 0
    audio_frames: int = 0
    progress: float = 0.0
    captured_seconds: float = 0.0
    processed_seconds: float = 0.0
    remaining_seconds: float = 0.0

    def public(self) -> dict[str, Any]:
        now = time.time()
        elapsed = 0.0 if self.started_at is None else (self.finished_at or now) - self.started_at
        progress = 1.0 if self.status == "ready" else max(0.0, min(0.99, self.progress))
        return {
            "id": self.id,
            "camera_id": self.camera_id,
            "status": self.status,
            "phase": self.phase,
            "created_at": dt.datetime.fromtimestamp(self.created_at).isoformat(timespec="seconds"),
            "created_at_unix": int(self.created_at),
            "elapsed_seconds": round(elapsed, 1),
            "progress": round(progress, 4),
            "progress_percent": int(round(progress * 100)),
            "captured_seconds": round(self.captured_seconds, 2),
            "processed_seconds": round(self.processed_seconds, 2),
            "remaining_seconds": round(max(0.0, self.remaining_seconds), 1),
            "request": self.request,
            "ready": self.status == "ready",
            "error": self.error,
            "video": self.video,
            "video_frames": self.video_frames,
            "audio_frames": self.audio_frames,
            "filename": self.output_name,
        }


@dataclasses.dataclass
class PlaybackSession:
    id: str
    camera_id: str
    request: dict[str, Any]
    work_directory: str
    status: str = "queued"
    phase: str = "Waiting for the camera"
    created_at: float = dataclasses.field(default_factory=time.time)
    last_access: float = dataclasses.field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    video: str = "copy"
    stop_event: threading.Event = dataclasses.field(default_factory=threading.Event, repr=False)
    capture_process: subprocess.Popen[bytes] | None = dataclasses.field(default=None, repr=False)
    ffmpeg_process: subprocess.Popen[bytes] | None = dataclasses.field(default=None, repr=False)

    @property
    def directory(self) -> Path:
        return Path(self.work_directory)

    @property
    def playlist_path(self) -> Path:
        return self.directory / "index.m3u8"

    source_fps: float = 25.0
    audio_offset_ms: int = 0
    has_audio: bool = True
    audio_alignment_in_feeder: bool = False
    playlist_announced: bool = False

    def segment_count(self) -> int:
        try:
            return sum(1 for _ in self.directory.glob("segment-*.m4s"))
        except OSError:
            return 0

    def buffered_seconds(self) -> float:
        try:
            text = self.playlist_path.read_text(encoding="utf-8")
        except OSError:
            return 0.0
        total = 0.0
        for match in re.finditer(r"^#EXTINF:([0-9.]+)", text, re.MULTILINE):
            try:
                total += float(match.group(1))
            except ValueError:
                continue
        return total

    def playlist_ready(self) -> bool:
        # Stay ready once announced; a transient stat race must not tear down the player.
        if self.playlist_announced:
            return True
        try:
            if self.playlist_path.stat().st_size <= 40:
                return False
            count = self.segment_count()
            buffered = self.buffered_seconds()
            ready = buffered >= HLS_START_BUFFER_SECONDS or (
                self.status == "complete" and count > 0
            )
            if ready:
                self.playlist_announced = True
            return ready
        except OSError:
            return False

    def public(self, *, touch: bool = True) -> dict[str, Any]:
        if touch:
            self.last_access = time.time()
        now = time.time()
        elapsed = 0.0 if self.started_at is None else (self.finished_at or now) - self.started_at
        return {
            "id": self.id,
            "camera_id": self.camera_id,
            "status": self.status,
            "phase": self.phase,
            "created_at": dt.datetime.fromtimestamp(self.created_at).isoformat(timespec="seconds"),
            "created_at_unix": int(self.created_at),
            "elapsed_seconds": round(elapsed, 1),
            "request": self.request,
            "playlist_ready": self.playlist_ready(),
            "complete": self.status == "complete",
            "error": self.error,
            "video": self.video,
            "segment_seconds": HLS_SEGMENT_SECONDS,
            "segment_count": self.segment_count(),
            "buffered_seconds": round(self.buffered_seconds(), 3),
            "start_buffer_seconds": HLS_START_BUFFER_SECONDS,
            "source_fps": round(self.source_fps, 4),
            "audio_offset_ms": self.audio_offset_ms,
            "has_audio": self.has_audio,
            "mime_type": (
                'video/mp4; codecs="avc1.640029, mp4a.40.2"'
                if self.has_audio
                else 'video/mp4; codecs="avc1.640029"'
            ),
        }


# Frame streams: the camera sends recordings in bags of 100 video frames and waits for
# 0x090A before the next one. The bridge forwards frames as they come and asks for the
# next bag only while the viewer is close behind, so the camera never runs ahead of
# what is being watched. Records on the wire:
#   "TF" kind flags pts_us length payload   (<2sBBQI, little endian)
FRAME_MAGIC = b"TF"
FRAME_HEADER = struct.Struct("<2sBBQI")
REC_INFO, REC_VIDEO, REC_AUDIO, REC_MARK, REC_END = 0, 1, 2, 3, 4
KEYFRAME_FLAG = 1
BAG_END, END_EVENT = 2, 3
STREAM_WINDOW_FRAMES = 150
STREAM_IDLE_SECONDS = 20
STREAM_PAUSE_LIMIT_SECONDS = 900
STREAM_RETAIN_SECONDS = 300
FIRST_REQUEST_ID = 11


def frame_record(kind: int, flags: int, pts_us: int, payload: bytes = b"") -> bytes:
    return FRAME_HEADER.pack(FRAME_MAGIC, kind, flags, pts_us, len(payload)) + payload


def should_release_bag(bag_pending: bool, delivered: int, rendered: int, reader: bool) -> bool:
    return bag_pending and reader and delivered - rendered < STREAM_WINDOW_FRAMES


def split_access_units(buffer: bytearray) -> list[bytes]:
    """Cut complete access units off the front of an Annex B stream with AUD NAL units."""
    units: list[bytes] = []
    while True:
        first = _find_aud(buffer, 0)
        if first < 0:
            return units
        if first:
            del buffer[:first]
        following = _find_aud(buffer, 4)
        if following < 0:
            return units
        units.append(bytes(buffer[:following]))
        del buffer[:following]


def _find_aud(buffer: bytearray, offset: int) -> int:
    index = buffer.find(b"\x00\x00\x01\x09", offset)
    if index < 0:
        return -1
    return index - 1 if index and buffer[index - 1] == 0 else index


def access_unit_is_keyframe(unit: bytes) -> bool:
    for match in re.finditer(rb"\x00\x00\x01([\x00-\xff])", unit):
        if match.group(1)[0] & 0x1F in (5, 7):
            return True
    return False


@dataclasses.dataclass
class FrameSession:
    id: str
    camera_id: str
    request: dict[str, Any]
    status: str = "queued"
    phase: str = "Waiting for the camera"
    created_at: float = dataclasses.field(default_factory=time.time)
    last_access: float = dataclasses.field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    video: str = "copy"
    width: int = 0
    height: int = 0
    has_audio: bool = False
    position_us: int = 0
    delivered: int = 0
    rendered: int = 0
    generation: int = 0
    bags: int = 0
    bag_pending: bool = False
    media_us: int = 0
    first_us: int = 0
    wall_start: float = 0.0
    pump_started: float = 0.0
    readers: int = 0
    reader_left: float = 0.0
    last_progress: float = 0.0
    utc_offset: int | None = None
    seek_to: dt.datetime | None = None
    request_id: int = FIRST_REQUEST_ID
    pending_request_id: int | None = None
    stop_event: threading.Event = dataclasses.field(default_factory=threading.Event, repr=False)
    ready: threading.Condition = dataclasses.field(default_factory=threading.Condition, repr=False)
    queue: collections.deque = dataclasses.field(default_factory=collections.deque, repr=False)
    client: TVT9008Client | None = dataclasses.field(default=None, repr=False)

    def reader_attached(self) -> bool:
        return self.readers > 0

    def idle_seconds(self) -> float:
        if self.readers:
            return 0.0
        return time.monotonic() - (self.reader_left or self.pump_started)

    def push(self, kind: int, record: bytes) -> None:
        with self.ready:
            self.queue.append((kind, record))
            self.ready.notify_all()

    def finished(self) -> bool:
        return self.status in ("complete", "stopped", "error")

    def speed(self) -> float | None:
        wall = time.monotonic() - self.wall_start if self.wall_start else 0.0
        if wall < 3.0 or not self.media_us:
            return None
        return round(self.media_us / 1_000_000 / wall, 2)

    def public(self, *, touch: bool = True) -> dict[str, Any]:
        if touch:
            self.last_access = time.time()
        position = None
        if self.position_us:
            position = dt.datetime.fromtimestamp(self.position_us / 1_000_000).isoformat(
                timespec="seconds"
            )
        return {
            "id": self.id,
            "camera_id": self.camera_id,
            "status": self.status,
            "phase": self.phase,
            "error": self.error,
            "request": self.request,
            "created_at_unix": int(self.created_at),
            "frames_ready": self.width > 0,
            "width": self.width,
            "height": self.height,
            "has_audio": self.has_audio,
            "video": self.video,
            "position": position,
            "generation": self.generation,
            "delivered_frames": self.delivered,
            "rendered_frames": self.rendered,
            "bags": self.bags,
            "speed": self.speed(),
            "complete": self.status == "complete",
        }


def camera(camera_id: str) -> dict[str, Any]:
    with CONFIG_LOCK:
        if camera_id not in CAMERAS:
            raise KeyError(f"Unknown camera: {camera_id}")
        return dict(CAMERAS[camera_id])


RECORDING_AUDIO_MODES = {"auto", "on", "off"}
ALAW_SAMPLE_RATE = 8000
ALAW_SILENCE_BYTE = b"\xd5"


def recording_audio_mode(camera_id: str) -> str:
    mode = str(camera(camera_id).get("recording_audio", "auto")).strip().lower()
    if mode not in RECORDING_AUDIO_MODES:
        raise ValueError("Recording audio mode must be auto, on, or off")
    return mode


def _camera_capabilities_path(camera_id: str) -> Path:
    return camera_index_directory(camera_id) / "capabilities.json"


def _read_camera_capabilities(camera_id: str) -> dict[str, Any]:
    try:
        value = json.loads(_camera_capabilities_path(camera_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _learned_archive_audio(camera_id: str) -> bool:
    return bool(_read_camera_capabilities(camera_id).get("recording_audio", False))


def _remember_capability(camera_id: str, key: str, value: Any) -> None:
    with CAPABILITIES_LOCK:
        current = _read_camera_capabilities(camera_id)
        if current.get(key) == value:
            return
        current[key] = value
        json_dump_atomic(_camera_capabilities_path(camera_id), current, mode=0o600)
        LOG.info("Camera %s: %s=%s", camera_id, key, value)


def _remember_archive_audio(camera_id: str) -> None:
    _remember_capability(camera_id, "recording_audio", True)


def camera_lock(camera_id: str) -> threading.Lock:
    with CONFIG_LOCK:
        if camera_id not in CAMERA_LOCKS:
            raise KeyError(f"Unknown camera: {camera_id}")
        return CAMERA_LOCKS[camera_id]


def camera_session_slot(camera_id: str) -> threading.BoundedSemaphore:
    with CONFIG_LOCK:
        if camera_id not in CAMERA_SESSION_SLOTS:
            raise KeyError(f"Unknown camera: {camera_id}")
        return CAMERA_SESSION_SLOTS[camera_id]


def metadata_lock(camera_id: str) -> threading.Lock:
    with CONFIG_LOCK:
        if camera_id not in METADATA_LOCKS:
            raise KeyError(f"Unknown camera: {camera_id}")
        return METADATA_LOCKS[camera_id]


def safe_camera(camera_id: str) -> dict[str, Any]:
    item = camera(camera_id)
    return {
        "id": camera_id,
        "name": str(item.get("name", camera_id)),
        "host": str(item["host"]),
        "port": int(item.get("port", 9008)),
        "username": str(item.get("username", "")),
        "recording_audio": recording_audio_mode(camera_id),
    }


def list_cameras() -> list[dict[str, Any]]:
    with CONFIG_LOCK:
        camera_ids = list(CAMERAS)
    return [safe_camera(camera_id) for camera_id in camera_ids]


def camera_slug(name: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_").lower()
    return value[:64] or "camera"


def next_camera_id(name: str) -> str:
    base = camera_slug(name)
    with CONFIG_LOCK:
        if base not in CAMERAS:
            return base
        for number in range(2, 10000):
            candidate = f"{base[:58]}_{number}"
            if candidate not in CAMERAS:
                return candidate
    raise ValueError("Could not generate a unique camera ID")


def normalize_host(value: Any) -> str:
    host = str(value or "").strip()
    if not host or len(host) > 253 or any(char.isspace() for char in host):
        raise ValueError("Camera host is required and must not contain spaces")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if not re.fullmatch(
        r"(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
        host,
    ):
        raise ValueError("Camera host must be an IP address or valid hostname")
    return host


def normalize_camera_definition(
    payload: dict[str, Any], existing: dict[str, Any] | None = None, *, fixed_id: str | None = None
) -> dict[str, Any]:
    existing = dict(existing or {})
    name = str(payload.get("name", existing.get("name", ""))).strip()
    if not name or len(name) > 80:
        raise ValueError("Camera name is required and must be 80 characters or fewer")
    camera_id = fixed_id or str(payload.get("id") or existing.get("id") or next_camera_id(name))
    if not CAMERA_ID_RE.fullmatch(camera_id):
        raise ValueError("Camera ID may contain only letters, numbers, underscores, and hyphens")
    recording_audio = (
        str(payload.get("recording_audio", existing.get("recording_audio", "auto"))).strip().lower()
    )
    if recording_audio not in RECORDING_AUDIO_MODES:
        raise ValueError("Recording audio mode must be auto, on, or off")
    port = int(payload.get("port", existing.get("port", 9008)))
    if not 1 <= port <= 65535:
        raise ValueError("Camera port must be between 1 and 65535")
    username = str(payload.get("username", existing.get("username", ""))).strip()
    supplied_password = payload.get("password")
    password = (
        str(supplied_password)
        if supplied_password not in (None, "")
        else str(existing.get("password", ""))
    )
    if not username or not password:
        raise ValueError("Camera username and password are required")
    return {
        "id": camera_id,
        "name": name,
        "host": normalize_host(payload.get("host", existing.get("host"))),
        "port": port,
        "username": username,
        "password": password,
        "recording_audio": recording_audio,
    }


def capture_environment(item: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "TVT_HOST": str(item.get("connect_host", item["host"])),
            "TVT_PORT": str(item.get("connect_port", item.get("port", 9008))),
            "TVT_USER": str(item["username"]),
            "TVT_PASSWORD": str(item["password"]),
        }
    )
    return env


def _metadata_client(item: dict[str, Any]) -> TVT9008Client:
    return TVT9008Client(
        str(item.get("connect_host", item["host"])),
        int(item.get("connect_port", item.get("port", 9008))),
        str(item["username"]),
        str(item["password"]),
        timeout=8.0,
    )


def run_command(
    command: list[str],
    *,
    timeout: int,
    log_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    LOG.info("Running %s", " ".join(command[:2]) + (" …" if len(command) > 2 else ""))
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    output = completed.stdout or ""
    if env and env.get("TVT_PASSWORD"):
        output = output.replace(env["TVT_PASSWORD"], "***")
    if log_path is not None:
        log_path.write_text(output, encoding="utf-8")
    if completed.returncode != 0:
        tail = "\n".join(output.splitlines()[-30:])
        raise RuntimeError(f"Command failed with exit code {completed.returncode}:\n{tail}")
    completed.stdout = output
    return completed


def _redact_log(path: Path, env: dict[str, str] | None) -> str:
    try:
        output = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if env and env.get("TVT_PASSWORD"):
        output = output.replace(env["TVT_PASSWORD"], "***")
        path.write_text(output, encoding="utf-8")
    return output


def _run_logged_process(
    command: list[str],
    *,
    timeout: int,
    log_path: Path,
    env: dict[str, str] | None = None,
    monitor: Callable[[], None] | None = None,
) -> None:
    LOG.info("Running %s", " ".join(command[:2]) + (" …" if len(command) > 2 else ""))
    deadline = time.monotonic() + timeout
    timed_out = False
    with log_path.open("wb") as log_handle:
        process = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
        )
        try:
            while process.poll() is None:
                if monitor is not None:
                    try:
                        monitor()
                    except Exception:
                        LOG.debug("Progress monitor failed", exc_info=True)
                if time.monotonic() >= deadline:
                    timed_out = True
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    break
                time.sleep(0.25)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    output = _redact_log(log_path, env)
    if timed_out:
        tail = "\n".join(output.splitlines()[-30:])
        raise TimeoutError(f"Command exceeded {timeout} seconds\n{tail}")
    if process.returncode != 0:
        tail = "\n".join(output.splitlines()[-30:])
        raise RuntimeError(f"Command failed with exit code {process.returncode}:\n{tail}")


def _read_json_if_ready(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _captured_span_seconds(timing: dict[str, Any]) -> float:
    explicit = float(timing.get("captured_seconds", 0) or 0)
    if explicit > 0:
        return explicit
    first_values = [
        int(timing.get("first_video_time_us", 0) or 0),
        int(timing.get("first_audio_time_us", 0) or 0),
    ]
    last_values = [
        int(timing.get("last_video_time_us", 0) or 0),
        int(timing.get("last_audio_time_us", 0) or 0),
    ]
    first = min((value for value in first_values if value > 0), default=0)
    last = max(last_values, default=0)
    return max(0.0, (last - first) / 1_000_000) if first and last else 0.0


def run_capture_with_progress(
    command: list[str],
    *,
    job: Job,
    duration: int,
    work_directory: Path,
    timeout: int,
    env: dict[str, str],
    log_path: Path,
) -> None:
    timing_path = work_directory / "timing.json"

    def monitor() -> None:
        timing = _read_json_if_ready(timing_path)
        captured = min(float(duration), _captured_span_seconds(timing))
        fraction = min(1.0, captured / max(1, duration))
        update_job(
            job,
            progress=min(0.92, fraction * 0.92),
            captured_seconds=captured,
            remaining_seconds=max(0.0, duration - captured),
        )

    _run_logged_process(
        command,
        timeout=timeout,
        log_path=log_path,
        env=env,
        monitor=monitor,
    )
    monitor()
    update_job(job, progress=0.92, captured_seconds=float(duration), remaining_seconds=0.0)


def _read_ffmpeg_out_time_seconds(path: Path) -> float:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0.0
    values: dict[str, str] = {}
    for line in lines:
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    for key, divisor in (("out_time_us", 1_000_000), ("out_time_ms", 1_000_000)):
        raw = values.get(key)
        if raw in (None, ""):
            continue
        try:
            return max(0.0, int(raw) / divisor)
        except ValueError:
            continue
    return 0.0


def run_ffmpeg_with_progress(
    command: list[str],
    *,
    job: Job,
    duration: int,
    work_directory: Path,
    timeout: int,
    log_path: Path,
) -> None:
    progress_path = work_directory / "ffmpeg-progress.txt"
    monitored_command = [command[0], "-progress", str(progress_path), "-nostats", *command[1:]]

    def monitor() -> None:
        processed = min(float(duration), _read_ffmpeg_out_time_seconds(progress_path))
        fraction = min(1.0, processed / max(1, duration))
        update_job(
            job,
            progress=0.92 + fraction * 0.07,
            processed_seconds=processed,
            remaining_seconds=0.0,
        )

    _run_logged_process(
        monitored_command,
        timeout=timeout,
        log_path=log_path,
        monitor=monitor,
    )
    monitor()
    update_job(job, progress=0.99, processed_seconds=float(duration), remaining_seconds=0.0)


def camera_has_active_jobs(camera_id: str) -> bool:
    with JOBS_LOCK:
        jobs = any(
            job.camera_id == camera_id and job.status in ("queued", "running")
            for job in JOBS.values()
        )
    with SESSIONS_LOCK:
        sessions = any(
            value.camera_id == camera_id and value.status in ("queued", "running", "playing")
            for value in SESSIONS.values()
        )
        streams = any(
            value.camera_id == camera_id and not value.finished()
            for value in FRAME_SESSIONS.values()
        )
    return jobs or sessions or streams


def test_camera_definition(item: dict[str, Any]) -> dict[str, Any]:
    now = dt.datetime.now()
    start = now - dt.timedelta(minutes=10)
    with _metadata_client(item) as client:
        segments = client.search(start, now)
        dates = client.recording_dates()
    return {
        "online": True,
        "segments_found": len(segments),
        "recording_dates": len(dates),
    }


def persist_camera_map(updated: dict[str, dict[str, Any]]) -> None:
    global CONFIG, CAMERAS, CAMERA_LOCKS, CAMERA_SESSION_SLOTS, METADATA_LOCKS
    with CONFIG_LOCK:
        new_config = json.loads(json.dumps(CONFIG))
        new_config["cameras"] = [dict(item) for item in updated.values()]
        json_dump_atomic(CONFIG_PATH, new_config, mode=0o600)
        old_locks = CAMERA_LOCKS
        old_session_slots = CAMERA_SESSION_SLOTS
        old_metadata_locks = METADATA_LOCKS
        CONFIG = new_config
        CAMERAS = {camera_id: dict(item) for camera_id, item in updated.items()}
        CAMERA_LOCKS = {
            camera_id: old_locks.get(camera_id, threading.Lock()) for camera_id in CAMERAS
        }
        CAMERA_SESSION_SLOTS = {
            camera_id: old_session_slots.get(
                camera_id, threading.BoundedSemaphore(NATIVE_SESSION_LIMIT)
            )
            for camera_id in CAMERAS
        }
        METADATA_LOCKS = {
            camera_id: old_metadata_locks.get(camera_id, threading.Lock()) for camera_id in CAMERAS
        }


def add_camera_definition(payload: dict[str, Any]) -> dict[str, Any]:
    item = normalize_camera_definition(payload)
    camera_id = str(item["id"])
    with CONFIG_LOCK:
        if camera_id in CAMERAS:
            raise ValueError(f"Camera ID already exists: {camera_id}")
    test = test_camera_definition(item)
    with CONFIG_LOCK:
        updated = {camera_id_: dict(value) for camera_id_, value in CAMERAS.items()}
        if camera_id in updated:
            raise ValueError(f"Camera ID already exists: {camera_id}")
        updated[camera_id] = item
        persist_camera_map(updated)
    return {"camera": safe_camera(camera_id), "test": test}


def update_camera_definition(camera_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    existing = camera(camera_id)
    if camera_has_active_jobs(camera_id):
        raise ValueError("Wait for this camera's active playback/download job to finish")
    item = normalize_camera_definition(payload, existing, fixed_id=camera_id)
    connection_keys = ("host", "port", "username", "password")
    if any(item.get(key) != existing.get(key) for key in connection_keys):
        with camera_lock(camera_id):
            test = test_camera_definition(item)
    else:
        test = {"online": True, "connection_test_skipped": True}
    with CONFIG_LOCK:
        if camera_has_active_jobs(camera_id):
            raise ValueError("Wait for this camera's active playback/download job to finish")
        updated = {camera_id_: dict(value) for camera_id_, value in CAMERAS.items()}
        if camera_id not in updated:
            raise KeyError(f"Unknown camera: {camera_id}")
        updated[camera_id] = item
        persist_camera_map(updated)
    camera_index = camera_index_directory(camera_id)
    if camera_index.exists():
        for cached in camera_index.iterdir():
            if cached.name == "capabilities.json":
                continue
            try:
                shutil.rmtree(cached, ignore_errors=True) if cached.is_dir() else cached.unlink(
                    missing_ok=True
                )
            except OSError:
                LOG.warning("Could not clear cached camera metadata: %s", cached)
    return {"camera": safe_camera(camera_id), "test": test}


def delete_camera_definition(camera_id: str) -> dict[str, Any]:
    existing = safe_camera(camera_id)
    if camera_has_active_jobs(camera_id):
        raise ValueError("Wait for this camera's active playback/download job to finish")
    lock = camera_lock(camera_id)
    with lock, CONFIG_LOCK:
        if camera_has_active_jobs(camera_id):
            raise ValueError("Wait for this camera's active playback/download job to finish")
        updated = {
            camera_id_: dict(value)
            for camera_id_, value in CAMERAS.items()
            if camera_id_ != camera_id
        }
        if len(updated) == len(CAMERAS):
            raise KeyError(f"Unknown camera: {camera_id}")
        persist_camera_map(updated)
    shutil.rmtree(camera_index_directory(camera_id), ignore_errors=True)
    return {"removed": existing}


def parse_local_timestamp(value: str) -> dt.datetime:
    value = value.replace(" ", "T")
    if not TIME_RE.match(value):
        raise ValueError("Timestamp must use YYYY-MM-DDTHH:MM:SS")
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        raise ValueError("Use the camera's local time without a timezone suffix")
    return parsed


def validate_date(value: str) -> dt.date:
    if not DATE_RE.match(value):
        raise ValueError("Date must use YYYY-MM-DD")
    return dt.date.fromisoformat(value)


def promote_completed_file(source: Path, destination: Path, *, mode: int = 0o600) -> None:
    """Move into place atomically, copying first when the volumes differ (EXDEV)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, destination)
        os.chmod(destination, mode)
        return
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise

    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as input_file, temporary.open("xb") as output_file:
            shutil.copyfileobj(input_file, output_file, length=8 * 1024 * 1024)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
        source.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)


def merge_segments(
    raw_segments: list[dict[str, Any]], query_start: dt.datetime, query_stop: dt.datetime
) -> dict[str, Any]:
    intervals: list[tuple[dt.datetime, dt.datetime]] = []
    for segment in raw_segments:
        try:
            start = max(dt.datetime.fromisoformat(str(segment["start"])), query_start)
            stop = min(dt.datetime.fromisoformat(str(segment["stop"])), query_stop)
        except (KeyError, TypeError, ValueError):
            continue
        if stop > start:
            intervals.append((start, stop))
    intervals.sort()
    merged: list[list[dt.datetime]] = []
    for start, stop in intervals:
        if not merged or (start - merged[-1][1]).total_seconds() > 2:
            merged.append([start, stop])
        elif stop > merged[-1][1]:
            merged[-1][1] = stop
    gaps: list[tuple[dt.datetime, dt.datetime]] = []
    cursor = query_start
    for start, stop in merged:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, stop)
    if cursor < query_stop:
        gaps.append((cursor, query_stop))
    recorded_seconds = sum((stop - start).total_seconds() for start, stop in merged)
    return {
        "raw_segments": raw_segments,
        "merged_ranges": [
            {
                "start": start.isoformat(timespec="seconds"),
                "stop": stop.isoformat(timespec="seconds"),
            }
            for start, stop in merged
        ],
        "gaps": [
            {
                "start": start.isoformat(timespec="seconds"),
                "stop": stop.isoformat(timespec="seconds"),
                "seconds": int((stop - start).total_seconds()),
            }
            for start, stop in gaps
        ],
        "recorded_seconds": int(recorded_seconds),
        "recorded_hours": round(recorded_seconds / 3600, 2),
    }


def search_window(
    camera_id: str,
    start: dt.datetime,
    stop: dt.datetime,
    *,
    cache_name: str | None = None,
    ttl: int = 60,
) -> dict[str, Any]:
    item = camera(camera_id)
    camera_index = camera_index_directory(camera_id)
    camera_index.mkdir(parents=True, exist_ok=True)
    cache_path = camera_index / f"{cache_name}.json" if cache_name else None
    if cache_path and cache_path.exists() and time.time() - cache_path.stat().st_mtime < ttl:
        return json.loads(cache_path.read_text(encoding="utf-8"))

    lock = metadata_lock(camera_id)
    # Another request may be refreshing the same window; wait for it and reuse its cache.
    if not lock.acquire(timeout=20):
        if cache_path and cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            cached["stale"] = True
            cached["busy"] = True
            return cached
        raise RuntimeError("Archive metadata is already being refreshed")
    try:
        if cache_path and cache_path.exists() and time.time() - cache_path.stat().st_mtime < ttl:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        try:
            with _metadata_client(item) as client:
                raw_segments = client.search(start, stop)
        except Exception:
            if cache_path and cache_path.exists():
                LOG.warning("Using cached archive metadata for %s after refresh failure", camera_id)
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                cached["stale"] = True
                cached["refresh_failed"] = True
                return cached
            raise
    finally:
        lock.release()
    result = {
        "camera_id": camera_id,
        "query_start": start.isoformat(timespec="seconds"),
        "query_stop": stop.isoformat(timespec="seconds"),
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        **merge_segments(raw_segments, start, stop),
    }
    if cache_path:
        json_dump_atomic(cache_path, result)
    return result


def get_timeline(camera_id: str, day: dt.date, *, force: bool = False) -> dict[str, Any]:
    start = dt.datetime.combine(day, dt.time.min)
    stop = start + dt.timedelta(days=1)
    result = search_window(
        camera_id, start, stop, cache_name=f"day-{day.isoformat()}", ttl=0 if force else 60
    )
    now = dt.datetime.now()
    result["recording_now"] = day == now.date() and any(
        dt.datetime.fromisoformat(item["start"])
        <= now
        <= dt.datetime.fromisoformat(item["stop"]) + dt.timedelta(seconds=120)
        for item in result["merged_ranges"]
    )
    return result


def get_availability(camera_id: str, days: int) -> dict[str, Any]:
    days = max(1, min(days, 180))
    stop = dt.datetime.combine(dt.date.today() + dt.timedelta(days=1), dt.time.min)
    start = stop - dt.timedelta(days=days)
    result = search_window(camera_id, start, stop, cache_name=f"availability-{days}", ttl=1800)
    dates: set[str] = set()
    for item in result["merged_ranges"]:
        start_value = dt.datetime.fromisoformat(item["start"])
        stop_value = dt.datetime.fromisoformat(item["stop"])
        cursor = start_value.date()
        end = (stop_value - dt.timedelta(microseconds=1)).date()
        while cursor <= end:
            dates.add(cursor.isoformat())
            cursor += dt.timedelta(days=1)
    result["days_with_recordings"] = sorted(dates)
    result["day_count"] = len(dates)
    result["earliest"] = result["merged_ranges"][0]["start"] if result["merged_ranges"] else None
    result["latest"] = result["merged_ranges"][-1]["stop"] if result["merged_ranges"] else None
    if result["earliest"] and result["latest"]:
        earliest = dt.datetime.fromisoformat(result["earliest"])
        latest = dt.datetime.fromisoformat(result["latest"])
        result["available_history_seconds"] = max(0, int((latest - earliest).total_seconds()))
        result["available_history_hours"] = round(result["available_history_seconds"] / 3600, 1)
    else:
        result["available_history_seconds"] = 0
        result["available_history_hours"] = 0
    return result


def get_status(camera_id: str, *, force: bool = False) -> dict[str, Any]:
    try:
        today = get_timeline(camera_id, dt.date.today(), force=force)
        availability = get_availability(camera_id, int(PROCESSING.get("availability_days", 45)))
        return {
            "camera": safe_camera(camera_id),
            "online": True,
            "timeline_today": today,
            "availability": availability,
            "encoder": encoder_info(),
            "gop_seconds": _read_camera_capabilities(camera_id).get("gop_seconds"),
        }
    except Exception as error:
        LOG.exception("Status failed for %s", camera_id)
        return {
            "camera": safe_camera(camera_id),
            "online": False,
            "error": str(error),
            "timeline_today": {},
            "availability": {},
            "encoder": encoder_info(),
        }


@functools.cache
def ffmpeg_version() -> str:
    try:
        completed = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=10, check=False
        )
        return completed.stdout.splitlines()[0].strip() if completed.stdout else "unavailable"
    except Exception:
        return "unavailable"


def encoder_info() -> dict[str, Any]:
    return {
        "name": ENCODER,
        "dri_device": DRI_DEVICE if ENCODER == "vaapi" else None,
        "ffmpeg": ffmpeg_version(),
    }


def playback_video_mode(camera_id: str) -> str:
    gop = float(_read_camera_capabilities(camera_id).get("gop_seconds", 0) or 0)
    if 0 < gop <= COPY_MAX_GOP_SECONDS:
        return "copy"
    return ENCODER


def video_args(quality: str, mode: str, gop: int | None = None) -> tuple[list[str], list[str]]:
    """Pre-input and output arguments. gop is the HLS keyframe interval in frames."""
    if mode == "copy":
        return [], ["-c:v", "copy"]
    if mode == "vaapi":
        pre = [
            "-init_hw_device",
            f"vaapi=va:{DRI_DEVICE}",
            "-filter_hw_device",
            "va",
            "-hwaccel",
            "vaapi",
            "-hwaccel_device",
            "va",
            "-hwaccel_output_format",
            "vaapi",
        ]
        if quality == "original":
            out = ["-c:v", "h264_vaapi", "-profile:v", "high", "-qp", "18"]
        else:
            out = [
                "-vf",
                "scale_vaapi=w=854:h=480:format=nv12",
                "-c:v",
                "h264_vaapi",
                "-profile:v",
                "high",
                "-b:v",
                "900k",
                "-maxrate",
                "1200k",
                "-bufsize",
                "2400k",
            ]
    else:
        pre = []
        if quality == "original":
            out = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
        else:
            out = [
                "-vf",
                "scale=854:480:flags=fast_bilinear",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "29",
            ]
    if gop is None:
        return pre, [*out, "-g", "50"]
    out += [
        "-g",
        str(gop),
        "-bf",
        "0",
        "-force_key_frames",
        f"expr:gte(t,n_forced*{HLS_SEGMENT_SECONDS})",
    ]
    if mode == "software":
        out += ["-keyint_min", str(gop), "-sc_threshold", "0"]
    return pre, out


def clean_cache() -> None:
    cutoff = time.time() - CACHE_HOURS * 3600
    for path in CACHE.glob("*.mp4"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except FileNotFoundError:
            pass
    work_cutoff = time.time() - 2 * 3600
    for path in WORK.iterdir():
        try:
            if path.stat().st_mtime < work_cutoff:
                shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(
                    missing_ok=True
                )
        except FileNotFoundError:
            pass
    with JOBS_LOCK:
        stale = [
            job_id
            for job_id, job in JOBS.items()
            if job.finished_at and job.finished_at < time.time() - 12 * 3600
        ]
        for job_id in stale:
            cache_key = JOBS[job_id].cache_key
            JOBS.pop(job_id, None)
            if CACHE_TO_JOB.get(cache_key) == job_id:
                CACHE_TO_JOB.pop(cache_key, None)


def build_cache_key(camera_id: str, request: dict[str, Any]) -> str:
    payload = json.dumps(
        {"version": APP_VERSION, "camera_id": camera_id, **request},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def update_job(job: Job, **changes: Any) -> None:
    with JOBS_LOCK:
        for key, value in changes.items():
            setattr(job, key, value)


def export_filename(camera_name: str, start: dt.datetime, duration: int) -> str:
    component = re.sub(r"[^a-zA-Z0-9]+", "-", camera_name).strip("-") or "Camera"
    stop = start + dt.timedelta(seconds=duration)
    return f"{component}_{start:%Y-%m-%d_%H-%M-%S}_to_{stop:%Y-%m-%d_%H-%M-%S}.mp4"


def generate_job(job: Job) -> None:
    item = camera(job.camera_id)
    request = job.request
    start = parse_local_timestamp(str(request["start"]))
    duration = int(request["duration"])
    gain_db = int(request["gain_db"])
    output_name = export_filename(str(item.get("name", job.camera_id)), start, duration)
    output_path = CACHE / f"{job.cache_key}.mp4"
    job_log = LOGS / f"{job.id}.log"
    work_directory = Path(tempfile.mkdtemp(prefix=f"job-{job.id[:8]}-", dir=WORK))
    update_job(
        job,
        status="queued",
        phase="Waiting for camera",
        started_at=None,
        progress=0.0,
        captured_seconds=0.0,
        processed_seconds=0.0,
        remaining_seconds=float(duration),
    )
    try:
        if output_path.exists() and output_path.stat().st_size > 1024:
            update_job(
                job,
                status="ready",
                phase="Ready from cache",
                finished_at=time.time(),
                output_path=str(output_path),
                output_name=output_name,
                progress=1.0,
                captured_seconds=float(duration),
                processed_seconds=float(duration),
                remaining_seconds=0.0,
            )
            return
        capture_log = work_directory / "capture.log"
        capture_command = [
            sys.executable,
            str(CAPTURE_HELPER),
            start.strftime("%Y-%m-%d %H:%M:%S"),
            str(duration),
            str(work_directory),
        ]
        with camera_session_slot(job.camera_id):
            update_job(
                job,
                status="running",
                phase="Receiving recording",
                started_at=time.time(),
            )
            run_capture_with_progress(
                capture_command,
                job=job,
                duration=duration,
                work_directory=work_directory,
                timeout=duration * 2 + 90,
                env=capture_environment(item),
                log_path=capture_log,
            )
        summary_path = work_directory / "summary.json"
        video_path = work_directory / "video.h264"
        audio_path = work_directory / "audio.alaw"
        if not summary_path.exists() or not video_path.exists():
            raise RuntimeError("The archive backend did not create its required output files")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        video_frames = int(summary.get("video_frames", 0))
        audio_frames = int(summary.get("audio_frames", 0))
        gop_seconds = float(summary.get("gop_seconds", 0) or 0)
        if gop_seconds:
            _remember_capability(job.camera_id, "gop_seconds", gop_seconds)
        has_audio = (
            bool(summary.get("has_audio", audio_frames > 0))
            and audio_path.exists()
            and audio_path.stat().st_size > 0
        )
        if video_frames < 2 or not video_path.stat().st_size:
            raise RuntimeError(f"Incomplete archive capture: {video_frames} video frames")
        update_job(
            job,
            video_frames=video_frames,
            audio_frames=audio_frames,
            phase="Preparing browser file",
            progress=0.92,
            remaining_seconds=0.0,
        )
        first_video = float(summary.get("first_video_time_us", 0))
        last_video = float(summary.get("last_video_time_us", 0))
        first_audio = float(summary.get("first_audio_time_us", 0))
        fps = float(summary.get("source_fps", 0) or 0)
        if not 5 <= fps <= 120:
            fps = (
                (video_frames - 1) * 1_000_000 / (last_video - first_video)
                if last_video > first_video
                else 25.0
            )
        if not 5 <= fps <= 120:
            fps = 25.0
        offset_ms = (
            round((first_audio - first_video) / 1000)
            if has_audio and first_audio and first_video
            else 0
        )
        pre_input, video_output = video_args("original", "copy")
        ffmpeg = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+genpts",
            *pre_input,
            "-r",
            f"{fps:.6f}",
            "-f",
            "h264",
            "-i",
            str(video_path),
        ]
        if has_audio:
            ffmpeg += [
                "-f",
                "alaw",
                "-ar",
                "8000",
                "-ac",
                "1",
                "-i",
                str(audio_path),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
            ]
        else:
            ffmpeg += ["-map", "0:v:0", "-an"]
        ffmpeg += video_output
        if has_audio:
            audio_filters: list[str] = []
            if offset_ms > 0:
                audio_filters.append(f"adelay={offset_ms}:all=1")
            elif offset_ms < 0:
                audio_filters += [
                    f"atrim=start={abs(offset_ms) / 1000:.3f}",
                    "asetpts=PTS-STARTPTS",
                ]
            if gain_db:
                audio_filters += [f"volume={gain_db}dB", "alimiter=limit=0.95"]
            audio_filters.append("aresample=async=1:first_pts=0")
            ffmpeg += [
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-ar",
                "48000",
                "-ac",
                "1",
                "-af",
                ",".join(audio_filters),
            ]
        ffmpeg += [
            "-t",
            str(duration),
            "-movflags",
            "+faststart",
            "-metadata",
            f"creation_time={start.isoformat(timespec='seconds')}",
            str(work_directory / "final.mp4"),
        ]
        ffmpeg_log = work_directory / "ffmpeg.log"
        run_ffmpeg_with_progress(
            ffmpeg,
            job=job,
            duration=duration,
            work_directory=work_directory,
            timeout=max(120, duration * 3),
            log_path=ffmpeg_log,
        )
        update_job(job, phase="Validating file", progress=0.995)
        probe = run_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,duration:format=duration,size",
                "-of",
                "json",
                str(work_directory / "final.mp4"),
            ],
            timeout=30,
        )
        stream_types = {
            stream.get("codec_type") for stream in json.loads(probe.stdout).get("streams", [])
        }
        required_streams = {"video", "audio"} if has_audio else {"video"}
        if not required_streams.issubset(stream_types):
            raise RuntimeError(
                f"Generated MP4 is missing required streams: {sorted(required_streams - stream_types)}"
            )
        promote_completed_file(work_directory / "final.mp4", output_path, mode=0o600)
        job_log.write_text(
            "\n=== Archive capture ===\n"
            + capture_log.read_text(errors="replace")
            + "\n=== FFmpeg ===\n"
            + ffmpeg_log.read_text(errors="replace"),
            encoding="utf-8",
        )
        update_job(
            job,
            status="ready",
            phase="Ready",
            finished_at=time.time(),
            output_path=str(output_path),
            output_name=output_name,
            progress=1.0,
            captured_seconds=float(duration),
            processed_seconds=float(duration),
            remaining_seconds=0.0,
        )
        shutil.rmtree(work_directory, ignore_errors=True)
    except Exception as error:
        LOG.error("Job %s failed: %s", job.id, error)
        try:
            diagnostics = LOGS / f"{job.id}-failure"
            if diagnostics.exists():
                shutil.rmtree(diagnostics)
            shutil.move(str(work_directory), diagnostics)
        except Exception:
            LOG.error("Could not retain diagnostics:\n%s", traceback.format_exc())
        update_job(
            job,
            status="error",
            phase="Failed",
            finished_at=time.time(),
            error=str(error),
            remaining_seconds=0.0,
        )


def create_job(camera_id: str, request: dict[str, Any]) -> Job:
    item = camera(camera_id)
    start = parse_local_timestamp(str(request.get("start", "")))
    duration = int(request.get("duration", 300))
    if not 5 <= duration <= DOWNLOAD_MAX_SECONDS:
        raise ValueError(f"Duration must be 5-{DOWNLOAD_MAX_SECONDS} seconds")
    gain_db = int(request.get("gain_db", DEFAULT_GAIN))
    if gain_db not in (0, 6, 12, 18, 24):
        raise ValueError("Audio gain must be 0, 6, 12, 18, or 24 dB")
    normalized = {
        "start": start.isoformat(timespec="seconds"),
        "duration": duration,
        "gain_db": gain_db,
    }
    cache_key = build_cache_key(camera_id, normalized)
    output_path = CACHE / f"{cache_key}.mp4"
    output_name = export_filename(str(item.get("name", camera_id)), start, duration)
    with CONFIG_LOCK, JOBS_LOCK:
        camera(camera_id)
        existing_id = CACHE_TO_JOB.get(cache_key)
        if (
            existing_id
            and existing_id in JOBS
            and JOBS[existing_id].status in ("queued", "running", "ready")
        ):
            return JOBS[existing_id]
        job = Job(id=uuid.uuid4().hex, camera_id=camera_id, cache_key=cache_key, request=normalized)
        if output_path.exists() and output_path.stat().st_size > 1024:
            job.status, job.phase = "ready", "Ready from cache"
            job.started_at = job.finished_at = job.created_at
            job.output_path, job.output_name = str(output_path), output_name
            job.progress = 1.0
            job.captured_seconds = job.processed_seconds = float(duration)
        JOBS[job.id] = job
        CACHE_TO_JOB[cache_key] = job.id
    if job.status == "queued":
        EXECUTOR.submit(generate_job, job)
    return job


def _tail_text(path: Path, limit: int = 12000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace").strip()


def _stream_failure_detail(work_directory: Path) -> str:
    parts: list[str] = []
    native = _tail_text(work_directory / "capture.log")
    ffmpeg = _tail_text(work_directory / "ffmpeg.log")
    if native:
        parts.append(f"archive capture log:\n{native}")
    if ffmpeg:
        parts.append(f"FFmpeg log:\n{ffmpeg}")
    return "\n\n".join(parts) or "No capture or ffmpeg diagnostics were produced."


def _feed_growing_file(
    source: Path,
    write_fd: int,
    capture_process: subprocess.Popen[bytes],
    stop_event: threading.Event,
) -> None:
    try:
        while not source.exists() and capture_process.poll() is None and not stop_event.wait(0.02):
            pass
        if not source.exists():
            return
        with source.open("rb", buffering=0) as handle:
            while not stop_event.is_set():
                chunk = handle.read(256 * 1024)
                if chunk:
                    view = memoryview(chunk)
                    while view and not stop_event.is_set():
                        try:
                            written = os.write(write_fd, view)
                        except (BrokenPipeError, OSError):
                            return
                        view = view[written:]
                    continue
                if capture_process.poll() is not None:
                    break
                stop_event.wait(0.02)
    finally:
        try:
            os.close(write_fd)
        except OSError:
            pass


def _timing_has_audio(value: dict[str, Any]) -> bool:
    return bool(
        value.get("has_audio", False)
        or int(value.get("audio_frames", 0) or 0) > 0
        or int(value.get("first_audio_time_us", 0) or 0) > 0
    )


def _video_media_elapsed_seconds(value: dict[str, Any]) -> float:
    first = int(value.get("first_video_time_us", 0) or 0)
    last = int(value.get("last_video_time_us", 0) or 0)
    return max(0.0, (last - first) / 1_000_000) if first > 0 and last > first else 0.0


def _audio_video_elapsed_samples(value: dict[str, Any]) -> int:
    explicit = float(value.get("captured_seconds", 0) or 0)
    if explicit > 0:
        return max(0, round(explicit * ALAW_SAMPLE_RATE))
    first = int(value.get("first_video_time_us", 0) or 0)
    last = int(value.get("last_video_time_us", 0) or 0)
    return (
        max(0, round((last - first) * ALAW_SAMPLE_RATE / 1_000_000))
        if first > 0 and last > first
        else 0
    )


def _audio_offset_samples(value: dict[str, Any]) -> int:
    v = int(value.get("first_video_time_us", 0) or 0)
    a = int(value.get("first_audio_time_us", 0) or 0)
    return round((a - v) * ALAW_SAMPLE_RATE / 1_000_000) if v and a else 0


def _write_pipe_bytes(fd: int, payload: bytes, stop: threading.Event) -> bool:
    view = memoryview(payload)
    while view and not stop.is_set():
        try:
            n = os.write(fd, view)
        except (BrokenPipeError, OSError):
            return False
        view = view[n:]
    return not stop.is_set()


def _feed_expected_archive_audio(
    source: Path,
    directory: Path,
    write_fd: int,
    capture_process: subprocess.Popen[bytes],
    stop_event: threading.Event,
    camera_id: str,
) -> None:
    handle = None
    emitted = 0
    offset = None
    real_started = False
    try:
        while not stop_event.is_set():
            timing = _read_live_timing(directory) or {}
            if _timing_has_audio(timing):
                _remember_archive_audio(camera_id)
            if handle is None and source.exists() and _timing_has_audio(timing):
                try:
                    if source.stat().st_size > 0:
                        handle = source.open("rb", buffering=0)
                        offset = _audio_offset_samples(timing)
                        if offset < 0:
                            handle.seek(-offset)
                        elif emitted > offset:
                            handle.seek(emitted - offset)
                except OSError:
                    handle = None
            if handle is None:
                target = _audio_video_elapsed_samples(timing)
                if target > emitted:
                    count = min(4096, target - emitted)
                    if not _write_pipe_bytes(write_fd, ALAW_SILENCE_BYTE * count, stop_event):
                        return
                    emitted += count
                    continue
            else:
                assert offset is not None
                if offset > emitted:
                    count = min(4096, offset - emitted)
                    if not _write_pipe_bytes(write_fd, ALAW_SILENCE_BYTE * count, stop_event):
                        return
                    emitted += count
                    continue
                chunk = handle.read(256 * 1024)
                if chunk:
                    if not _write_pipe_bytes(write_fd, chunk, stop_event):
                        return
                    emitted += len(chunk)
                    real_started = True
                    continue
                if not real_started:
                    target = _audio_video_elapsed_samples(timing)
                    if target > emitted:
                        count = min(4096, target - emitted)
                        if not _write_pipe_bytes(write_fd, ALAW_SILENCE_BYTE * count, stop_event):
                            return
                        emitted += count
                        try:
                            handle.seek(max(0, emitted - offset))
                        except OSError:
                            pass
                        continue
            if capture_process.poll() is not None:
                break
            stop_event.wait(0.02)
    finally:
        if handle is not None:
            handle.close()
        try:
            os.close(write_fd)
        except OSError:
            pass


def _watch_for_archive_audio(
    directory: Path,
    capture_process: subprocess.Popen[bytes],
    stop_event: threading.Event,
    camera_id: str,
) -> None:
    while not stop_event.is_set():
        timing = _read_live_timing(directory)
        if timing and _timing_has_audio(timing):
            _remember_archive_audio(camera_id)
            return
        if capture_process.poll() is not None:
            return
        stop_event.wait(0.04)


def _session(session_id: str, *, touch: bool = True) -> PlaybackSession:
    with SESSIONS_LOCK:
        value = SESSIONS.get(session_id)
        if value is None:
            raise KeyError(f"Unknown playback session: {session_id}")
        if touch:
            value.last_access = time.time()
        return value


def _terminate_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _read_live_timing(directory: Path) -> dict[str, Any] | None:
    path = directory / "timing.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _wait_for_capture_timing(
    directory: Path,
    capture_process: subprocess.Popen[bytes],
    stop_event: threading.Event,
    *,
    probe_audio: bool = True,
    phase_callback: Callable[[str], None] | None = None,
) -> tuple[float, int, bool]:
    """Wait for the first video, then measure frame rate and audio offset."""
    opening_deadline = time.monotonic() + HLS_FIRST_MEDIA_TIMEOUT_SECONDS
    probe_deadline: float | None = None
    latest: dict[str, Any] | None = None

    def measured_fps(value: dict[str, Any]) -> float:
        frames = int(value.get("video_frames", 0) or 0)
        first_video = int(value.get("first_video_time_us", 0) or 0)
        last_video = int(value.get("last_video_time_us", 0) or 0)
        span = last_video - first_video
        if frames > 1 and span > 0:
            direct = (frames - 1) * 1_000_000 / span
            if 5.0 <= direct <= 120.0:
                return direct
        reported = float(value.get("source_fps", 0) or 0)
        return reported if 5.0 <= reported <= 120.0 else 0.0

    def result_from(value: dict[str, Any], *, fallback: bool = False) -> tuple[float, int, bool]:
        fps = measured_fps(value) or (25.0 if fallback else 0.0)
        first_video = int(value.get("first_video_time_us", 0) or 0)
        first_audio = int(value.get("first_audio_time_us", 0) or 0)
        audio_frames = int(value.get("audio_frames", 0) or 0)
        has_audio = bool(value.get("has_audio", False) or first_audio or audio_frames)
        offset = (
            round((first_audio - first_video) / 1000)
            if has_audio and first_audio and first_video
            else 0
        )
        return fps, offset, has_audio

    while not stop_event.is_set():
        now = time.monotonic()
        latest = _read_live_timing(directory) or latest
        if latest:
            frames = int(latest.get("video_frames", 0) or 0)
            if frames > 0 and probe_deadline is None:
                probe_deadline = now + HLS_TIMING_MAX_SECONDS
                if phase_callback is not None:
                    phase_callback("Measuring archive frame timing")

            if probe_deadline is not None:
                fps = measured_fps(latest)
                first_audio = int(latest.get("first_audio_time_us", 0) or 0)
                audio_frames = int(latest.get("audio_frames", 0) or 0)
                has_audio = bool(latest.get("has_audio", False) or first_audio or audio_frames)
                if frames >= HLS_TIMING_SAMPLE_FRAMES and fps:
                    if has_audio:
                        return result_from(latest)
                    if (
                        not probe_audio
                        or _video_media_elapsed_seconds(latest) >= HLS_AUDIO_PROBE_MEDIA_SECONDS
                    ):
                        return fps, 0, False
                if now >= probe_deadline:
                    LOG.warning(
                        "Archive timing probe reached %.2fs after first video; using best available timing",
                        HLS_TIMING_MAX_SECONDS,
                    )
                    return result_from(latest, fallback=True)

        rc = capture_process.poll()
        if rc is not None:
            frames = int((latest or {}).get("video_frames", 0) or 0)
            if frames < 2:
                detail = _tail_text(directory / "capture.log")
                raise RuntimeError(_friendly_capture_error(detail, rc))
            return result_from(latest or {}, fallback=True)

        if probe_deadline is None and now >= opening_deadline:
            detail = _tail_text(directory / "capture.log")
            if detail:
                LOG.warning("No archive video arrived before startup timeout: %s", detail[-500:])
            raise RuntimeError(
                f"Camera archive opened but delivered no video within "
                f"{HLS_FIRST_MEDIA_TIMEOUT_SECONDS:.0f} seconds."
            )
        stop_event.wait(0.04)

    raise RuntimeError("Archive playback startup was cancelled.")


def _friendly_capture_error(detail: str, return_code: int | None = None) -> str:
    text = detail or ""
    lower = text.lower()
    if "no route to host" in lower or "network is unreachable" in lower:
        return "Camera is temporarily unreachable on the LAN. The archive connection was retried but no route was available."
    if "connection refused" in lower:
        return "Camera refused the archive connection. Check that the camera is online and TCP port 9008 is reachable."
    if "timed out" in lower or "timeout" in lower:
        return "Camera archive connection timed out after automatic retries."
    if "login" in lower and ("reject" in lower or "failed" in lower):
        return "Camera rejected the archive login."
    suffix = f" (capture exited {return_code})" if return_code is not None else ""
    return f"Camera archive capture failed{suffix}." + (f" {text[-500:]}" if text else "")


def _hls_command(video_input: str, audio_input: str | None, session: PlaybackSession) -> list[str]:
    request = session.request
    duration = int(request["duration"])
    quality = str(request["quality"])
    gain_db = int(request["gain_db"])
    gop = max(1, round(session.source_fps * HLS_SEGMENT_SECONDS))
    pre_input, video_output = video_args(quality, session.video, gop)
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-fflags",
        "+genpts",
        "-analyzeduration",
        "500000",
        "-probesize",
        "500000",
        *pre_input,
        "-thread_queue_size",
        "512",
        "-r",
        f"{session.source_fps:.6f}",
        "-f",
        "h264",
        "-i",
        video_input,
    ]
    if audio_input is not None:
        command += [
            "-thread_queue_size",
            "512",
            "-f",
            "alaw",
            "-ar",
            "8000",
            "-ac",
            "1",
            "-i",
            audio_input,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
        ]
    else:
        command += ["-map", "0:v:0", "-an"]
    command += video_output
    if audio_input is not None:
        filters: list[str] = []
        total_offset = (
            0 if session.audio_alignment_in_feeder else session.audio_offset_ms
        ) + STREAM_AUDIO_DELAY_MS
        if total_offset > 0:
            filters.append(f"adelay={total_offset}:all=1")
        elif total_offset < 0:
            filters += [f"atrim=start={abs(total_offset) / 1000:.3f}", "asetpts=PTS-STARTPTS"]
        if gain_db:
            filters += [f"volume={gain_db}dB", "alimiter=limit=0.95"]
        filters.append("aresample=async=1:first_pts=0")
        command += [
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-af",
            ",".join(filters),
        ]
    command += [
        "-t",
        str(duration),
        "-max_interleave_delta",
        "0",
        "-f",
        "hls",
        "-hls_time",
        str(HLS_SEGMENT_SECONDS),
        "-hls_init_time",
        str(HLS_SEGMENT_SECONDS),
        "-hls_list_size",
        "0",
        "-hls_playlist_type",
        "event",
        "-hls_segment_type",
        "fmp4",
        "-hls_allow_cache",
        "0",
        "-flush_packets",
        "1",
        "-hls_fmp4_init_filename",
        "init.mp4",
        "-hls_flags",
        "temp_file" if session.video == "copy" else "split_by_time+temp_file",
        "-hls_segment_filename",
        str(session.directory / "segment-%05d.m4s"),
        str(session.playlist_path),
    ]
    return command


def generate_hls_session(session: PlaybackSession) -> None:
    item = camera(session.camera_id)
    start = parse_local_timestamp(str(session.request["start"]))
    directory = session.directory
    directory.mkdir(parents=True, exist_ok=True)
    video_path = directory / "video.h264"
    audio_path = directory / "audio.alaw"
    capture_log_path = directory / "capture.log"
    ffmpeg_log_path = directory / "ffmpeg.log"
    feeder_threads: list[threading.Thread] = []
    stop_feeders = threading.Event()
    session.phase = "Waiting for camera"
    lock = camera_session_slot(session.camera_id)
    lock.acquire()
    session.started_at = time.time()
    session.status = "running"
    session.phase = "Opening the camera archive"
    try:
        with capture_log_path.open("wb") as capture_log, ffmpeg_log_path.open("wb") as ffmpeg_log:
            startup_error: RuntimeError | None = None
            measured_offset = 0
            audio_mode = recording_audio_mode(session.camera_id)
            learned_audio = _learned_archive_audio(session.camera_id)
            probe_audio = audio_mode == "auto" and not learned_audio
            for startup_attempt in range(HLS_FIRST_MEDIA_RETRIES + 1):
                if startup_attempt:
                    session.phase = (
                        f"Retrying camera archive ({startup_attempt}/{HLS_FIRST_MEDIA_RETRIES})"
                    )
                    LOG.warning(
                        "Playback session %s is retrying archive startup after no first video",
                        session.id,
                    )
                    for stale_path in (
                        video_path,
                        audio_path,
                        directory / "timing.json",
                        directory / "summary.json",
                    ):
                        stale_path.unlink(missing_ok=True)
                    if session.stop_event.wait(0.50):
                        raise RuntimeError("Archive playback startup was cancelled.")

                session.capture_process = subprocess.Popen(
                    [
                        sys.executable,
                        str(CAPTURE_HELPER),
                        start.strftime("%Y-%m-%d %H:%M:%S"),
                        str(session.request["duration"]),
                        str(directory),
                    ],
                    stdout=capture_log,
                    stderr=subprocess.STDOUT,
                    env=capture_environment(item),
                )
                try:
                    session.source_fps, measured_offset, session.has_audio = (
                        _wait_for_capture_timing(
                            directory,
                            session.capture_process,
                            session.stop_event,
                            probe_audio=probe_audio,
                            phase_callback=lambda phase: setattr(session, "phase", phase),
                        )
                    )
                    startup_error = None
                    break
                except RuntimeError as error:
                    _terminate_process(session.capture_process)
                    session.capture_process = None
                    if "delivered no video within" not in str(error):
                        raise
                    startup_error = error
                    if startup_attempt >= HLS_FIRST_MEDIA_RETRIES:
                        break

            if startup_error is not None:
                attempts = HLS_FIRST_MEDIA_RETRIES + 1
                raise RuntimeError(
                    f"Camera archive delivered no video after {attempts} startup "
                    f"attempt{'s' if attempts != 1 else ''} "
                    f"({HLS_FIRST_MEDIA_TIMEOUT_SECONDS:.0f} seconds each)."
                ) from startup_error

            detected_audio = bool(session.has_audio)
            if detected_audio:
                _remember_archive_audio(session.camera_id)
                learned_audio = True
            if audio_mode == "off":
                expected_audio = False
            elif audio_mode == "on":
                expected_audio = True
            else:
                expected_audio = detected_audio or learned_audio
            session.has_audio = expected_audio
            session.audio_offset_ms = measured_offset
            session.audio_alignment_in_feeder = expected_audio
            LOG.info(
                "Playback session %s measured %.4f fps, %d ms audio offset, detected_audio=%s expected_audio=%s mode=%s",
                session.id,
                session.source_fps,
                session.audio_offset_ms,
                detected_audio,
                session.has_audio,
                audio_mode,
            )
            video_read, video_write = os.pipe()
            audio_read = audio_write = None
            if session.has_audio:
                audio_read, audio_write = os.pipe()
            try:
                session.video = playback_video_mode(session.camera_id)
                command = _hls_command(
                    f"pipe:{video_read}",
                    f"pipe:{audio_read}" if audio_read is not None else None,
                    session,
                )
                session.phase = f"Preparing {session.request['quality']} HLS"
                pass_fds = (video_read,) if audio_read is None else (video_read, audio_read)
                session.ffmpeg_process = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=ffmpeg_log,
                    pass_fds=pass_fds,
                )
            finally:
                os.close(video_read)
                if audio_read is not None:
                    os.close(audio_read)
            assert session.capture_process is not None
            feeder_threads.append(
                threading.Thread(
                    target=_feed_growing_file,
                    args=(video_path, video_write, session.capture_process, stop_feeders),
                    name=f"{session.camera_id}-hls-video",
                    daemon=True,
                )
            )
            if session.has_audio and audio_write is not None:
                if session.audio_alignment_in_feeder:
                    feeder_threads.append(
                        threading.Thread(
                            target=_feed_expected_archive_audio,
                            args=(
                                audio_path,
                                directory,
                                audio_write,
                                session.capture_process,
                                stop_feeders,
                                session.camera_id,
                            ),
                            name=f"{session.camera_id}-hls-audio",
                            daemon=True,
                        )
                    )
                else:
                    feeder_threads.append(
                        threading.Thread(
                            target=_feed_growing_file,
                            args=(audio_path, audio_write, session.capture_process, stop_feeders),
                            name=f"{session.camera_id}-hls-audio",
                            daemon=True,
                        )
                    )
            elif audio_mode == "auto" and not learned_audio:
                feeder_threads.append(
                    threading.Thread(
                        target=_watch_for_archive_audio,
                        args=(directory, session.capture_process, stop_feeders, session.camera_id),
                        name=f"{session.camera_id}-audio-capability",
                        daemon=True,
                    )
                )
            for thread in feeder_threads:
                thread.start()
            assert session.ffmpeg_process is not None
            ready_announced = False
            while session.ffmpeg_process.poll() is None:
                if session.stop_event.wait(0.10):
                    session.phase = "Stopping"
                    _terminate_process(session.capture_process)
                    _terminate_process(session.ffmpeg_process)
                    break
                if not ready_announced and session.playlist_ready():
                    ready_announced = True
                    session.status = "playing"
                    session.phase = "Ready to play"
            ffmpeg_rc = session.ffmpeg_process.poll()
            capture_rc = session.capture_process.poll() if session.capture_process else None
            if (
                not session.stop_event.is_set()
                and ffmpeg_rc is not None
                and capture_rc is None
                and session.capture_process is not None
            ):
                # ffmpeg may see EOF before the capture helper exits; let its status win.
                try:
                    capture_rc = session.capture_process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    capture_rc = None

            if session.stop_event.is_set():
                session.status = "stopped"
                session.phase = "Stopped"
            else:
                capture_detail = _tail_text(capture_log_path)
                if capture_rc not in (None, 0):
                    raise RuntimeError(_friendly_capture_error(capture_detail, capture_rc))
                if capture_rc is None:
                    raise RuntimeError(
                        "Camera archive capture failed. "
                        "The media pipeline ended before archive capture completed."
                    )
                if ffmpeg_rc == 0 and capture_rc == 0 and session.playlist_ready():
                    session.status = "complete"
                    session.phase = "Recording range ready"
                else:
                    detail = _stream_failure_detail(directory)
                    raise RuntimeError(
                        f"Archive playback pipeline failed "
                        f"(ffmpeg={ffmpeg_rc}, capture={capture_rc}).\n{detail}"
                    )
    except Exception as error:
        LOG.exception("Playback session %s failed", session.id)
        session.status = "error"
        session.phase = "Failed"
        session.error = str(error)
    finally:
        stop_feeders.set()
        _terminate_process(session.capture_process)
        _terminate_process(session.ffmpeg_process)
        for thread in feeder_threads:
            thread.join(timeout=2)
        timing = _read_live_timing(session.directory) or {}
        gop_seconds = float(timing.get("gop_seconds", 0) or 0)
        if gop_seconds:
            _remember_capability(session.camera_id, "gop_seconds", gop_seconds)
        session.finished_at = time.time()
        lock.release()


FRAME_SESSIONS: dict[str, FrameSession] = {}


def _frame_session(session_id: str, *, touch: bool = True) -> FrameSession:
    with SESSIONS_LOCK:
        value = FRAME_SESSIONS.get(session_id)
        if value is None:
            raise KeyError(f"Unknown stream: {session_id}")
        if touch:
            value.last_access = time.time()
        return value


def _drain(client: TVT9008Client, seconds: float, stop: threading.Event) -> None:
    """The camera drops the socket when a command arrives before its post-login status frames."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not stop.is_set():
        try:
            client.read_frame()
        except TimeoutError:
            pass


def _range_body(start: dt.datetime, stop: dt.datetime) -> bytes:
    # Camera timestamps are local time; the bridge runs in the camera's timezone.
    return TVT9008Client._playback_body(
        int(time.mktime(start.timetuple())), int(time.mktime(stop.timetuple()))
    )


def utc_offset_at(pts_us: int) -> int:
    """The bridge runs in the camera's timezone; DST may differ at the recording's time."""
    offset = dt.datetime.fromtimestamp(pts_us / 1_000_000).astimezone().utcoffset()
    return int(offset.total_seconds()) if offset is not None else 0


def _push_info(session: FrameSession, pts_us: int) -> None:
    session.utc_offset = utc_offset_at(pts_us)
    info = {
        "width": session.width,
        "height": session.height,
        "start": session.request["start"],
        "utc_offset": session.utc_offset,
    }
    session.push(REC_INFO, frame_record(REC_INFO, 0, pts_us, json.dumps(info).encode()))


def _end_of_day(start: dt.datetime) -> dt.datetime:
    return start.replace(hour=23, minute=59, second=59)


def _release_bag(session: FrameSession) -> None:
    with session.ready:
        if session.client is None or session.pending_request_id is not None:
            return
        if not should_release_bag(
            session.bag_pending, session.delivered, session.rendered, session.reader_attached()
        ):
            return
        session.bag_pending = False
        session.bags += 1
        try:
            session.client.send(KIND_PLAYBACK_CONTINUE, session.request_id)
        except OSError:
            pass


def _begin_seek(session: FrameSession) -> None:
    target, session.seek_to = session.seek_to, None
    if target is None or session.client is None:
        return
    session.pending_request_id = session.request_id + 1
    session.phase = "Seeking"
    session.client.send(
        KIND_PLAYBACK_START, session.pending_request_id, _range_body(target, _end_of_day(target))
    )


def _switch_stream(session: FrameSession, transcoder: Transcoder | None) -> None:
    with session.ready:
        session.request_id = session.pending_request_id or session.request_id
        session.pending_request_id = None
        session.queue.clear()
        session.delivered = session.rendered = 0
        session.bag_pending = False
        session.generation += 1
        session.media_us = 0
        session.first_us = 0
        session.last_progress = time.monotonic()
        session.wall_start = time.monotonic()
        session.phase = "Playing"
        session.queue.append(
            (
                REC_MARK,
                frame_record(
                    REC_MARK, 0, 0, json.dumps({"generation": session.generation}).encode()
                ),
            )
        )
        session.ready.notify_all()
    if transcoder is not None:
        transcoder.restart()


class Transcoder:
    """Re-encode the frame stream with ffmpeg, one access unit out per frame in."""

    def __init__(self, session: FrameSession) -> None:
        self.session = session
        self.process: subprocess.Popen[bytes] | None = None
        self.pts: collections.deque[tuple[int, int]] = collections.deque()
        self.thread: threading.Thread | None = None
        self.log = (LOGS / f"stream-{session.id}.log").open("ab")

    def command(self) -> list[str]:
        pre_input, output = video_args("low", ENCODER)
        latency = ["-tune", "zerolatency"] if ENCODER == "software" else ["-async_depth", "1"]
        return [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            *pre_input,
            "-fflags",
            "+genpts",
            "-f",
            "h264",
            "-r",
            "25",
            "-i",
            "pipe:0",
            "-an",
            *output,
            "-g",
            "50",
            "-bf",
            "0",
            *latency,
            "-bsf:v",
            "h264_metadata=aud=insert",
            "-f",
            "h264",
            "pipe:1",
        ]

    def start(self) -> None:
        self.pts.clear()
        self.process = subprocess.Popen(
            self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log
        )
        self.thread = threading.Thread(
            target=self._read,
            args=(self.process,),
            name=f"stream-{self.session.id}-encode",
            daemon=True,
        )
        self.thread.start()

    def restart(self) -> None:
        self.stop()
        self.start()

    def feed(self, payload: bytes, pts_us: int, keyframe: bool) -> None:
        process = self.process
        if process is None or process.stdin is None:
            return
        self.pts.append((pts_us, int(keyframe)))
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            self.session.error = "The encoder stopped"
            self.session.stop_event.set()

    def _read(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stdout is not None
        buffer = bytearray()
        while True:
            chunk = process.stdout.read1(256 * 1024)
            if not chunk:
                return
            buffer.extend(chunk)
            for unit in split_access_units(buffer):
                pts_us, _ = self.pts.popleft() if self.pts else (self.session.position_us, 0)
                flags = KEYFRAME_FLAG if access_unit_is_keyframe(unit) else 0
                if process is self.process:
                    self.session.push(REC_VIDEO, frame_record(REC_VIDEO, flags, pts_us, unit))

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is not None:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except OSError:
                pass
            _terminate_process(process)
        if self.thread is not None:
            self.thread.join(timeout=2)
            self.thread = None

    def close(self) -> None:
        self.stop()
        self.log.close()
        try:
            if Path(self.log.name).stat().st_size == 0:
                Path(self.log.name).unlink()
        except OSError:
            pass


def _pump_frames(session: FrameSession, transcoder: Transcoder | None) -> None:
    client = session.client
    assert client is not None
    while not session.stop_event.is_set():
        if session.seek_to is not None:
            _begin_seek(session)
        if session.idle_seconds() > STREAM_IDLE_SECONDS:
            session.phase = "Nobody is watching"
            return
        if (
            session.last_progress
            and time.monotonic() - session.last_progress > STREAM_PAUSE_LIMIT_SECONDS
        ):
            session.phase = "Paused too long"
            return
        try:
            frame = client.read_frame()
        except TimeoutError:
            _release_bag(session)
            continue
        if frame is None or frame.kind != KIND_RECORDED_MEDIA:
            continue
        if frame.request_id != session.request_id:
            if frame.request_id != session.pending_request_id:
                continue
            _switch_stream(session, transcoder)
        body = frame.body
        if len(body) < MEDIA_HEADER_SIZE:
            continue
        marker = body[3]
        if marker == BAG_END and body[:2] == b"\x00\x00":
            session.bag_pending = True
            _release_bag(session)
            continue
        if marker == END_EVENT and body[:2] == b"\x00\x00":
            session.status = "complete"
            session.phase = "End of the recording"
            return
        media_type, payload, pts_us, keyframe = TVT9008Client._extract_media(frame)
        if media_type == "video":
            if not session.width:
                session.width, session.height = struct.unpack_from("<II", body, 8)
                session.status = "playing"
                session.phase = "Playing"
                session.wall_start = time.monotonic()
            if utc_offset_at(pts_us) != session.utc_offset:
                _push_info(session, pts_us)
            if not session.first_us:
                session.first_us = pts_us
            session.media_us = max(session.media_us, pts_us - session.first_us)
            session.position_us = pts_us
            if transcoder is not None:
                transcoder.feed(payload, pts_us, keyframe)
            else:
                session.push(
                    REC_VIDEO,
                    frame_record(REC_VIDEO, KEYFRAME_FLAG if keyframe else 0, pts_us, payload),
                )
        elif media_type == "audio":
            session.has_audio = True
            session.push(REC_AUDIO, frame_record(REC_AUDIO, 0, pts_us, payload))


def run_frame_session(session: FrameSession) -> None:
    item = camera(session.camera_id)
    slot = camera_session_slot(session.camera_id)
    while not slot.acquire(timeout=0.5):
        if session.stop_event.is_set():
            session.status, session.phase, session.finished_at = "stopped", "Stopped", time.time()
            session.push(REC_END, frame_record(REC_END, 0, 0))
            return
    transcoder: Transcoder | None = None
    try:
        session.started_at = time.time()
        session.status = "running"
        session.phase = "Opening the camera archive"
        start = parse_local_timestamp(str(session.request["start"]))
        stop = start + dt.timedelta(seconds=int(session.request["duration"]))
        client = _metadata_client(item)
        client.connect()
        session.client = client
        _drain(client, 1.5, session.stop_event)
        client.send(KIND_PLAYBACK_START, session.request_id, _range_body(start, stop))
        client.wait_for(KIND_PLAYBACK_START_RESPONSE, session.request_id, 10.0)
        session.video = "copy" if session.request["quality"] == "original" else ENCODER
        if session.video != "copy":
            transcoder = Transcoder(session)
            transcoder.start()
        session.phase = "Waiting for the first frame"
        session.pump_started = time.monotonic()
        _pump_frames(session, transcoder)
        if session.status not in ("complete",):
            session.status = "stopped"
            if session.phase == "Playing":
                session.phase = "Stopped"
    except Exception as error:
        LOG.exception("Stream %s failed", session.id)
        session.status = "error"
        session.phase = "Failed"
        session.error = _friendly_capture_error(str(error))
    finally:
        if transcoder is not None:
            transcoder.close()
        if session.client is not None:
            session.client.close()
        session.finished_at = time.time()
        session.push(
            REC_END,
            frame_record(
                REC_END,
                0,
                session.position_us,
                json.dumps(
                    {"status": session.status, "phase": session.phase, "error": session.error}
                ).encode(),
            ),
        )
        slot.release()
        LOG.info(
            "Stream %s %s after %d bags, %.1f media seconds, speed %s",
            session.id,
            session.status,
            session.bags,
            session.media_us / 1_000_000,
            session.speed(),
        )


def create_frame_session(camera_id: str, request: dict[str, Any]) -> FrameSession:
    camera(camera_id)
    start = parse_local_timestamp(str(request.get("start", "")))
    remaining = int((_end_of_day(start) - start).total_seconds())
    duration = int(request.get("duration", remaining))
    if not 1 <= duration <= max(1, remaining):
        raise ValueError("Duration must be within the day")
    quality = str(request.get("quality", "original"))
    if quality not in QUALITIES:
        raise ValueError("Quality must be original or low")
    value = FrameSession(
        id=uuid.uuid4().hex,
        camera_id=camera_id,
        request={
            "start": start.isoformat(timespec="seconds"),
            "duration": duration,
            "quality": quality,
        },
    )
    with CONFIG_LOCK, SESSIONS_LOCK:
        camera(camera_id)
        for other in FRAME_SESSIONS.values():
            if other.camera_id == camera_id and not other.finished():
                other.stop_event.set()
        FRAME_SESSIONS[value.id] = value
    PLAYBACK_EXECUTOR.submit(run_frame_session, value)
    return value


def control_frame_session(session_id: str, request: dict[str, Any]) -> FrameSession:
    value = _frame_session(session_id)
    if "rendered" in request:
        if int(request.get("generation", value.generation)) == value.generation:
            rendered = int(request["rendered"])
            if rendered > value.rendered:
                value.rendered = rendered
                value.last_progress = time.monotonic()
            _release_bag(value)
    if "seek" in request:
        target = str(request["seek"])
        if re.fullmatch(r"\d{2}:\d{2}:\d{2}", target):
            target = f"{value.request['start'][:10]}T{target}"
        value.seek_to = parse_local_timestamp(target)
    return value


def stop_frame_session(session_id: str) -> FrameSession:
    value = _frame_session(session_id)
    value.stop_event.set()
    return value


def create_playback_session(camera_id: str, request: dict[str, Any]) -> PlaybackSession:
    camera(camera_id)
    start = parse_local_timestamp(str(request.get("start", "")))
    duration = int(request.get("duration", PLAYBACK_MAX_SECONDS))
    if not 5 <= duration <= PLAYBACK_MAX_SECONDS:
        raise ValueError(f"Duration must be 5-{PLAYBACK_MAX_SECONDS} seconds")
    quality = str(request.get("quality", "original"))
    if quality not in QUALITIES:
        raise ValueError("Quality must be original or low")
    gain_db = int(request.get("gain_db", DEFAULT_GAIN))
    if gain_db not in (0, 6, 12, 18, 24):
        raise ValueError("Audio gain must be 0, 6, 12, 18, or 24 dB")
    normalized = {
        "start": start.isoformat(timespec="seconds"),
        "duration": duration,
        "quality": quality,
        "gain_db": gain_db,
    }
    session_id = uuid.uuid4().hex
    directory = WORK / f"hls-{session_id}"
    value = PlaybackSession(
        id=session_id, camera_id=camera_id, request=normalized, work_directory=str(directory)
    )
    with CONFIG_LOCK, SESSIONS_LOCK:
        camera(camera_id)
        SESSIONS[session_id] = value
    PLAYBACK_EXECUTOR.submit(generate_hls_session, value)
    return value


def stop_playback_session(session_id: str) -> PlaybackSession:
    value = _session(session_id)
    value.stop_event.set()
    value.last_access = time.time()
    return value


def clean_sessions() -> None:
    now = time.time()
    remove: list[tuple[str, PlaybackSession]] = []
    with SESSIONS_LOCK:
        for session_id, value in list(SESSIONS.items()):
            if (
                value.status in ("queued", "running", "playing")
                and now - value.last_access > HLS_IDLE_SECONDS
            ):
                value.stop_event.set()
            if value.finished_at and now - value.finished_at > HLS_RETAIN_SECONDS:
                remove.append((session_id, value))
        for session_id, _ in remove:
            SESSIONS.pop(session_id, None)
        for session_id, stream in list(FRAME_SESSIONS.items()):
            if stream.finished_at and now - stream.finished_at > STREAM_RETAIN_SECONDS:
                FRAME_SESSIONS.pop(session_id, None)
    for _, value in remove:
        shutil.rmtree(value.directory, ignore_errors=True)


def cleanup_loop() -> None:
    while True:
        try:
            clean_cache()
            clean_sessions()
        except Exception:
            LOG.exception("Background cleanup failed")
        time.sleep(30)


def announce_to_supervisor() -> None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return
    host = socket.gethostname()
    body = json.dumps(
        {"service": "tvt_archive", "config": {"host": host, "port": PORT, "token": TOKEN}}
    ).encode()
    for delay in (2, 5, 15, 30, 60, 120):
        time.sleep(delay)
        request = urllib.request.Request(
            "http://supervisor/discovery",
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10):
                LOG.info("Announced to the Supervisor as %s:%s", host, PORT)
                return
        except OSError as error:
            LOG.warning("Supervisor discovery failed: %s", error)


def authorized(header: str) -> bool:
    if not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:], TOKEN)


class Handler(BaseHTTPRequestHandler):
    server_version = "TVTArchiveBridge/0.8.4"
    timeout = 60

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s %s", self.address_string(), fmt % args)

    def _parse(self) -> tuple[urllib.parse.SplitResult, dict[str, list[str]]]:
        parsed = urllib.parse.urlsplit(self.path)
        return parsed, urllib.parse.parse_qs(parsed.query)

    def _authorized(self) -> bool:
        return authorized(self.headers.get("Authorization", ""))

    def _headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _json(self, value: Any, status: int = 200, *, head_only: bool = False) -> None:
        payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self._headers()
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)

    def _error(self, status: int, message: str, *, head_only: bool = False) -> None:
        self._json({"error": message, "status": status}, status, head_only=head_only)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > 65536:
            raise ValueError("Invalid JSON body size")
        payload = json.loads(self.rfile.read(length).decode())
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _serve_javascript(self, path: Path, *, head_only: bool = False) -> None:
        if not path.is_file():
            self._error(404, "Player library is not installed", head_only=head_only)
            return
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self._headers()
        self.end_headers()
        if head_only:
            return
        try:
            with path.open("rb") as handle:
                shutil.copyfileobj(handle, self.wfile, length=256 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            LOG.info("Player-library client disconnected")

    def _send_range(
        self, path: Path, content_type: str, headers: dict[str, str], *, head_only: bool
    ) -> None:
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range")
        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
            if not match:
                self._error(416, "Invalid byte range", head_only=head_only)
                return
            if match.group(1):
                start = int(match.group(1))
            if match.group(2):
                end = int(match.group(2))
            if not match.group(1) and match.group(2):
                start, end = max(0, size - int(match.group(2))), size - 1
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            end, status = min(end, size - 1), 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        for key, value in headers.items():
            self.send_header(key, value)
        self._headers()
        self.end_headers()
        if head_only:
            return
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining:
                    block = handle.read(min(1024 * 1024, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError):
            LOG.info("Client disconnected: %s", path.name)

    def _serve_file(
        self, path: Path, filename: str, download: bool, *, head_only: bool = False
    ) -> None:
        if not path.is_file():
            self._error(404, "File not found", head_only=head_only)
            return
        disposition = f'{"attachment" if download else "inline"}; filename="{filename}"'
        self._send_range(
            path,
            "video/mp4",
            {"Cache-Control": "private, max-age=3600", "Content-Disposition": disposition},
            head_only=head_only,
        )

    def _serve_frames(self, session_id: str) -> None:
        session = _frame_session(session_id)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self._headers()
        self.end_headers()
        with session.ready:
            session.readers += 1
        try:
            while True:
                with session.ready:
                    while not session.queue and not session.finished():
                        session.ready.wait(1.0)
                    if not session.queue:
                        return
                    kind, record = session.queue.popleft()
                self.wfile.write(record)
                self.wfile.flush()
                if kind == REC_VIDEO:
                    session.delivered += 1
                    _release_bag(session)
                elif kind == REC_END:
                    return
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            LOG.info("Stream %s viewer disconnected", session_id)
        finally:
            with session.ready:
                session.readers -= 1
                session.reader_left = time.monotonic()

    def _serve_hls_asset(self, session_id: str, asset: str, *, head_only: bool = False) -> None:
        value = _session(session_id)
        if not re.fullmatch(r"(?:index\.m3u8|init\.mp4|segment-\d{5}\.m4s)", asset):
            self._error(404, "Unknown HLS asset", head_only=head_only)
            return
        path = value.directory / asset
        if not path.is_file():
            if value.status == "error":
                self._error(409, value.error or "Playback session failed", head_only=head_only)
            else:
                self._error(425, "Playback media is not ready yet", head_only=head_only)
            return
        content_type = {
            ".m3u8": "application/vnd.apple.mpegurl",
            ".mp4": "video/mp4",
            ".m4s": "video/iso.segment",
        }[path.suffix]
        cache = "no-store" if asset.endswith(".m3u8") else "private, max-age=3600, immutable"
        self._send_range(path, content_type, {"Cache-Control": cache}, head_only=head_only)

    def _route_get(self, *, head_only: bool = False) -> None:
        parsed, query = self._parse()
        path = parsed.path.rstrip("/") or "/"
        if path == "/":
            self._json({"name": "TVT Archive Bridge", "version": APP_VERSION}, head_only=head_only)
            return
        if path == "/api/health":
            self._json(
                {
                    "ok": True,
                    "version": APP_VERSION,
                    "camera_count": len(CAMERAS),
                    "encoder": encoder_info(),
                    "native_session_limit_per_camera": NATIVE_SESSION_LIMIT,
                    "active_jobs": sum(j.status in ("queued", "running") for j in JOBS.values()),
                    "active_playback_sessions": sum(
                        x.status in ("queued", "running", "playing") for x in SESSIONS.values()
                    ),
                    "active_streams": sum(not x.finished() for x in FRAME_SESSIONS.values()),
                },
                head_only=head_only,
            )
            return
        if not self._authorized():
            self._error(401, "Missing or invalid access token", head_only=head_only)
            return
        try:
            if path == "/api/player/hls.js":
                self._serve_javascript(HLS_JS_PATH, head_only=head_only)
                return
            if path == "/api/cameras":
                self._json({"cameras": list_cameras()}, head_only=head_only)
                return
            match = re.fullmatch(r"/api/cameras/([^/]+)", path)
            if match:
                self._json({"camera": safe_camera(match.group(1))}, head_only=head_only)
                return
            match = re.fullmatch(
                r"/api/sessions/([a-f0-9]{32})/(index\.m3u8|init\.mp4|segment-\d{5}\.m4s)", path
            )
            if match:
                self._serve_hls_asset(match.group(1), match.group(2), head_only=head_only)
                return
            match = re.fullmatch(r"/api/sessions/([a-f0-9]{32})", path)
            if match:
                self._json(_session(match.group(1)).public(), head_only=head_only)
                return
            match = re.fullmatch(r"/api/cameras/([^/]+)/(timeline|availability|status)", path)
            if match:
                camera_id, action = match.groups()
                if action == "timeline":
                    day = validate_date(query.get("date", [dt.date.today().isoformat()])[0])
                    force = query.get("refresh", ["0"])[0].lower() in ("1", "true", "yes")
                    self._json(get_timeline(camera_id, day, force=force), head_only=head_only)
                    return
                if action == "availability":
                    self._json(
                        get_availability(camera_id, int(query.get("days", ["45"])[0])),
                        head_only=head_only,
                    )
                    return
                force = query.get("refresh", ["0"])[0].lower() in ("1", "true", "yes")
                self._json(get_status(camera_id, force=force), head_only=head_only)
                return
            match = re.fullmatch(r"/api/streams/([a-f0-9]{32})/frames", path)
            if match:
                if head_only:
                    self._json({"ok": True}, head_only=True)
                    return
                self._serve_frames(match.group(1))
                return
            match = re.fullmatch(r"/api/streams/([a-f0-9]{32})", path)
            if match:
                self._json(_frame_session(match.group(1)).public(), head_only=head_only)
                return
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})(?:/file)?", path)
            if match:
                job = JOBS.get(match.group(1))
                if job is None:
                    self._error(404, "Unknown job", head_only=head_only)
                    return
                if path.endswith("/file"):
                    if job.status != "ready" or not job.output_path:
                        self._error(409, "The file is not ready", head_only=head_only)
                        return
                    self._serve_file(
                        Path(job.output_path),
                        job.output_name or "recording.mp4",
                        query.get("download", ["0"])[0] == "1",
                        head_only=head_only,
                    )
                    return
                self._json(job.public(), head_only=head_only)
                return
            self._error(404, "Not found", head_only=head_only)
        except KeyError as error:
            self._error(404, str(error), head_only=head_only)
        except ValueError as error:
            self._error(400, str(error), head_only=head_only)
        except Exception:
            LOG.exception("GET %s failed", path)
            self._error(500, "Internal bridge error", head_only=head_only)

    def do_GET(self) -> None:
        self._route_get()

    def do_HEAD(self) -> None:
        self._route_get(head_only=True)

    def do_POST(self) -> None:
        parsed, _ = self._parse()
        if not self._authorized():
            self._error(401, "Missing or invalid access token")
            return
        path = parsed.path.rstrip("/")
        try:
            if path == "/api/cameras":
                self._json(add_camera_definition(self._read_json()), 201)
                return
            match = re.fullmatch(r"/api/cameras/([^/]+)/sessions", path)
            if match:
                playback = create_playback_session(match.group(1), self._read_json())
                self._json(playback.public(), 202)
                return
            match = re.fullmatch(r"/api/cameras/([^/]+)/streams", path)
            if match:
                self._json(create_frame_session(match.group(1), self._read_json()).public(), 202)
                return
            match = re.fullmatch(r"/api/streams/([a-f0-9]{32})", path)
            if match:
                self._json(control_frame_session(match.group(1), self._read_json()).public())
                return
            match = re.fullmatch(r"/api/cameras/([^/]+)/jobs", path)
            if match:
                job = create_job(match.group(1), self._read_json())
                self._json(job.public(), 200 if job.status == "ready" else 202)
                return
            self._error(404, "Not found")
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self._error(400, str(error))
        except KeyError as error:
            self._error(404, str(error))
        except Exception:
            LOG.exception("POST %s failed", path)
            self._error(500, "Internal bridge error")

    def do_PUT(self) -> None:
        parsed, _ = self._parse()
        if not self._authorized():
            self._error(401, "Missing or invalid access token")
            return
        path = parsed.path.rstrip("/")
        match = re.fullmatch(r"/api/cameras/([^/]+)", path)
        if not match:
            self._error(404, "Not found")
            return
        try:
            self._json(update_camera_definition(match.group(1), self._read_json()))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self._error(400, str(error))
        except KeyError as error:
            self._error(404, str(error))
        except Exception:
            LOG.exception("PUT %s failed", path)
            self._error(500, "Internal bridge error")

    def do_DELETE(self) -> None:
        parsed, _ = self._parse()
        if not self._authorized():
            self._error(401, "Missing or invalid access token")
            return
        path = parsed.path.rstrip("/")
        stream_match = re.fullmatch(r"/api/streams/([a-f0-9]{32})", path)
        if stream_match:
            try:
                self._json(stop_frame_session(stream_match.group(1)).public())
            except KeyError as error:
                self._error(404, str(error))
            return
        session_match = re.fullmatch(r"/api/sessions/([a-f0-9]{32})", path)
        if session_match:
            try:
                self._json(stop_playback_session(session_match.group(1)).public())
            except KeyError as error:
                self._error(404, str(error))
            return
        match = re.fullmatch(r"/api/cameras/([^/]+)", path)
        if not match:
            self._error(404, "Not found")
            return
        try:
            self._json(delete_camera_definition(match.group(1)))
        except ValueError as error:
            self._error(400, str(error))
        except KeyError as error:
            self._error(404, str(error))
        except Exception:
            LOG.exception("DELETE %s failed", path)
            self._error(500, "Internal bridge error")


if __name__ == "__main__":
    if sys.argv[1:] == ["show-token"]:
        print(TOKEN)
        raise SystemExit(0)
    if sys.argv[1:] not in ([], ["run"]):
        raise SystemExit(f"usage: {sys.argv[0]} [run|show-token]")
    drop_privileges()
    if ENCODER == "vaapi" and not Path(DRI_DEVICE).exists():
        raise SystemExit(
            f"encoder is vaapi but {DRI_DEVICE} does not exist; use software or pass the GPU through"
        )
    threading.Thread(target=cleanup_loop, name="cache-cleanup", daemon=True).start()
    threading.Thread(target=announce_to_supervisor, name="discovery", daemon=True).start()
    clean_cache()
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    LOG.info("TVT Archive Bridge %s listening on %s:%s", APP_VERSION, BIND, PORT)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        for value in list(SESSIONS.values()):
            value.stop_event.set()
        for stream in list(FRAME_SESSIONS.values()):
            stream.stop_event.set()
        EXECUTOR.shutdown(wait=False, cancel_futures=True)
        PLAYBACK_EXECUTOR.shutdown(wait=False, cancel_futures=True)
        server.server_close()

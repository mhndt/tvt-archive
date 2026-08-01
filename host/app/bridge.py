#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import dataclasses
import datetime as dt
import errno
import hashlib
import functools
import ipaddress
import hmac
import json
import logging
import os
import re
import select
import sys
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from native9008 import TVT9008Client

APP_VERSION = "0.8.1"
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
HLS_JS_VERSION = os.environ.get("TVT_ARCHIVE_HLS_JS_VERSION", "1.6.16")

for directory in (CACHE, WORK, INDEX, LOGS):
    directory.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s %(message)s")
LOG = logging.getLogger("tvt-archive")

CONFIG_LOCK = threading.RLock()
with CONFIG_PATH.open("r", encoding="utf-8") as handle:
    CONFIG = json.load(handle)

SERVER = CONFIG.get("server", {})
PROCESSING = CONFIG.get("processing", {})
CAMERAS: dict[str, dict[str, Any]] = {str(item["id"]): dict(item) for item in CONFIG.get("cameras", [])}

TOKEN = str(SERVER["token"])
BIND = str(SERVER.get("bind", "0.0.0.0"))
PORT = int(SERVER.get("port", 8099))
CACHE_HOURS = int(PROCESSING.get("cache_hours", 6))
DEFAULT_GAIN = int(PROCESSING.get("default_gain_db", 0))
PLAYBACK_MAX_SECONDS = int(PROCESSING.get("playback_max_seconds", 900))
DOWNLOAD_MAX_SECONDS = int(PROCESSING.get("download_max_seconds", 3600))
ACCELERATOR_PREFERENCE = str(PROCESSING.get("accelerator", os.environ.get("TVT_ARCHIVE_ACCELERATOR", "auto"))).lower()
VAAPI_DRIVER_PREFERENCE = str(PROCESSING.get("vaapi_driver", os.environ.get("TVT_ARCHIVE_VAAPI_DRIVER", "auto"))).strip() or "auto"
DRI_DEVICE = str(PROCESSING.get("dri_device", os.environ.get("TVT_ARCHIVE_DRI_DEVICE", "/dev/dri/renderD128")))
STREAM_AUDIO_DELAY_MS = int(PROCESSING.get("stream_audio_delay_ms", os.environ.get("TVT_ARCHIVE_STREAM_AUDIO_DELAY_MS", 0)))
MAX_WORKERS = max(1, min(int(PROCESSING.get("max_parallel_jobs", 1)), 4))
NATIVE_SESSION_LIMIT = max(
    1, min(int(PROCESSING.get("max_native_sessions_per_camera", 2)), 4)
)

CAMERA_LOCKS: dict[str, threading.Lock] = {camera_id: threading.Lock() for camera_id in CAMERAS}
CAMERA_SESSION_SLOTS: dict[str, threading.BoundedSemaphore] = {
    camera_id: threading.BoundedSemaphore(NATIVE_SESSION_LIMIT) for camera_id in CAMERAS
}
METADATA_LOCKS: dict[str, threading.Lock] = {camera_id: threading.Lock() for camera_id in CAMERAS}
JOBS_LOCK = threading.Lock()
JOBS: dict[str, "Job"] = {}
CACHE_TO_JOB: dict[str, str] = {}
EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="camera-job")
PLAYBACK_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=max(1, min(int(PROCESSING.get("max_parallel_playback_sessions", 2)), 4)),
    thread_name_prefix="playback-session",
)
SESSIONS_LOCK = threading.RLock()
SESSIONS: dict[str, "PlaybackSession"] = {}
HLS_IDLE_SECONDS = max(60, int(PROCESSING.get("hls_idle_seconds", 300)))
HLS_RETAIN_SECONDS = max(300, int(PROCESSING.get("hls_retain_seconds", 1800)))
HLS_SEGMENT_SECONDS = max(1, min(int(PROCESSING.get("hls_segment_seconds", 1)), 6))
HLS_START_BUFFER_SECONDS = max(
    2, min(int(PROCESSING.get("hls_start_buffer_seconds", 3)), 30)
)
HLS_TIMING_SAMPLE_FRAMES = max(
    8, min(int(PROCESSING.get("hls_timing_sample_frames", 10)), 120)
)
HLS_FIRST_MEDIA_TIMEOUT_SECONDS = max(
    5.0, min(float(PROCESSING.get("hls_first_media_timeout_seconds", 15.0)), 60.0)
)
HLS_TIMING_MAX_SECONDS = max(
    0.75, min(float(PROCESSING.get("hls_timing_max_seconds", 2.5)), 10.0)
)
HLS_AUDIO_DETECT_SECONDS = max(
    0.25,
    min(
        float(PROCESSING.get("hls_audio_detect_seconds", 1.0)),
        HLS_TIMING_MAX_SECONDS,
    ),
)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}$")
CAMERA_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


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
    accelerator_used: str = "copy"
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
            "accelerator_used": self.accelerator_used,
            "qsv_used": self.accelerator_used.startswith("intel_qsv"),
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
    accelerator_used: str = "copy"
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
    playlist_announced: bool = False

    def segment_count(self) -> int:
        try:
            return sum(1 for _ in self.directory.glob("segment-*.m4s"))
        except OSError:
            return 0

    def buffered_seconds(self) -> float:
        """Return real HLS media duration, including long copy-mode GOP segments."""
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
        # Once the playlist has been exposed, keep it exposed. FFmpeg replaces
        # the event playlist atomically while adding segments, and a transient
        # read/stat race must not make Home Assistant withdraw the signed URL
        # and cause the browser player to be recreated.
        if self.playlist_announced:
            return True
        try:
            if self.playlist_path.stat().st_size <= 40:
                return False
            count = self.segment_count()
            buffered = self.buffered_seconds()
            ready = buffered >= HLS_START_BUFFER_SECONDS or (self.status == "complete" and count > 0)
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
            "accelerator_used": self.accelerator_used,
            "segment_seconds": HLS_SEGMENT_SECONDS,
            "segment_count": self.segment_count(),
            "buffered_seconds": round(self.buffered_seconds(), 3),
            "start_buffer_seconds": HLS_START_BUFFER_SECONDS,
            "source_fps": round(self.source_fps, 4),
            "audio_offset_ms": self.audio_offset_ms,
            "has_audio": self.has_audio,
            "mime_type": ('video/mp4; codecs="avc1.640029, mp4a.40.2"' if self.has_audio
                          else 'video/mp4; codecs="avc1.640029"'),
        }


def camera(camera_id: str) -> dict[str, Any]:
    with CONFIG_LOCK:
        if camera_id not in CAMERAS:
            raise KeyError(f"Unknown camera: {camera_id}")
        return dict(CAMERAS[camera_id])


def camera_lock(camera_id: str) -> threading.Lock:
    with CONFIG_LOCK:
        if camera_id not in CAMERA_LOCKS:
            raise KeyError(f"Unknown camera: {camera_id}")
        return CAMERA_LOCKS[camera_id]


def camera_session_slot(camera_id: str) -> threading.BoundedSemaphore:
    """Return the per-camera archive session gate.

    Two native TCP/9008 sessions are permitted by default because real-camera
    testing proved that playback and one export can run together reliably.
    """
    with CONFIG_LOCK:
        if camera_id not in CAMERA_SESSION_SLOTS:
            raise KeyError(f"Unknown camera: {camera_id}")
        return CAMERA_SESSION_SLOTS[camera_id]


def metadata_lock(camera_id: str) -> threading.Lock:
    with CONFIG_LOCK:
        if camera_id not in METADATA_LOCKS:
            raise KeyError(f"Unknown camera: {camera_id}")
        return METADATA_LOCKS[camera_id]


def _public_live_profiles(item: dict[str, Any]) -> list[dict[str, Any]]:
    profiles = item.get("live_profiles")
    if not isinstance(profiles, list):
        # v0.6.x migration: fixed quality keys become ordinary named profiles.
        streams = item.get("live_streams", {})
        profiles = [
            {"id": quality, "name": label, "entity_id": streams.get(quality),
             "default": quality == "high"}
            for quality, label in (("high", "High"), ("balanced", "Balanced"),
                                   ("data_saver", "Data Saver"))
            if isinstance(streams, dict) and streams.get(quality)
        ]
    return [
        {
            "id": str(profile.get("id", "")),
            "name": str(profile.get("name", profile.get("id", "Live"))),
            "entity_id": str(profile.get("entity_id", "")),
            "default": bool(profile.get("default", False)),
        }
        for profile in profiles if isinstance(profile, dict) and profile.get("entity_id")
    ]


def safe_camera(camera_id: str) -> dict[str, Any]:
    item = camera(camera_id)
    return {
        "id": camera_id,
        "name": str(item.get("name", camera_id)),
        "host": str(item["host"]),
        "port": int(item.get("port", 9008)),
        "channel": int(item.get("channel", 0)),
        "username": str(item.get("username", "")),
        "archive_backend": str(item.get("archive_backend", "native_9008")),
        "rtsp_port": int(item.get("rtsp_port", 554)),
        "rtsp_stream_type": str(item.get("rtsp_stream_type", "main")),
        "rtsp_transport": str(item.get("rtsp_transport", "tcp")),
        "rtsp_fps": float(item.get("rtsp_fps", 25.0)),
        "live_profiles": _public_live_profiles(item),
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
    if not re.fullmatch(r"(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", host):
        raise ValueError("Camera host must be an IP address or valid hostname")
    return host


def _normalize_live_profiles(payload: dict[str, Any], existing: dict[str, Any]) -> list[dict[str, Any]]:
    supplied = payload.get("live_profiles")
    if supplied is None:
        if isinstance(existing.get("live_profiles"), list):
            supplied = existing.get("live_profiles")
        else:
            streams = existing.get("live_streams", {})
            supplied = [
                {"id": quality, "name": label, "entity_id": streams.get(quality),
                 "default": quality == "high"}
                for quality, label in (("high", "High"), ("balanced", "Balanced"),
                                       ("data_saver", "Data Saver"))
                if isinstance(streams, dict) and streams.get(quality)
            ]
    if not isinstance(supplied, list):
        raise ValueError("Live profiles must be a list")
    profiles: list[dict[str, Any]] = []
    seen: set[str] = set()
    default_seen = False
    for position, raw in enumerate(supplied):
        if not isinstance(raw, dict):
            raise ValueError("Each live profile must be an object")
        name = str(raw.get("name", "")).strip()
        entity_id = str(raw.get("entity_id", "")).strip()
        if not name or len(name) > 40:
            raise ValueError("Live profile names must contain 1-40 characters")
        if not re.fullmatch(r"camera\.[a-zA-Z0-9_]+", entity_id):
            raise ValueError(f"Live profile {name!r} must reference a camera.* entity")
        profile_id = str(raw.get("id") or camera_slug(name))[:48]
        if not CAMERA_ID_RE.fullmatch(profile_id) or profile_id in seen:
            profile_id = f"profile_{position + 1}"
        seen.add(profile_id)
        is_default = bool(raw.get("default", False)) and not default_seen
        default_seen = default_seen or is_default
        profiles.append({"id": profile_id, "name": name, "entity_id": entity_id,
                         "default": is_default})
    if profiles and not default_seen:
        profiles[0]["default"] = True
    return profiles


def normalize_camera_definition(payload: dict[str, Any], existing: dict[str, Any] | None = None,
                                *, fixed_id: str | None = None) -> dict[str, Any]:
    existing = dict(existing or {})
    name = str(payload.get("name", existing.get("name", ""))).strip()
    if not name or len(name) > 80:
        raise ValueError("Camera name is required and must be 80 characters or fewer")
    camera_id = fixed_id or str(payload.get("id") or existing.get("id") or next_camera_id(name))
    if not CAMERA_ID_RE.fullmatch(camera_id):
        raise ValueError("Camera ID may contain only letters, numbers, underscores, and hyphens")
    backend = str(payload.get("archive_backend", existing.get("archive_backend", "native_9008")))
    if backend not in ("native_9008", "rtsp"):
        raise ValueError("Archive backend must be native_9008 or rtsp")
    port = int(payload.get("port", existing.get("port", 9008)))
    rtsp_port = int(payload.get("rtsp_port", existing.get("rtsp_port", 554)))
    channel = int(payload.get("channel", existing.get("channel", 0)))
    rtsp_fps = float(payload.get("rtsp_fps", existing.get("rtsp_fps", 25.0)))
    rtsp_stream_type = str(payload.get("rtsp_stream_type", existing.get("rtsp_stream_type", "main")))
    rtsp_transport = str(payload.get("rtsp_transport", existing.get("rtsp_transport", "tcp")))
    if not 1 <= port <= 65535 or not 1 <= rtsp_port <= 65535:
        raise ValueError("Camera ports must be between 1 and 65535")
    if not 0 <= channel <= 255:
        raise ValueError("Camera channel must be between 0 and 255")
    if not 5 <= rtsp_fps <= 120:
        raise ValueError("Recorded RTSP FPS must be between 5 and 120")
    if rtsp_stream_type not in ("main", "sub"):
        raise ValueError("Recorded RTSP stream must be main or sub")
    if rtsp_transport not in ("tcp", "udp"):
        raise ValueError("Recorded RTSP transport must be tcp or udp")
    username = str(payload.get("username", existing.get("username", ""))).strip()
    supplied_password = payload.get("password")
    password = str(supplied_password) if supplied_password not in (None, "") else str(existing.get("password", ""))
    if not username or not password:
        raise ValueError("Camera username and password are required")
    return {
        "id": camera_id,
        "name": name,
        "host": normalize_host(payload.get("host", existing.get("host"))),
        "port": port,
        "rtsp_port": rtsp_port,
        "channel": channel,
        "username": username,
        "password": password,
        "archive_backend": backend,
        "rtsp_stream_type": rtsp_stream_type,
        "rtsp_transport": rtsp_transport,
        "rtsp_fps": rtsp_fps,
        "live_profiles": _normalize_live_profiles(payload, existing),
    }


def capture_environment(item: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "TVT_ARCHIVE_BACKEND": str(item.get("archive_backend", "native_9008")),
        "TVT_HOST": str(item.get("connect_host", item["host"])),
        "TVT_PORT": str(item.get("connect_port", item.get("port", 9008))),
        "TVT_RTSP_PORT": str(item.get("rtsp_port", 554)),
        "TVT_RTSP_STREAM_TYPE": str(item.get("rtsp_stream_type", "main")),
        "TVT_RTSP_TRANSPORT": str(item.get("rtsp_transport", "tcp")),
        "TVT_RTSP_FPS": str(item.get("rtsp_fps", 25.0)),
        "TVT_USER": str(item["username"]),
        "TVT_PASSWORD": str(item["password"]),
        "TVT_CHANNEL": str(item.get("channel", 0)),
    })
    return env


def _metadata_client(item: dict[str, Any]) -> TVT9008Client:
    return TVT9008Client(
        str(item.get("connect_host", item["host"])),
        int(item.get("connect_port", item.get("port", 9008))),
        str(item["username"]), str(item["password"]), timeout=8.0,
    )


def run_command(command: list[str], *, timeout: int, log_path: Path | None = None,
                env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    LOG.info("Running %s", " ".join(command[:2]) + (" …" if len(command) > 2 else ""))
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, timeout=timeout, check=False, env=env)
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
            command, stdout=log_handle, stderr=subprocess.STDOUT, env=env,
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
    started = time.monotonic()
    timing_path = work_directory / "timing.json"

    def monitor() -> None:
        timing = _read_json_if_ready(timing_path)
        captured = min(float(duration), _captured_span_seconds(timing))
        if captured <= 0 and str(env.get("TVT_ARCHIVE_BACKEND")) == "rtsp":
            captured = min(float(duration), time.monotonic() - started)
        fraction = min(1.0, captured / max(1, duration))
        update_job(
            job, progress=min(0.92, fraction * 0.92),
            captured_seconds=captured, remaining_seconds=max(0.0, duration - captured),
        )

    _run_logged_process(
        command, timeout=timeout, log_path=log_path, env=env, monitor=monitor,
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
            job, progress=0.92 + fraction * 0.07,
            processed_seconds=processed, remaining_seconds=0.0,
        )

    _run_logged_process(
        monitored_command, timeout=timeout, log_path=log_path, monitor=monitor,
    )
    monitor()
    update_job(job, progress=0.99, processed_seconds=float(duration), remaining_seconds=0.0)


def camera_has_active_jobs(camera_id: str) -> bool:
    with JOBS_LOCK:
        jobs = any(job.camera_id == camera_id and job.status in ("queued", "running")
                   for job in JOBS.values())
    with SESSIONS_LOCK:
        sessions = any(value.camera_id == camera_id and value.status in ("queued", "running", "playing")
                       for value in SESSIONS.values())
    return jobs or sessions


def test_camera_definition(item: dict[str, Any]) -> dict[str, Any]:
    now = dt.datetime.now()
    start = now - dt.timedelta(minutes=10)
    with _metadata_client(item) as client:
        segments = client.search(start, now)
        dates = client.recording_dates()
    return {"online": True, "segments_found": len(segments),
            "recording_dates": len(dates),
            "archive_backend": item.get("archive_backend", "native_9008")}

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
        CAMERA_LOCKS = {camera_id: old_locks.get(camera_id, threading.Lock()) for camera_id in CAMERAS}
        CAMERA_SESSION_SLOTS = {
            camera_id: old_session_slots.get(
                camera_id, threading.BoundedSemaphore(NATIVE_SESSION_LIMIT)
            )
            for camera_id in CAMERAS
        }
        METADATA_LOCKS = {
            camera_id: old_metadata_locks.get(camera_id, threading.Lock())
            for camera_id in CAMERAS
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
    connection_keys = {
        "host", "port", "rtsp_port", "channel", "username", "password",
        "archive_backend", "rtsp_stream_type", "rtsp_transport", "rtsp_fps",
    }
    if any(item.get(key) != existing.get(key) for key in connection_keys):
        with camera_lock(camera_id):
            test = test_camera_definition(item)
    else:
        test = {"online": True, "connection_test_skipped": True}
    with CONFIG_LOCK:
        updated = {camera_id_: dict(value) for camera_id_, value in CAMERAS.items()}
        if camera_id not in updated:
            raise KeyError(f"Unknown camera: {camera_id}")
        updated[camera_id] = item
    persist_camera_map(updated)
    shutil.rmtree(INDEX / camera_id, ignore_errors=True)
    return {"camera": safe_camera(camera_id), "test": test}


def delete_camera_definition(camera_id: str) -> dict[str, Any]:
    existing = safe_camera(camera_id)
    if camera_has_active_jobs(camera_id):
        raise ValueError("Wait for this camera's active playback/download job to finish")
    lock = camera_lock(camera_id)
    with lock:
        with CONFIG_LOCK:
            updated = {camera_id_: dict(value) for camera_id_, value in CAMERAS.items()
                       if camera_id_ != camera_id}
            if len(updated) == len(CAMERAS):
                raise KeyError(f"Unknown camera: {camera_id}")
        persist_camera_map(updated)
    shutil.rmtree(INDEX / camera_id, ignore_errors=True)
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


def json_dump_atomic(path: Path, value: Any, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    if mode is not None:
        os.chmod(temporary, mode)
    os.replace(temporary, path)


def promote_completed_file(source: Path, destination: Path, *, mode: int = 0o600) -> None:
    """Publish a completed file atomically, including across Docker volumes.

    ``os.replace`` is atomic but raises EXDEV when /state and /cache are separate
    mounts.  In that case copy into a temporary file *inside the destination
    directory*, flush it, and atomically rename that temporary file into place.
    Readers therefore never observe a partially copied export.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, destination)
        os.chmod(destination, mode)
        return
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise

    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
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


def merge_segments(raw_segments: list[dict[str, Any]], query_start: dt.datetime,
                   query_stop: dt.datetime) -> dict[str, Any]:
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
        "merged_ranges": [{"start": start.isoformat(timespec="seconds"),
                           "stop": stop.isoformat(timespec="seconds")} for start, stop in merged],
        "gaps": [{"start": start.isoformat(timespec="seconds"),
                  "stop": stop.isoformat(timespec="seconds"),
                  "seconds": int((stop - start).total_seconds())} for start, stop in gaps],
        "recorded_seconds": int(recorded_seconds),
        "recorded_hours": round(recorded_seconds / 3600, 2),
    }


def search_window(camera_id: str, start: dt.datetime, stop: dt.datetime, *,
                  cache_name: str | None = None, ttl: int = 60) -> dict[str, Any]:
    item = camera(camera_id)
    camera_index = INDEX / camera_id
    camera_index.mkdir(parents=True, exist_ok=True)
    cache_path = camera_index / f"{cache_name}.json" if cache_name else None
    if cache_path and cache_path.exists() and time.time() - cache_path.stat().st_mtime < ttl:
        return json.loads(cache_path.read_text(encoding="utf-8"))

    lock = metadata_lock(camera_id)
    if not lock.acquire(blocking=False):
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
        "channel": int(item.get("channel", 0)),
        "archive_backend": str(item.get("archive_backend", "native_9008")),
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        **merge_segments(raw_segments, start, stop),
    }
    if cache_path:
        json_dump_atomic(cache_path, result)
    return result

def get_timeline(camera_id: str, day: dt.date, *, force: bool = False) -> dict[str, Any]:
    start = dt.datetime.combine(day, dt.time.min)
    stop = start + dt.timedelta(days=1)
    result = search_window(camera_id, start, stop, cache_name=f"day-{day.isoformat()}",
                           ttl=0 if force else 60)
    now = dt.datetime.now()
    result["recording_now"] = (day == now.date() and any(
        dt.datetime.fromisoformat(item["start"]) <= now <=
        dt.datetime.fromisoformat(item["stop"]) + dt.timedelta(seconds=120)
        for item in result["merged_ranges"]))
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


def get_device_info(camera_id: str, *, force: bool = False) -> dict[str, Any]:
    # The pure-Python interoperability backend currently implements archive
    # metadata and playback only. Storage detail is deliberately reported as
    # unavailable rather than retaining the proprietary SDK dependency.
    item = camera(camera_id)
    return {
        "supported": False,
        "disk_status": "unknown",
        "disk_total_mb": 0,
        "disks": [],
        "archive_backend": str(item.get("archive_backend", "native_9008")),
        "detail": "Storage details are not exposed by the current read-only protocol backend.",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }

def get_status(camera_id: str, *, force: bool = False) -> dict[str, Any]:
    try:
        today = get_timeline(camera_id, dt.date.today(), force=force)
        availability = get_availability(camera_id, int(PROCESSING.get("availability_days", 45)))
        device = get_device_info(camera_id, force=force)
        return {"camera": safe_camera(camera_id), "online": True, "timeline_today": today,
                "availability": availability, "device": device,
                "accelerator": acceleration_capabilities()}
    except Exception as error:
        LOG.exception("Status failed for %s", camera_id)
        return {"camera": safe_camera(camera_id), "online": False, "error": str(error),
                "timeline_today": {}, "availability": {}, "device": {},
                "accelerator": acceleration_capabilities()}


def ffmpeg_version() -> str:
    try:
        completed = subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, timeout=10, check=False)
        return completed.stdout.splitlines()[0].strip() if completed.stdout else "unavailable"
    except Exception:
        return "unavailable"


@functools.lru_cache(maxsize=1)
def ffmpeg_encoders() -> set[str]:
    try:
        completed = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, timeout=15, check=False)
        return {match.group(1) for line in completed.stdout.splitlines()
                if (match := re.match(r"^\s*[A-Z.]{6}\s+([^\s]+)", line))}
    except Exception:
        return set()


def _device_accessible(path: str) -> bool:
    return Path(path).exists() and os.access(path, os.R_OK | os.W_OK)


def dri_vendor() -> str:
    render_name = Path(DRI_DEVICE).name
    vendor_path = Path("/sys/class/drm") / render_name / "device" / "vendor"
    try:
        value = vendor_path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return "unknown"
    return {"0x8086": "intel", "0x1002": "amd"}.get(value, value)


@functools.lru_cache(maxsize=None)
def vaapi_runtime_driver_info(driver: str = "") -> str | None:
    if not _device_accessible(DRI_DEVICE):
        return None
    environment = os.environ.copy()
    if driver:
        environment["LIBVA_DRIVER_NAME"] = driver
    else:
        environment.pop("LIBVA_DRIVER_NAME", None)
    try:
        completed = subprocess.run(
            ["vainfo", "--display", "drm", "--device", DRI_DEVICE],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=15, check=False, env=environment,
        )
    except Exception:
        return None
    for line in completed.stdout.splitlines():
        cleaned = line.strip()
        if "Driver version:" in cleaned:
            return cleaned.split("Driver version:", 1)[1].strip()
        if "VAAPI driver:" in cleaned:
            return cleaned.split("VAAPI driver:", 1)[1].strip()
    return None


@functools.lru_cache(maxsize=None)
def encoder_works(candidate: str, vaapi_driver: str = "") -> bool:
    """Probe a complete decode, resize, and encode path before selecting it."""
    probe_dir = Path(tempfile.mkdtemp(prefix="accelerator-probe-", dir=WORK))
    source = probe_dir / "source.h264"
    environment = os.environ.copy()
    if vaapi_driver:
        environment["LIBVA_DRIVER_NAME"] = vaapi_driver
    else:
        environment.pop("LIBVA_DRIVER_NAME", None)
    try:
        generated = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "testsrc2=size=1920x1080:rate=25", "-frames:v", "30",
             "-pix_fmt", "yuvj420p", "-c:v", "libx264", "-preset", "ultrafast",
             "-profile:v", "high", "-level:v", "4.1", "-f", "h264", str(source)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45, check=False,
            env=environment,
        )
        if generated.returncode != 0 or not source.exists():
            return candidate == "software" and "libx264" in ffmpeg_encoders()
        input_args = ["-f", "h264", "-i", str(source), "-frames:v", "20", "-an"]
        if candidate == "vaapi_full":
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-init_hw_device", f"vaapi=va:{DRI_DEVICE}", "-filter_hw_device", "va",
                "-hwaccel", "vaapi", "-hwaccel_device", "va",
                "-hwaccel_output_format", "vaapi", *input_args,
                "-vf", "scale_vaapi=w=1280:h=720:format=nv12",
                "-c:v", "h264_vaapi", "-profile:v", "high",
                "-b:v", "2500k", "-maxrate", "3000k", "-bufsize", "5000k",
                "-g", "50", "-f", "null", "-",
            ]
        elif candidate == "intel_qsv_full":
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-init_hw_device", f"vaapi=va:{DRI_DEVICE}",
                "-init_hw_device", "qsv=qs@va", "-filter_hw_device", "qs",
                "-hwaccel", "qsv", "-hwaccel_device", "qs",
                "-hwaccel_output_format", "qsv", *input_args,
                "-vf", "vpp_qsv=w=1280:h=720", "-c:v", "h264_qsv",
                "-global_quality", "24", "-look_ahead", "0", "-g", "50",
                "-f", "null", "-",
            ]
        elif candidate == "nvidia_full":
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-hwaccel", "cuda", "-hwaccel_output_format", "cuda", *input_args,
                "-vf", "scale_cuda=1280:720", "-c:v", "h264_nvenc",
                "-preset", "p4", "-cq", "24", "-b:v", "0", "-g", "50",
                "-f", "null", "-",
            ]
        elif candidate == "vaapi_hybrid":
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-init_hw_device", f"vaapi=va:{DRI_DEVICE}", "-filter_hw_device", "va",
                *input_args, "-vf", "scale=1280:720:flags=fast_bilinear,format=nv12,hwupload",
                "-c:v", "h264_vaapi", "-qp", "24", "-g", "50", "-f", "null", "-",
            ]
        elif candidate == "nvidia_hybrid":
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", *input_args,
                "-vf", "scale=1280:720:flags=fast_bilinear", "-c:v", "h264_nvenc",
                "-preset", "p4", "-cq", "24", "-b:v", "0", "-g", "50",
                "-f", "null", "-",
            ]
        elif candidate == "software":
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", *input_args,
                "-vf", "scale=1280:720:flags=fast_bilinear", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "24", "-g", "50",
                "-f", "null", "-",
            ]
        else:
            return False
        return subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=45, check=False, env=environment,
        ).returncode == 0
    except Exception:
        return False
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)

def _vaapi_driver_candidates() -> list[str]:
    if VAAPI_DRIVER_PREFERENCE.lower() not in ("auto", "default"):
        return [VAAPI_DRIVER_PREFERENCE]
    vendor = dri_vendor()
    if vendor == "intel":
        values = [os.environ.get("LIBVA_DRIVER_NAME", ""), "iHD", "i965", ""]
    else:
        values = [os.environ.get("LIBVA_DRIVER_NAME", ""), ""]
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result

def acceleration_capabilities() -> dict[str, Any]:
    encoders = ffmpeg_encoders()
    available: list[str] = []
    vendor = dri_vendor()
    dri_ok = _device_accessible(DRI_DEVICE)
    selected_vaapi_driver = ""

    qsv_driver = next((driver for driver in _vaapi_driver_candidates()
                       if vendor == "intel" and dri_ok and "h264_qsv" in encoders
                       and encoder_works("intel_qsv_full", driver)), None)
    if qsv_driver is not None:
        available.append("intel_qsv_full")

    full_driver = next((driver for driver in _vaapi_driver_candidates()
                        if dri_ok and "h264_vaapi" in encoders
                        and encoder_works("vaapi_full", driver)), None)
    if full_driver is not None:
        available.append("vaapi_full")
        selected_vaapi_driver = full_driver

    if (Path("/dev/nvidia0").exists() or os.environ.get("NVIDIA_VISIBLE_DEVICES") not in (None, "", "void")) and "h264_nvenc" in encoders and encoder_works("nvidia_full"):
        available.append("nvidia_full")

    hybrid_driver = next((driver for driver in _vaapi_driver_candidates()
                          if dri_ok and "h264_vaapi" in encoders
                          and encoder_works("vaapi_hybrid", driver)), None)
    if hybrid_driver is not None:
        available.append("vaapi_hybrid")
        if not selected_vaapi_driver:
            selected_vaapi_driver = hybrid_driver

    if "h264_nvenc" in encoders and encoder_works("nvidia_hybrid"):
        available.append("nvidia_hybrid")
    if "libx264" in encoders and encoder_works("software"):
        available.append("software")

    aliases = {
        "qsv": "intel_qsv_full", "intel": "intel_qsv_full",
        "vaapi": "vaapi_full", "nvidia": "nvidia_full", "cpu": "software",
    }
    preference = aliases.get(ACCELERATOR_PREFERENCE, ACCELERATOR_PREFERENCE)
    selected = "none"
    order = ("vaapi_full", "intel_qsv_full", "nvidia_full", "vaapi_hybrid", "nvidia_hybrid", "software")
    if preference == "auto":
        selected = next((candidate for candidate in order if candidate in available), "none")
    elif preference in available:
        selected = preference
    elif preference not in ("none", "off"):
        selected = next((candidate for candidate in order if candidate in available), "none")

    if selected.startswith("vaapi") and selected_vaapi_driver:
        os.environ["LIBVA_DRIVER_NAME"] = selected_vaapi_driver
    elif selected == "intel_qsv_full" and qsv_driver:
        os.environ["LIBVA_DRIVER_NAME"] = qsv_driver

    runtime_driver = (
        vaapi_runtime_driver_info(
            selected_vaapi_driver if selected.startswith("vaapi") else (qsv_driver or "")
        )
        if selected in ("vaapi_full", "vaapi_hybrid", "intel_qsv_full") else None
    )
    reported_vaapi_driver = selected_vaapi_driver or None
    if not reported_vaapi_driver and runtime_driver:
        if "iHD" in runtime_driver:
            reported_vaapi_driver = "iHD"
        elif "i965" in runtime_driver:
            reported_vaapi_driver = "i965"
        elif "radeonsi" in runtime_driver.lower():
            reported_vaapi_driver = "radeonsi"

    return {
        "preference": ACCELERATOR_PREFERENCE,
        "selected": selected,
        "available": available,
        "dri_device": DRI_DEVICE if Path(DRI_DEVICE).exists() else None,
        "dri_vendor": vendor,
        "vaapi_driver_preference": VAAPI_DRIVER_PREFERENCE,
        "vaapi_driver": reported_vaapi_driver,
        "vaapi_runtime_driver": runtime_driver,
        "packaged_intel_media_driver": os.environ.get(
            "TVT_ARCHIVE_INTEL_MEDIA_DRIVER_VERSION"
        ),
        "ffmpeg_version": ffmpeg_version(),
        "recording_playback_qualities": ["original", "balanced", "data_saver"],
        "playback_transport": "low_latency_hls_fmp4",
        "downloads": "original",
    }


def transcode_video_args(quality: str) -> tuple[list[str], list[str], str]:
    """Return pre-input args, output args, and the selected file pipeline."""
    if quality == "original":
        return [], ["-c:v", "copy"], "copy"
    selected = str(acceleration_capabilities().get("selected", "none"))
    width = 1280 if quality == "balanced" else 854
    height = 720 if quality == "balanced" else 480
    cq = "24" if quality == "balanced" else "29"
    bitrate, maxrate, bufsize = (("2500k", "3000k", "5000k") if quality == "balanced"
                                 else ("900k", "1200k", "2400k"))
    if selected == "vaapi_full":
        pre = ["-init_hw_device", f"vaapi=va:{DRI_DEVICE}", "-filter_hw_device", "va",
               "-hwaccel", "vaapi", "-hwaccel_device", "va",
               "-hwaccel_output_format", "vaapi"]
        out = ["-vf", f"scale_vaapi=w={width}:h={height}:format=nv12",
               "-c:v", "h264_vaapi", "-profile:v", "high",
               "-b:v", bitrate, "-maxrate", maxrate, "-bufsize", bufsize, "-g", "50"]
        return pre, out, selected
    if selected == "intel_qsv_full":
        pre = ["-init_hw_device", f"vaapi=va:{DRI_DEVICE}", "-init_hw_device", "qsv=qs@va",
               "-filter_hw_device", "qs", "-hwaccel", "qsv", "-hwaccel_device", "qs",
               "-hwaccel_output_format", "qsv"]
        out = ["-vf", f"vpp_qsv=w={width}:h={height}", "-c:v", "h264_qsv",
               "-global_quality", cq, "-look_ahead", "0", "-g", "50"]
        return pre, out, selected
    if selected == "nvidia_full":
        pre = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        out = ["-vf", f"scale_cuda={width}:{height}", "-c:v", "h264_nvenc",
               "-preset", "p4", "-cq", cq, "-b:v", "0", "-g", "50"]
        return pre, out, selected
    if selected == "vaapi_hybrid":
        pre = ["-init_hw_device", f"vaapi=va:{DRI_DEVICE}", "-filter_hw_device", "va"]
        out = ["-vf", f"scale={width}:{height}:flags=fast_bilinear,format=nv12,hwupload",
               "-c:v", "h264_vaapi", "-qp", cq, "-g", "50"]
        return pre, out, selected
    if selected == "nvidia_hybrid":
        return [], ["-vf", f"scale={width}:{height}:flags=fast_bilinear",
                    "-c:v", "h264_nvenc", "-preset", "p4", "-cq", cq,
                    "-b:v", "0", "-g", "50"], selected
    if selected == "software":
        return [], ["-vf", f"scale={width}:{height}:flags=fast_bilinear",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", cq,
                    "-g", "50"], selected
    raise RuntimeError("Reduced-quality playback requires an available video pipeline")

def hls_video_pipeline(quality: str, source_fps: float = 25.0) -> tuple[list[str], list[str], str]:
    """Return a short-GOP browser playback pipeline.

    File exports keep Original video as a bit-for-bit stream copy. Browser playback
    is different: copied camera GOPs can be 6-10 seconds long, which makes HLS
    publish in large bursts and causes the play/buffer/play cycle seen in Firefox.
    For playback only, Original is re-encoded at the source resolution with a
    one-second GOP. Balanced and Data Saver retain their normal resize pipelines.
    """
    gop = max(1, round(source_fps * HLS_SEGMENT_SECONDS))
    if quality != "original":
        pre, out, selected = transcode_video_args(quality)
    else:
        selected = str(acceleration_capabilities().get("selected", "none"))
        if selected == "vaapi_full":
            pre = ["-init_hw_device", f"vaapi=va:{DRI_DEVICE}", "-filter_hw_device", "va",
                   "-hwaccel", "vaapi", "-hwaccel_device", "va",
                   "-hwaccel_output_format", "vaapi"]
            out = ["-c:v", "h264_vaapi", "-profile:v", "high", "-qp", "18", "-g", str(gop)]
        elif selected == "intel_qsv_full":
            pre = ["-init_hw_device", f"vaapi=va:{DRI_DEVICE}", "-init_hw_device", "qsv=qs@va",
                   "-filter_hw_device", "qs", "-hwaccel", "qsv", "-hwaccel_device", "qs",
                   "-hwaccel_output_format", "qsv"]
            out = ["-c:v", "h264_qsv", "-global_quality", "18", "-look_ahead", "0", "-g", str(gop)]
        elif selected == "nvidia_full":
            pre = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
            out = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "18", "-b:v", "0", "-g", str(gop)]
        elif selected == "vaapi_hybrid":
            pre = ["-init_hw_device", f"vaapi=va:{DRI_DEVICE}", "-filter_hw_device", "va"]
            out = ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-qp", "18", "-g", str(gop)]
        elif selected == "nvidia_hybrid":
            pre = []
            out = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "18", "-b:v", "0", "-g", str(gop)]
        elif selected == "software":
            pre = []
            out = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-g", str(gop)]
        else:
            raise RuntimeError("Original browser playback requires an available H.264 encoder")

    # Make every HLS fragment independently decodable and prevent scene-cut logic
    # from creating irregular 6-10 second segment bursts.
    if "-g" in out:
        index = out.index("-g")
        out[index + 1] = str(gop)
    if "libx264" in out:
        out += ["-keyint_min", str(gop), "-sc_threshold", "0"]
    out += ["-bf", "0", "-force_key_frames",
            f"expr:gte(t,n_forced*{HLS_SEGMENT_SECONDS})"]
    return pre, out, selected

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
                shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
    with JOBS_LOCK:
        stale = [job_id for job_id, job in JOBS.items()
                 if job.finished_at and job.finished_at < time.time() - 12 * 3600]
        for job_id in stale:
            cache_key = JOBS[job_id].cache_key
            JOBS.pop(job_id, None)
            if CACHE_TO_JOB.get(cache_key) == job_id:
                CACHE_TO_JOB.pop(cache_key, None)


def build_cache_key(camera_id: str, request: dict[str, Any]) -> str:
    payload = json.dumps({"version": APP_VERSION, "camera_id": camera_id, **request},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def update_job(job: Job, **changes: Any) -> None:
    with JOBS_LOCK:
        for key, value in changes.items():
            setattr(job, key, value)


def safe_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_") or "camera"


def export_filename(camera_name: str, start: dt.datetime, duration: int, quality: str) -> str:
    component = re.sub(r"[^a-zA-Z0-9]+", "-", camera_name).strip("-") or "Camera"
    stop = start + dt.timedelta(seconds=duration)
    quality_label = {
        "original": "Original",
        "balanced": "Balanced-720p",
        "data_saver": "Data-Saver-480p",
    }.get(quality, safe_slug(quality))
    return (
        f"{component}_{start:%Y-%m-%d_%H-%M-%S}_to_"
        f"{stop:%Y-%m-%d_%H-%M-%S}_{quality_label}.mp4"
    )


def generate_job(job: Job) -> None:
    item = camera(job.camera_id)
    request = job.request
    start = parse_local_timestamp(str(request["start"]))
    duration = int(request["duration"])
    gain_db = int(request["gain_db"])
    quality = str(request.get("quality", "original"))
    output_name = export_filename(str(item.get("name", job.camera_id)), start, duration, quality)
    output_path = CACHE / f"{job.cache_key}.mp4"
    job_log = LOGS / f"{job.id}.log"
    work_directory = Path(tempfile.mkdtemp(prefix=f"job-{job.id[:8]}-", dir=WORK))
    update_job(
        job, status="queued", phase="Waiting for camera", started_at=None,
        progress=0.0, captured_seconds=0.0, processed_seconds=0.0,
        remaining_seconds=float(duration),
    )
    try:
        if output_path.exists() and output_path.stat().st_size > 1024:
            update_job(
                job, status="ready", phase="Ready from cache", finished_at=time.time(),
                output_path=str(output_path), output_name=output_name, progress=1.0,
                captured_seconds=float(duration), processed_seconds=float(duration),
                remaining_seconds=0.0,
            )
            return
        capture_log = work_directory / "capture.log"
        capture_command = [
            sys.executable, str(CAPTURE_HELPER), start.strftime("%Y-%m-%d %H:%M:%S"),
            str(duration), str(work_directory),
        ]
        with camera_session_slot(job.camera_id):
            update_job(
                job, status="running", phase="Receiving recording", started_at=time.time(),
            )
            run_capture_with_progress(
                capture_command, job=job, duration=duration, work_directory=work_directory,
                timeout=duration * 2 + 90, env=capture_environment(item), log_path=capture_log,
            )
        summary_path = work_directory / "summary.json"
        video_path = work_directory / "video.h264"
        audio_path = work_directory / "audio.alaw"
        if not summary_path.exists() or not video_path.exists():
            raise RuntimeError("The archive backend did not create its required output files")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        video_frames = int(summary.get("video_frames", 0))
        audio_frames = int(summary.get("audio_frames", 0))
        has_audio = (
            bool(summary.get("has_audio", audio_frames > 0))
            and audio_path.exists() and audio_path.stat().st_size > 0
        )
        if video_frames < 2 or not video_path.stat().st_size:
            raise RuntimeError(f"Incomplete archive capture: {video_frames} video frames")
        update_job(
            job, video_frames=video_frames, audio_frames=audio_frames,
            phase="Preparing browser file", progress=0.92, remaining_seconds=0.0,
        )
        first_video = float(summary.get("first_video_time_us", 0))
        last_video = float(summary.get("last_video_time_us", 0))
        first_audio = float(summary.get("first_audio_time_us", 0))
        fps = float(summary.get("source_fps", 0) or 0)
        if not 5 <= fps <= 120:
            fps = ((video_frames - 1) * 1_000_000 / (last_video - first_video)
                   if last_video > first_video else 25.0)
        if not 5 <= fps <= 120:
            fps = 25.0
        offset_ms = (round((first_audio - first_video) / 1000)
                     if has_audio and first_audio and first_video else 0)
        pre_input, video_args, accelerator_used = transcode_video_args(quality)
        ffmpeg = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-fflags", "+genpts", *pre_input,
            "-r", f"{fps:.6f}", "-f", "h264", "-i", str(video_path),
        ]
        if has_audio:
            ffmpeg += [
                "-f", "alaw", "-ar", "8000", "-ac", "1", "-i", str(audio_path),
                "-map", "0:v:0", "-map", "1:a:0",
            ]
        else:
            ffmpeg += ["-map", "0:v:0", "-an"]
        ffmpeg += video_args
        if has_audio:
            audio_filters: list[str] = []
            if offset_ms > 0:
                audio_filters.append(f"adelay={offset_ms}:all=1")
            elif offset_ms < 0:
                audio_filters += [
                    f"atrim=start={abs(offset_ms) / 1000:.3f}", "asetpts=PTS-STARTPTS",
                ]
            if gain_db:
                audio_filters += [f"volume={gain_db}dB", "alimiter=limit=0.95"]
            audio_filters.append("aresample=async=1:first_pts=0")
            ffmpeg += [
                "-c:a", "aac", "-b:a", "64k", "-ar", "48000", "-ac", "1",
                "-af", ",".join(audio_filters),
            ]
        ffmpeg += [
            "-t", str(duration), "-movflags", "+faststart", "-metadata",
            f"creation_time={start.isoformat(timespec='seconds')}",
            str(work_directory / "final.mp4"),
        ]
        ffmpeg_log = work_directory / "ffmpeg.log"
        run_ffmpeg_with_progress(
            ffmpeg, job=job, duration=duration, work_directory=work_directory,
            timeout=max(120, duration * 3), log_path=ffmpeg_log,
        )
        update_job(job, phase="Validating file", progress=0.995)
        probe = run_command(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,duration:format=duration,size",
             "-of", "json", str(work_directory / "final.mp4")], timeout=30,
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
            "\n=== Archive capture ===\n" + capture_log.read_text(errors="replace")
            + "\n=== FFmpeg ===\n" + ffmpeg_log.read_text(errors="replace"),
            encoding="utf-8",
        )
        update_job(
            job, status="ready", phase="Ready", finished_at=time.time(),
            output_path=str(output_path), output_name=output_name,
            accelerator_used=accelerator_used, progress=1.0,
            captured_seconds=float(duration), processed_seconds=float(duration),
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
            job, status="error", phase="Failed", finished_at=time.time(),
            error=str(error), remaining_seconds=0.0,
        )

def create_job(camera_id: str, request: dict[str, Any]) -> Job:
    camera(camera_id)
    start = parse_local_timestamp(str(request.get("start", "")))
    kind = str(request.get("kind", "playback"))
    if kind not in ("playback", "download"):
        raise ValueError("Kind must be playback or download")
    duration = int(request.get("duration", 300))
    limit = DOWNLOAD_MAX_SECONDS if kind == "download" else PLAYBACK_MAX_SECONDS
    if not 5 <= duration <= limit:
        raise ValueError(f"Duration must be 5-{limit} seconds for {kind}")
    gain_db = int(request.get("gain_db", DEFAULT_GAIN))
    if gain_db not in (0, 6, 12, 18, 24):
        raise ValueError("Audio gain must be 0, 6, 12, 18, or 24 dB")
    quality = str(request.get("quality", ""))
    if not quality:
        quality = "balanced" if str(request.get("mode", "copy")) == "qsv" else "original"
    if quality not in ("original", "balanced", "data_saver"):
        raise ValueError("Quality must be original, balanced, or data_saver")
    if kind == "download":
        quality = "original"
    normalized = {"start": start.isoformat(timespec="seconds"), "duration": duration,
                  "gain_db": gain_db, "quality": quality, "kind": kind}
    cache_key = build_cache_key(camera_id, normalized)
    output_path = CACHE / f"{cache_key}.mp4"
    item = camera(camera_id)
    output_name = export_filename(str(item.get("name", camera_id)), start, duration, quality)
    with JOBS_LOCK:
        existing_id = CACHE_TO_JOB.get(cache_key)
        if existing_id and existing_id in JOBS and JOBS[existing_id].status in ("queued", "running", "ready"):
            return JOBS[existing_id]
        job = Job(id=uuid.uuid4().hex, camera_id=camera_id, cache_key=cache_key, request=normalized)
        if output_path.exists() and output_path.stat().st_size > 1024:
            job.status, job.phase = "ready", "Ready from cache"
            job.started_at = job.finished_at = job.created_at
            job.output_path, job.output_name = str(output_path), output_name
            job.progress = 1.0
            job.captured_seconds = job.processed_seconds = float(duration)
            job.accelerator_used = (
                "copy" if quality == "original"
                else str(acceleration_capabilities().get("selected", "software"))
            )
        JOBS[job.id] = job
        CACHE_TO_JOB[cache_key] = job.id
    if job.status == "queued":
        EXECUTOR.submit(generate_job, job)
    return job




def stream_ffmpeg_command(video_input: str | Path, audio_input: str | Path | None, duration: int,
                          quality: str, gain_db: int, *, source_fps: float = 25.0,
                          audio_offset_ms: int = 0) -> tuple[list[str], str]:
    pre_input, video_output, accelerator_used = transcode_video_args(quality)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-fflags", "+genpts",
        "-analyzeduration", "1000000", "-probesize", "1000000", *pre_input,
        "-thread_queue_size", "512", "-r", f"{source_fps:.6f}",
        "-f", "h264", "-i", str(video_input),
    ]
    if audio_input is not None:
        command += [
            "-thread_queue_size", "512", "-f", "alaw", "-ar", "8000", "-ac", "1",
            "-i", str(audio_input), "-map", "0:v:0", "-map", "1:a:0",
        ]
    else:
        command += ["-map", "0:v:0", "-an"]
    command += video_output
    if audio_input is not None:
        filters: list[str] = []
        total_offset = audio_offset_ms + STREAM_AUDIO_DELAY_MS
        if total_offset > 0:
            filters.append(f"adelay={total_offset}:all=1")
        elif total_offset < 0:
            filters += [f"atrim=start={abs(total_offset) / 1000:.3f}", "asetpts=PTS-STARTPTS"]
        if gain_db:
            filters += [f"volume={gain_db}dB", "alimiter=limit=0.95"]
        filters.append("aresample=async=1:first_pts=0")
        command += [
            "-c:a", "aac", "-b:a", "64k", "-ar", "48000", "-ac", "1",
            "-af", ",".join(filters),
        ]
    command += [
        "-t", str(duration), "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-frag_duration", "1000000", "-flush_packets", "1", "-f", "mp4", "pipe:1",
    ]
    return command, accelerator_used

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
    return "\n\n".join(parts) or "No native-capture or FFmpeg diagnostics were produced."


def _retain_stream_diagnostics(work_directory: Path, camera_id: str, error: BaseException) -> Path | None:
    try:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = LOGS / f"stream-{safe_slug(camera_id)}-{stamp}-{uuid.uuid4().hex[:8]}"
        destination.mkdir(parents=True, exist_ok=False)
        (destination / "error.txt").write_text(f"{type(error).__name__}: {error}\n", encoding="utf-8")
        sizes: dict[str, int] = {}
        for path in work_directory.iterdir():
            try:
                sizes[path.name] = path.stat().st_size
            except OSError:
                continue
            if path.name in {"capture.log", "ffmpeg.log", "summary.json", "timing.json"} and path.is_file():
                shutil.copy2(path, destination / path.name)
        (destination / "artifacts.json").write_text(
            json.dumps({"camera_id": camera_id, "files": sizes}, indent=2) + "\n",
            encoding="utf-8",
        )
        LOG.error("Retained recording-stream diagnostics at %s", destination)
        return destination
    except Exception:
        LOG.exception("Could not retain recording-stream diagnostics")
        return None


def _feed_growing_file(source: Path, write_fd: int, capture_process: subprocess.Popen[bytes],
                       stop_event: threading.Event) -> None:
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
    phase_callback: Callable[[str], None] | None = None,
) -> tuple[float, int, bool]:
    """Wait for first video, then briefly measure cadence and audio timing.

    Camera connection/login/seek time remains in the ``Opening`` phase. The
    short timing deadline begins only after the first video packet is visible,
    so a transport retry no longer consumes the frame-analysis budget. Native
    timing JSON already contains a rolling source_fps estimate; use it as a
    safe fallback when packet timestamps do not span cleanly enough for the
    direct calculation.
    """
    opening_deadline = time.monotonic() + HLS_FIRST_MEDIA_TIMEOUT_SECONDS
    probe_deadline: float | None = None
    latest: dict[str, Any] | None = None
    video_ready_since: float | None = None

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
        offset = round((first_audio - first_video) / 1000) if has_audio and first_audio and first_video else 0
        return fps, offset, has_audio

    while not stop_event.is_set():
        now = time.monotonic()
        latest = _read_live_timing(directory) or latest
        if latest:
            backend = str(latest.get("backend", "native_9008"))
            frames = int(latest.get("video_frames", 0) or 0)
            if backend == "rtsp":
                fps = measured_fps(latest)
                if fps:
                    return fps, 0, False

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
                    if video_ready_since is None:
                        video_ready_since = now
                    elif now - video_ready_since >= HLS_AUDIO_DETECT_SECONDS:
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
    """Turn transport failures into concise UI errors while logs retain detail."""
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

def _hls_command(video_input: str, audio_input: str | None,
                 session: PlaybackSession) -> tuple[list[str], str]:
    request = session.request
    duration = int(request["duration"])
    quality = str(request["quality"])
    gain_db = int(request["gain_db"])
    pre_input, video_output, accelerator = hls_video_pipeline(quality, session.source_fps)
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
               "-fflags", "+genpts", "-analyzeduration", "500000", "-probesize", "500000",
               *pre_input, "-thread_queue_size", "512", "-r", f"{session.source_fps:.6f}",
               "-f", "h264", "-i", video_input]
    if audio_input is not None:
        command += ["-thread_queue_size", "512", "-f", "alaw", "-ar", "8000", "-ac", "1",
                    "-i", audio_input, "-map", "0:v:0", "-map", "1:a:0"]
    else:
        command += ["-map", "0:v:0", "-an"]
    command += video_output
    if audio_input is not None:
        filters: list[str] = []
        total_offset = session.audio_offset_ms + STREAM_AUDIO_DELAY_MS
        if total_offset > 0:
            filters.append(f"adelay={total_offset}:all=1")
        elif total_offset < 0:
            filters += [f"atrim=start={abs(total_offset) / 1000:.3f}", "asetpts=PTS-STARTPTS"]
        if gain_db:
            filters += [f"volume={gain_db}dB", "alimiter=limit=0.95"]
        filters.append("aresample=async=1:first_pts=0")
        command += ["-c:a", "aac", "-b:a", "64k", "-ar", "48000", "-ac", "1",
                    "-af", ",".join(filters)]
    command += ["-t", str(duration), "-max_interleave_delta", "0",
                "-f", "hls", "-hls_time", str(HLS_SEGMENT_SECONDS), "-hls_init_time", str(HLS_SEGMENT_SECONDS),
                "-hls_list_size", "0", "-hls_playlist_type", "event", "-hls_segment_type", "fmp4",
                "-hls_allow_cache", "0", "-flush_packets", "1",
                "-hls_fmp4_init_filename", "init.mp4", "-hls_flags", "independent_segments+temp_file",
                "-hls_segment_filename", str(session.directory / "segment-%05d.m4s"),
                str(session.playlist_path)]
    return command, accelerator

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
            session.capture_process = subprocess.Popen(
                [sys.executable, str(CAPTURE_HELPER), start.strftime("%Y-%m-%d %H:%M:%S"),
                 str(session.request["duration"]), str(directory)],
                stdout=capture_log, stderr=subprocess.STDOUT, env=capture_environment(item),
            )
            session.source_fps, measured_offset, session.has_audio = _wait_for_capture_timing(
                directory,
                session.capture_process,
                session.stop_event,
                phase_callback=lambda phase: setattr(session, "phase", phase),
            )
            session.audio_offset_ms = measured_offset
            LOG.info(
                "Playback session %s measured %.4f fps, %d ms audio offset, audio=%s",
                session.id, session.source_fps, session.audio_offset_ms, session.has_audio,
            )
            video_read, video_write = os.pipe()
            audio_read = audio_write = None
            if session.has_audio:
                audio_read, audio_write = os.pipe()
            try:
                command, accelerator = _hls_command(
                    f"pipe:{video_read}", f"pipe:{audio_read}" if audio_read is not None else None, session
                )
                session.accelerator_used = accelerator
                session.phase = f"Preparing {session.request['quality'].replace('_', ' ')} HLS"
                pass_fds = (video_read,) if audio_read is None else (video_read, audio_read)
                session.ffmpeg_process = subprocess.Popen(
                    command, stdout=subprocess.DEVNULL, stderr=ffmpeg_log, pass_fds=pass_fds,
                )
            finally:
                os.close(video_read)
                if audio_read is not None:
                    os.close(audio_read)
            assert session.capture_process is not None
            feeder_threads.append(threading.Thread(
                target=_feed_growing_file,
                args=(video_path, video_write, session.capture_process, stop_feeders),
                name=f"{session.camera_id}-hls-video", daemon=True,
            ))
            if session.has_audio and audio_write is not None:
                feeder_threads.append(threading.Thread(
                    target=_feed_growing_file,
                    args=(audio_path, audio_write, session.capture_process, stop_feeders),
                    name=f"{session.camera_id}-hls-audio", daemon=True,
                ))
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
            if session.stop_event.is_set():
                session.status = "stopped"
                session.phase = "Stopped"
            elif ffmpeg_rc == 0 and session.playlist_ready():
                session.status = "complete"
                session.phase = "Recording range ready"
            else:
                capture_detail = _tail_text(capture_log_path)
                if capture_rc not in (None, 0):
                    raise RuntimeError(_friendly_capture_error(capture_detail, capture_rc))
                detail = _stream_failure_detail(directory)
                raise RuntimeError(
                    f"Archive playback pipeline failed (ffmpeg={ffmpeg_rc}).\n{detail}"
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
        session.finished_at = time.time()
        lock.release()

def create_playback_session(camera_id: str, request: dict[str, Any]) -> PlaybackSession:
    camera(camera_id)
    start = parse_local_timestamp(str(request.get("start", "")))
    duration = int(request.get("duration", PLAYBACK_MAX_SECONDS))
    if not 5 <= duration <= PLAYBACK_MAX_SECONDS:
        raise ValueError(f"Duration must be 5-{PLAYBACK_MAX_SECONDS} seconds")
    quality = str(request.get("quality", "original"))
    if quality == "auto":
        selected = str(acceleration_capabilities().get("selected", "none"))
        quality = "balanced" if selected not in ("none", "software") else "original"
    if quality not in ("original", "balanced", "data_saver"):
        raise ValueError("Quality must be auto, original, balanced, or data_saver")
    gain_db = int(request.get("gain_db", DEFAULT_GAIN))
    if gain_db not in (0, 6, 12, 18, 24):
        raise ValueError("Audio gain must be 0, 6, 12, 18, or 24 dB")
    normalized = {"start": start.isoformat(timespec="seconds"), "duration": duration,
                  "quality": quality, "gain_db": gain_db}
    session_id = uuid.uuid4().hex
    directory = WORK / f"hls-{session_id}"
    value = PlaybackSession(id=session_id, camera_id=camera_id, request=normalized,
                            work_directory=str(directory))
    with SESSIONS_LOCK:
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
            if value.status in ("queued", "running", "playing") and now - value.last_access > HLS_IDLE_SECONDS:
                value.stop_event.set()
            if value.finished_at and now - value.finished_at > HLS_RETAIN_SECONDS:
                remove.append((session_id, value))
        for session_id, _ in remove:
            SESSIONS.pop(session_id, None)
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


class Handler(BaseHTTPRequestHandler):
    server_version = "TVTArchiveBridge/0.8.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s %s", self.address_string(), fmt % args)

    def _parse(self) -> tuple[urllib.parse.SplitResult, dict[str, list[str]]]:
        parsed = urllib.parse.urlsplit(self.path)
        return parsed, urllib.parse.parse_qs(parsed.query)

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        supplied = ""
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            supplied = header[7:]
        elif query.get("token"):
            supplied = query["token"][0]
        return bool(supplied) and hmac.compare_digest(supplied, TOKEN)

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

    def _serve_file(self, path: Path, filename: str, download: bool, *, head_only: bool = False) -> None:
        if not path.is_file():
            self._error(404, "File not found", head_only=head_only)
            return
        size, start, end, status = path.stat().st_size, 0, path.stat().st_size - 1, 200
        range_header = self.headers.get("Range")
        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
            if not match:
                self._error(416, "Invalid byte range", head_only=head_only); return
            if match.group(1): start = int(match.group(1))
            if match.group(2): end = int(match.group(2))
            if not match.group(1) and match.group(2):
                start, end = max(0, size - int(match.group(2))), size - 1
            if start >= size or start > end:
                self.send_response(416); self.send_header("Content-Range", f"bytes */{size}"); self.end_headers(); return
            end, status = min(end, size - 1), 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=3600")
        if status == 206: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Disposition", f'{"attachment" if download else "inline"}; filename="{filename}"')
        self._headers(); self.end_headers()
        if head_only: return
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
            LOG.info("Prepared-file client disconnected: %s", filename)


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
        }.get(path.suffix, "application/octet-stream")
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range")
        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
            if not match:
                self._error(416, "Invalid byte range", head_only=head_only); return
            if match.group(1): start = int(match.group(1))
            if match.group(2): end = int(match.group(2))
            if not match.group(1) and match.group(2):
                start, end = max(0, size - int(match.group(2))), size - 1
            if start >= size or start > end:
                self.send_response(416); self.send_header("Content-Range", f"bytes */{size}"); self.end_headers(); return
            end, status = min(end, size - 1), 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store" if asset.endswith(".m3u8") else "private, max-age=3600, immutable")
        self.send_header("X-TVT-Archive-Accelerator", value.accelerator_used)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self._headers(); self.end_headers()
        if head_only:
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                block = handle.read(min(1024 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)


    def _serve_recording_stream(self, camera_id: str, query: dict[str, list[str]], *, head_only: bool = False) -> None:
        if head_only:
            self._error(405, "HEAD is not supported for live recording streams", head_only=True)
            return
        start = parse_local_timestamp(query.get("start", [""])[0])
        duration = int(query.get("duration", [str(PLAYBACK_MAX_SECONDS)])[0])
        if not 5 <= duration <= PLAYBACK_MAX_SECONDS:
            raise ValueError(f"Duration must be 5-{PLAYBACK_MAX_SECONDS} seconds")
        quality = str(query.get("quality", ["original"])[0])
        if quality == "auto":
            quality = "balanced" if acceleration_capabilities().get("selected") not in (None, "none", "software") else "original"
        if quality not in ("original", "balanced", "data_saver"):
            raise ValueError("Quality must be auto, original, balanced, or data_saver")
        gain_db = int(query.get("gain_db", [str(DEFAULT_GAIN)])[0])
        if gain_db not in (0, 6, 12, 18, 24):
            raise ValueError("Audio gain must be 0, 6, 12, 18, or 24 dB")

        item = camera(camera_id)
        lock = camera_session_slot(camera_id)
        work_directory = Path(tempfile.mkdtemp(prefix=f"stream-{camera_id}-", dir=WORK))
        video_path = work_directory / "video.h264"
        audio_path = work_directory / "audio.alaw"
        ffmpeg_log = (work_directory / "ffmpeg.log").open("wb")
        capture_log = (work_directory / "capture.log").open("wb")
        ffmpeg_process: subprocess.Popen[bytes] | None = None
        capture_process: subprocess.Popen[bytes] | None = None
        feeder_threads: list[threading.Thread] = []
        stop_feeders = threading.Event()
        stop_capture = threading.Event()
        failure: BaseException | None = None
        response_started = False
        lock.acquire()
        try:
            capture_process = subprocess.Popen(
                [sys.executable, str(CAPTURE_HELPER), start.strftime("%Y-%m-%d %H:%M:%S"),
                 str(duration), str(work_directory)],
                stdout=capture_log, stderr=subprocess.STDOUT, env=capture_environment(item),
            )
            source_fps, audio_offset_ms, has_audio = _wait_for_capture_timing(
                work_directory, capture_process, stop_capture
            )
            video_read, video_write = os.pipe()
            audio_read = audio_write = None
            if has_audio:
                audio_read, audio_write = os.pipe()
            try:
                command, accelerator_used = stream_ffmpeg_command(
                    f"pipe:{video_read}", f"pipe:{audio_read}" if audio_read is not None else None,
                    duration, quality, gain_db, source_fps=source_fps,
                    audio_offset_ms=audio_offset_ms,
                )
                pass_fds = (video_read,) if audio_read is None else (video_read, audio_read)
                ffmpeg_process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=ffmpeg_log, pass_fds=pass_fds,
                )
            finally:
                os.close(video_read)
                if audio_read is not None:
                    os.close(audio_read)

            feeder_threads.append(threading.Thread(
                target=_feed_growing_file,
                args=(video_path, video_write, capture_process, stop_feeders),
                name=f"{camera_id}-video-feed", daemon=True,
            ))
            if has_audio and audio_write is not None:
                feeder_threads.append(threading.Thread(
                    target=_feed_growing_file,
                    args=(audio_path, audio_write, capture_process, stop_feeders),
                    name=f"{camera_id}-audio-feed", daemon=True,
                ))
            for thread in feeder_threads:
                thread.start()

            assert ffmpeg_process.stdout is not None
            ready, _, _ = select.select([ffmpeg_process.stdout], [], [], 30)
            if not ready:
                raise RuntimeError("Timed out waiting for the first recording media fragment.\n" +
                                   _stream_failure_detail(work_directory))
            first = os.read(ffmpeg_process.stdout.fileno(), 256 * 1024)
            if not first:
                raise RuntimeError("FFmpeg ended before producing recording media.\n" +
                                   _stream_failure_detail(work_directory))
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-TVT-Archive-Quality", quality)
            self.send_header("X-TVT-Archive-Accelerator", accelerator_used)
            self.send_header("X-TVT-Archive-Audio", "1" if has_audio else "0")
            self._headers()
            self.end_headers()
            response_started = True
            self.wfile.write(first)
            self.wfile.flush()
            while True:
                block = os.read(ffmpeg_process.stdout.fileno(), 256 * 1024)
                if not block:
                    break
                self.wfile.write(block)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            LOG.info("Recording stream client disconnected: %s", camera_id)
        except Exception as error:
            failure = error
            if response_started:
                LOG.exception("Recording stream failed after HTTP response started")
                return
            raise
        finally:
            stop_capture.set()
            stop_feeders.set()
            for process in (capture_process, ffmpeg_process):
                _terminate_process(process)
            for thread in feeder_threads:
                thread.join(timeout=2)
            ffmpeg_log.close()
            capture_log.close()
            if failure is not None:
                _retain_stream_diagnostics(work_directory, camera_id, failure)
            lock.release()
            shutil.rmtree(work_directory, ignore_errors=True)

    def _route_get(self, *, head_only: bool = False) -> None:
        parsed, query = self._parse()
        path = parsed.path.rstrip("/") or "/"
        if path == "/":
            self._json({"name": "TVT Archive Bridge", "version": APP_VERSION}, head_only=head_only); return
        if path == "/api/health":
            self._json({"ok": True, "version": APP_VERSION,
                        "camera_count": len(CAMERAS), "accelerator": acceleration_capabilities(),
                        "normal_video_mode": "H.264 stream copy",
                        "playback_transport": "HLS fMP4 sessions",
                        "hls_player": f"hls.js {HLS_JS_VERSION}",
                        "native_session_limit_per_camera": NATIVE_SESSION_LIMIT,
                        "active_jobs": sum(j.status in ("queued", "running") for j in JOBS.values()),
                        "active_playback_sessions": sum(x.status in ("queued", "running", "playing") for x in SESSIONS.values())},
                       head_only=head_only); return
        if not self._authorized(query):
            self._error(401, "Missing or invalid access token", head_only=head_only); return
        try:
            if path == "/api/player/hls.js":
                self._serve_javascript(HLS_JS_PATH, head_only=head_only); return
            if path == "/api/cameras":
                self._json({"cameras": list_cameras()}, head_only=head_only); return
            match = re.fullmatch(r"/api/cameras/([^/]+)", path)
            if match:
                self._json({"camera": safe_camera(match.group(1))}, head_only=head_only); return
            match = re.fullmatch(r"/api/sessions/([a-f0-9]{32})/(index\.m3u8|init\.mp4|segment-\d{5}\.m4s)", path)
            if match:
                self._serve_hls_asset(match.group(1), match.group(2), head_only=head_only); return
            match = re.fullmatch(r"/api/sessions/([a-f0-9]{32})", path)
            if match:
                self._json(_session(match.group(1)).public(), head_only=head_only); return
            match = re.fullmatch(r"/api/cameras/([^/]+)/stream", path)
            if match:
                self._serve_recording_stream(match.group(1), query, head_only=head_only); return
            match = re.fullmatch(r"/api/cameras/([^/]+)/(timeline|availability|status)", path)
            if match:
                camera_id, action = match.groups()
                if action == "timeline":
                    day = validate_date(query.get("date", [dt.date.today().isoformat()])[0])
                    force = query.get("refresh", ["0"])[0].lower() in ("1", "true", "yes")
                    self._json(get_timeline(camera_id, day, force=force), head_only=head_only); return
                if action == "availability":
                    self._json(get_availability(camera_id, int(query.get("days", ["45"])[0])), head_only=head_only); return
                force = query.get("refresh", ["0"])[0].lower() in ("1", "true", "yes")
                self._json(get_status(camera_id, force=force), head_only=head_only); return
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})(?:/file)?", path)
            if match:
                job = JOBS.get(match.group(1))
                if job is None: self._error(404, "Unknown job", head_only=head_only); return
                if path.endswith("/file"):
                    if job.status != "ready" or not job.output_path:
                        self._error(409, "The file is not ready", head_only=head_only); return
                    self._serve_file(Path(job.output_path), job.output_name or "recording.mp4",
                                     query.get("download", ["0"])[0] == "1", head_only=head_only); return
                self._json(job.public(), head_only=head_only); return
            self._error(404, "Not found", head_only=head_only)
        except KeyError as error:
            self._error(404, str(error), head_only=head_only)
        except ValueError as error:
            self._error(400, str(error), head_only=head_only)
        except Exception as error:
            LOG.exception("GET %s failed", path); self._error(500, str(error), head_only=head_only)

    def do_GET(self) -> None: self._route_get()
    def do_HEAD(self) -> None: self._route_get(head_only=True)

    def do_POST(self) -> None:
        parsed, query = self._parse()
        if not self._authorized(query):
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
        except Exception as error:
            LOG.exception("POST %s failed", path)
            self._error(500, str(error))

    def do_PUT(self) -> None:
        parsed, query = self._parse()
        if not self._authorized(query):
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
        except Exception as error:
            LOG.exception("PUT %s failed", path)
            self._error(500, str(error))

    def do_DELETE(self) -> None:
        parsed, query = self._parse()
        if not self._authorized(query):
            self._error(401, "Missing or invalid access token")
            return
        path = parsed.path.rstrip("/")
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
        except Exception as error:
            LOG.exception("DELETE %s failed", path)
            self._error(500, str(error))


if __name__ == "__main__":
    threading.Thread(target=cleanup_loop, name="cache-cleanup", daemon=True).start()
    clean_cache()
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    LOG.info("TVT Archive Bridge %s listening on %s:%s", APP_VERSION, BIND, PORT)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        for value in list(SESSIONS.values()):
            value.stop_event.set()
        EXECUTOR.shutdown(wait=False, cancel_futures=True)
        PLAYBACK_EXECUTOR.shutdown(wait=False, cancel_futures=True)
        server.server_close()

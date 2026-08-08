#!/usr/bin/env python3
"""Capture one TVT archive interval into growing elementary-stream files.

The bridge starts this helper as a subprocess so browser disconnects and HLS
sessions can terminate capture cleanly. Backends:

* native_9008: pure-Python private TCP/9008 playback with H.264 + G.711 A-law.
* rtsp: TVT recorded RTSP playback with H.264 video and no assumed audio.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from native9008 import TVT9008Client, TVT9008Error

STOP = False
CHILD: subprocess.Popen[bytes] | None = None


def handle_signal(signum, frame) -> None:
    global STOP
    STOP = True
    if CHILD is not None and CHILD.poll() is None:
        CHILD.terminate()


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def atomic_json(path: Path, value: dict[str, object]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def rtsp_url(start: datetime, duration: int) -> str:
    username = urllib.parse.quote(required("TVT_USER"), safe="")
    password = urllib.parse.quote(required("TVT_PASSWORD"), safe="")
    host = required("TVT_HOST")
    port = int(os.environ.get("TVT_RTSP_PORT", "554"))
    channel = int(os.environ.get("TVT_CHANNEL", "0"))
    stream_type = os.environ.get("TVT_RTSP_STREAM_TYPE", "main")
    if stream_type not in {"main", "sub"}:
        raise ValueError("TVT_RTSP_STREAM_TYPE must be main or sub")
    return (
        f"rtsp://{username}:{password}@{host}:{port}/"
        f"chID={channel}&date={start:%Y-%m-%d}&time={start:%H:%M:%S}&"
        f"timelen={duration}&streamType={stream_type}&action=playback"
    )




def ffmpeg_progress_seconds(path: Path) -> float:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0.0
    values: dict[str, str] = {}
    for line in lines:
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    for key in ("out_time_us", "out_time_ms"):
        raw = values.get(key)
        if raw in (None, ""):
            continue
        try:
            return max(0.0, int(raw) / 1_000_000)
        except ValueError:
            continue
    return 0.0

def probe_h264(path: Path, fallback_fps: float) -> tuple[int, float]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames,avg_frame_rate", "-of", "json", str(path),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=45, check=False,
    )
    if result.returncode != 0:
        return 0, fallback_fps
    try:
        stream = json.loads(result.stdout).get("streams", [{}])[0]
        frames = int(stream.get("nb_read_frames") or 0)
        numerator, denominator = str(stream.get("avg_frame_rate", "0/0")).split("/", 1)
        fps = float(numerator) / float(denominator) if float(denominator) else fallback_fps
        if not 5 <= fps <= 120:
            fps = fallback_fps
        return frames, fps
    except Exception:
        return 0, fallback_fps


def capture_rtsp(start: datetime, duration: int, directory: Path) -> int:
    global CHILD
    video = directory / "video.h264"
    audio = directory / "audio.alaw"
    timing = directory / "timing.json"
    summary = directory / "summary.json"
    progress = directory / "rtsp-progress.txt"
    ffmpeg_log = directory / "rtsp-ffmpeg.log"
    audio.touch()
    fps = float(os.environ.get("TVT_RTSP_FPS", "25"))
    if not 5 <= fps <= 120:
        raise ValueError("TVT_RTSP_FPS must be between 5 and 120")
    first = int(time.mktime(start.timetuple())) * 1_000_000
    value = {
        "video_frames": 0, "audio_frames": 0,
        "video_bytes": 0, "audio_bytes": 0,
        "first_video_time_us": first,
        "last_video_time_us": 0,
        "first_audio_time_us": 0, "last_audio_time_us": 0,
        "source_fps": fps, "has_audio": False, "backend": "rtsp",
        "captured_seconds": 0.0,
    }
    atomic_json(timing, value)
    url = rtsp_url(start, duration)
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-progress", str(progress), "-nostats",
        "-rtsp_transport", os.environ.get("TVT_RTSP_TRANSPORT", "tcp"),
        "-rw_timeout", "12000000", "-i", url,
        "-map", "0:v:0", "-an", "-c:v", "copy", "-bsf:v", "h264_mp4toannexb",
        "-t", str(duration), "-f", "h264", str(video),
    ]
    password = required("TVT_PASSWORD")
    with ffmpeg_log.open("wb") as log_handle:
        CHILD = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=log_handle,
        )
        while CHILD.poll() is None:
            if STOP:
                CHILD.terminate()
                break
            captured = min(float(duration), ffmpeg_progress_seconds(progress))
            value["captured_seconds"] = captured
            value["last_video_time_us"] = first + round(captured * 1_000_000) if captured else 0
            atomic_json(timing, value)
            time.sleep(0.25)
        try:
            rc = CHILD.wait(timeout=10)
        except subprocess.TimeoutExpired:
            CHILD.kill()
            rc = CHILD.wait(timeout=5)
    CHILD = None
    sanitized = ffmpeg_log.read_text(encoding="utf-8", errors="replace").replace(password, "***")
    ffmpeg_log.write_text(sanitized, encoding="utf-8")
    if sanitized.strip():
        print(sanitized.strip(), file=sys.stderr)
    if STOP:
        return 143
    if rc != 0:
        raise RuntimeError(f"Recorded RTSP FFmpeg exited with status {rc}")
    if not video.exists() or video.stat().st_size == 0:
        raise RuntimeError("Recorded RTSP returned no H.264 video")
    frames, measured_fps = probe_h264(video, fps)
    if frames <= 0:
        frames = max(2, round(duration * measured_fps))
    last = first + round((frames - 1) * 1_000_000 / measured_fps)
    value.update({
        "video_frames": frames,
        "video_bytes": video.stat().st_size,
        "last_video_time_us": last,
        "source_fps": measured_fps,
        "captured_seconds": min(float(duration), max(0.0, (last - first) / 1_000_000)),
        "timed_out": False,
    })
    atomic_json(timing, value)
    atomic_json(summary, value)
    return 0


def capture_native(start: datetime, duration: int, directory: Path) -> int:
    host = required("TVT_HOST")
    port = int(os.environ.get("TVT_PORT", "9008"))
    username = required("TVT_USER")
    password = required("TVT_PASSWORD")
    timeout = float(os.environ.get("TVT_TIMEOUT", "8"))
    attempts = max(1, min(8, int(os.environ.get("TVT_CAPTURE_CONNECT_ATTEMPTS", "5"))))
    backoff = (0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 5.0, 5.0)
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        if STOP:
            return 143
        try:
            with TVT9008Client(host, port, username, password, timeout=timeout) as client:
                result = client.capture(start, duration, directory, stop_requested=lambda: STOP)
            if result.timed_out:
                raise TimeoutError(
                    "Native TCP/9008 capture timed out before the requested recording range completed"
                )
            if result.video_frames < 2 or result.video_bytes <= 0:
                raise RuntimeError(
                    f"Native TCP/9008 capture returned {result.video_frames} video frames"
                )
            return 0
        except (OSError, EOFError, TimeoutError, TVT9008Error) as error:
            last_error = error
            # A failure after media has already begun must not overwrite the growing
            # files that FFmpeg is currently consuming. Initial connection failures,
            # however, are safe to retry transparently.
            video = directory / "video.h264"
            if video.exists() and video.stat().st_size > 0:
                raise
            if attempt >= attempts:
                break
            delay = backoff[min(attempt, len(backoff) - 1)]
            print(
                f"archive connection attempt {attempt}/{attempts} failed: {error}; "
                f"retrying in {delay:.1f}s",
                file=sys.stderr,
                flush=True,
            )
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                if STOP:
                    return 143
                time.sleep(min(0.1, deadline - time.monotonic()))
    assert last_error is not None
    raise RuntimeError(
        f"Archive connection failed after {attempts} attempts: {last_error}"
    ) from last_error


def main() -> int:
    if len(sys.argv) != 4:
        print("Usage: archive_capture.py START_TIMESTAMP DURATION_SECONDS WORK_DIRECTORY", file=sys.stderr)
        return 2
    start = datetime.strptime(sys.argv[1].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    duration = int(sys.argv[2])
    if not 1 <= duration <= 3600:
        raise ValueError("Duration must be between 1 and 3600 seconds")
    directory = Path(sys.argv[3])
    directory.mkdir(parents=True, exist_ok=True)
    backend = os.environ.get("TVT_ARCHIVE_BACKEND", "native_9008")
    if backend == "native_9008":
        return capture_native(start, duration, directory)
    if backend == "rtsp":
        return capture_rtsp(start, duration, directory)
    raise ValueError(f"Unsupported archive backend: {backend}")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)

#!/usr/bin/env python3
"""Capture one archive interval into growing video.h264 and audio.alaw files."""

from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from native9008 import TVT9008Client, TVT9008Error

STOP = False


def handle_signal(signum, frame) -> None:
    global STOP
    STOP = True


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def capture(start: datetime, duration: int, directory: Path) -> int:
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
                raise TimeoutError("Capture timed out before the requested range completed")
            if result.video_frames < 2 or result.video_bytes <= 0:
                raise RuntimeError(f"Capture returned {result.video_frames} video frames")
            return 0
        except (OSError, EOFError, TimeoutError, TVT9008Error) as error:
            last_error = error
            # Retry only before media starts; ffmpeg is already reading the files after that.
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
        print("usage: archive_capture.py START DURATION_SECONDS WORK_DIRECTORY", file=sys.stderr)
        return 2
    start = datetime.strptime(sys.argv[1].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    duration = int(sys.argv[2])
    if not 1 <= duration <= 3600:
        raise ValueError("Duration must be between 1 and 3600 seconds")
    directory = Path(sys.argv[3])
    directory.mkdir(parents=True, exist_ok=True)
    return capture(start, duration, directory)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

#!/usr/bin/env python3
"""Pure-Python interoperability client for TVT/NVMS TCP/9008 archives.

This module implements only the read-only operations required by TVT Archive:
login, recording searches, and recorded-media playback. It does not change
camera settings or delete recordings.
"""
from __future__ import annotations

import json
import os
import re
import socket
import struct
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Callable, Optional

OUTER_MAGIC = b"1111"
HEAD_MAGIC = b"head"
HEAD_SIZE = 64
KEY_OFFSET = 28
KEY_SIZE = 4
TARGET_CAMERA = 3

KIND_LOGIN_REQUEST = 0x00000101
KIND_LOGIN_RESPONSE = 0x01000101
KIND_CONFIG_REQUEST = 0x00000411
KIND_CONFIG_RESPONSE = 0x0100040F
KIND_PLAYBACK_START = 0x0000090B
KIND_PLAYBACK_START_RESPONSE = 0x01000909
KIND_PLAYBACK_CONTINUE = 0x0000090A
CONTINUATION_INTERVAL_FRAMES = 25
NATIVE_VIDEO_STALL_SECONDS = max(
    5.0, min(float(os.environ.get("TVT_ARCHIVE_NATIVE_VIDEO_STALL_SECONDS", "15")), 120.0)
)
NATIVE_CAPTURE_DEADLINE_FACTOR = max(
    2.0, min(float(os.environ.get("TVT_ARCHIVE_NATIVE_CAPTURE_DEADLINE_FACTOR", "6")), 10.0)
)
KIND_RECORDED_MEDIA = 0x01000C05

LOGIN_REQUEST_ID = 0x00000105
CONFIG_REQUEST_ID = 0x0000FFFF
MAX_NORMAL_FRAME = 32 * 1024 * 1024
MAX_FRAGMENTED_OBJECT = 32 * 1024 * 1024
MAX_FRAGMENT_CHUNKS = 4096
MAX_INFLIGHT_FRAGMENT_OBJECTS = 16
MAX_INFLIGHT_FRAGMENT_BYTES = 64 * 1024 * 1024
FRAGMENT_ASSEMBLY_TTL_SECONDS = 60.0
MEDIA_HEADER_SIZE = 40
AUDIO_PREFIX_SIZE = 4
AUDIO_MARKER = b"\x01\x22\x00\x01"
EXPECTED_AUDIO_BYTES = 320


class TVT9008Error(RuntimeError):
    """Raised for transport or protocol failures."""


@dataclass
class InnerFrame:
    kind: int
    request_id: int
    target: int
    body: bytes
    fragmented: bool = False
    fragment_id: int | None = None


@dataclass
class FragmentAssembly:
    chunk_count: int
    total_length: int
    chunks: dict[int, bytes]
    received_bytes: int = 0
    updated_at: float = 0.0


@dataclass
class CaptureSummary:
    video_frames: int = 0
    audio_frames: int = 0
    video_bytes: int = 0
    audio_bytes: int = 0
    first_video_time_us: int = 0
    last_video_time_us: int = 0
    first_audio_time_us: int = 0
    last_audio_time_us: int = 0
    keyframes: int = 0
    fragmented_objects: int = 0
    fragment_chunks: int = 0
    housekeeping_frames: int = 0
    continuation_commands: int = 0
    timed_out: bool = False
    has_audio: bool = False
    backend: str = "native_9008"

    def as_dict(self) -> dict[str, object]:
        return {
            "video_frames": self.video_frames,
            "audio_frames": self.audio_frames,
            "video_bytes": self.video_bytes,
            "audio_bytes": self.audio_bytes,
            "first_video_time_us": self.first_video_time_us,
            "last_video_time_us": self.last_video_time_us,
            "first_audio_time_us": self.first_audio_time_us,
            "last_audio_time_us": self.last_audio_time_us,
            "first_video_relative_us": 0,
            "last_video_relative_us": 0,
            "first_audio_relative_us": 0,
            "last_audio_relative_us": 0,
            "keyframes": self.keyframes,
            "fragmented_objects": self.fragmented_objects,
            "fragment_chunks": self.fragment_chunks,
            "housekeeping_frames": self.housekeeping_frames,
            "continuation_commands": self.continuation_commands,
            "timed_out": self.timed_out,
            "has_audio": self.has_audio,
            "backend": self.backend,
        }


class ApplicationObjectReader:
    """Read normal and fragmented 1111-framed application objects."""

    def __init__(self, stream: socket.socket | BinaryIO) -> None:
        self.stream = stream
        self.buffer = bytearray()
        self.fragments: dict[int, FragmentAssembly] = {}
        self.fragment_bytes = 0
        self.is_socket = hasattr(stream, "recv")
        self.fragmented_objects = 0
        self.fragment_chunks = 0

    def _read_some(self, size: int) -> bytes:
        return self.stream.recv(size) if self.is_socket else self.stream.read(size)

    def read_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self._read_some(max(1, size - len(self.buffer)))
            if not chunk:
                raise EOFError(f"Stream ended with {len(self.buffer)}/{size} bytes buffered")
            self.buffer.extend(chunk)
        output = bytes(self.buffer[:size])
        del self.buffer[:size]
        return output

    def _discard_fragment(self, object_id: int) -> None:
        assembly = self.fragments.pop(object_id, None)
        if assembly is not None:
            self.fragment_bytes = max(0, self.fragment_bytes - assembly.received_bytes)

    def _expire_fragments(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        for object_id, assembly in list(self.fragments.items()):
            if current - assembly.updated_at > FRAGMENT_ASSEMBLY_TTL_SECONDS:
                self._discard_fragment(object_id)

    def read_object(self) -> tuple[bytes, bool, int | None] | None:
        while True:
            self._expire_fragments()
            header = self.read_exact(8)
            if header[:4] != OUTER_MAGIC:
                raise TVT9008Error(f"Unexpected outer magic {header[:4]!r}")
            declared = struct.unpack_from("<I", header, 4)[0]
            if declared == 0:
                return None
            if declared != 0xFFFFFFFF:
                if declared < 16 or declared > MAX_NORMAL_FRAME:
                    raise TVT9008Error(f"Invalid normal object length {declared}")
                return self.read_exact(declared), False, None

            object_id, chunk_count, total_length, chunk_index, chunk_length, reserved = struct.unpack(
                "<IIIIII", self.read_exact(24)
            )
            if reserved != 0:
                raise TVT9008Error(f"Fragment {object_id} has nonzero reserved field")
            if not (1 <= chunk_count <= MAX_FRAGMENT_CHUNKS):
                raise TVT9008Error(f"Fragment {object_id} has invalid chunk count")
            if not (1 <= chunk_index <= chunk_count):
                raise TVT9008Error(f"Fragment {object_id} has invalid chunk index")
            if not (1 <= total_length <= MAX_FRAGMENTED_OBJECT):
                raise TVT9008Error(f"Fragment {object_id} has invalid total length")
            if not (1 <= chunk_length <= MAX_FRAGMENTED_OBJECT):
                raise TVT9008Error(f"Fragment {object_id} has invalid chunk length")

            chunk = self.read_exact(chunk_length)
            now = time.monotonic()
            assembly = self.fragments.get(object_id)
            if assembly is None:
                if len(self.fragments) >= MAX_INFLIGHT_FRAGMENT_OBJECTS:
                    raise TVT9008Error("Too many incomplete fragmented objects")
                assembly = FragmentAssembly(chunk_count, total_length, {}, updated_at=now)
                self.fragments[object_id] = assembly
            elif assembly.chunk_count != chunk_count or assembly.total_length != total_length:
                self._discard_fragment(object_id)
                raise TVT9008Error(f"Fragment metadata changed for object {object_id}")

            existing = assembly.chunks.get(chunk_index)
            if existing is not None:
                assembly.updated_at = now
                self.fragment_chunks += 1
                if existing != chunk:
                    self._discard_fragment(object_id)
                    raise TVT9008Error(f"Fragment {object_id} duplicate chunk {chunk_index} changed")
                continue

            retained = assembly.received_bytes + chunk_length
            if retained > MAX_FRAGMENTED_OBJECT:
                self._discard_fragment(object_id)
                raise TVT9008Error(f"Fragment {object_id} retained too many bytes")
            if self.fragment_bytes + chunk_length > MAX_INFLIGHT_FRAGMENT_BYTES:
                self._discard_fragment(object_id)
                raise TVT9008Error("In-flight fragment memory limit exceeded")

            assembly.chunks[chunk_index] = chunk
            assembly.received_bytes = retained
            assembly.updated_at = now
            self.fragment_bytes += chunk_length
            self.fragment_chunks += 1
            if len(assembly.chunks) != assembly.chunk_count:
                continue
            try:
                payload = b"".join(assembly.chunks[index] for index in range(1, chunk_count + 1))
            except KeyError as exc:
                self._discard_fragment(object_id)
                raise TVT9008Error(f"Fragment object {object_id} is incomplete") from exc
            self._discard_fragment(object_id)
            if len(payload) < total_length:
                raise TVT9008Error(f"Fragment object {object_id} reconstructed short")
            self.fragmented_objects += 1
            return payload[:total_length], True, object_id


def _xor_repeating(data: bytes, key: bytes) -> bytes:
    return bytes(value ^ key[index % len(key)] for index, value in enumerate(data))


def _outer(inner: bytes) -> bytes:
    return OUTER_MAGIC + struct.pack("<I", len(inner)) + inner


def _command(kind: int, request_id: int, body: bytes = b"", target: int = TARGET_CAMERA) -> bytes:
    return _outer(struct.pack("<IIII", kind, request_id, target, len(body)) + body)


def _parse_inner(payload: bytes, fragmented: bool = False, fragment_id: int | None = None) -> InnerFrame:
    if len(payload) < 16:
        raise TVT9008Error("Inner object is shorter than its header")
    kind, request_id, target, body_length = struct.unpack_from("<IIII", payload, 0)
    if body_length > len(payload) - 16:
        raise TVT9008Error("Inner body length exceeds available payload")
    return InnerFrame(kind, request_id, target, payload[16:16 + body_length], fragmented, fragment_id)


def _client_tag() -> bytes:
    node = uuid.getnode()
    mac = ":".join(f"{(node >> shift) & 0xFF:02X}" for shift in range(40, -1, -8))
    return mac[:6].encode("ascii")


def _login_body(username: str, password: str, key: bytes) -> bytes:
    username_bytes = username.encode("utf-8")
    password_bytes = password.encode("utf-8")
    if not username_bytes or len(username_bytes) > 31:
        raise ValueError("Username must contain 1-31 UTF-8 bytes")
    if not password_bytes or len(password_bytes) > 31:
        raise ValueError("Password must contain 1-31 UTF-8 bytes")
    body = bytearray(116)
    username_plain = username_bytes + b"\x00"
    body[0x04:0x04 + len(username_plain)] = _xor_repeating(username_plain, key)
    password_field = bytearray(32)
    password_field[:len(password_bytes)] = password_bytes
    body[0x24:0x44] = _xor_repeating(bytes(password_field), key)
    body[0x68:0x6E] = _client_tag()
    struct.pack_into("<I", body, 0x70, 3)
    return bytes(body)


def _command_field(name: str) -> bytes:
    encoded = name.encode("ascii")
    if len(encoded) >= 64:
        raise ValueError("Command name is too long")
    return encoded.ljust(64, b"\x00")


def _search_xml(start: datetime, stop: datetime) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<config version="1.0" xmlns="http://www.ipc.com/ver10">\n'
        '  <search>\n'
        '    <recTypes type="list">\n'
        '      <itemType type="recType"/>\n'
        '      <item>manual</item><item>nic broken</item><item>schedule</item>\n'
        '      <item>motion</item><item>sensor</item><item>intel detection</item>\n'
        '    </recTypes>\n'
        f'    <starttime type="string"><![CDATA[{start:%Y-%m-%d %H:%M:%S}]]></starttime>\n'
        f'    <endtime type="string"><![CDATA[{stop:%Y-%m-%d %H:%M:%S}]]></endtime>\n'
        '  </search>\n'
        '</config>'
    ).encode("utf-8")


def _strip_http(payload: bytes) -> bytes:
    position = payload.find(b"<?xml")
    if position >= 0:
        return payload[position:].rstrip(b"\x00")
    separator = payload.find(b"\r\n\r\n")
    return payload[separator + 4:].rstrip(b"\x00") if separator >= 0 else payload.rstrip(b"\x00")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_xml(payload: bytes) -> tuple[bytes, ET.Element]:
    xml_bytes = _strip_http(payload)
    try:
        return xml_bytes, ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        preview = xml_bytes[:200].decode("utf-8", errors="replace")
        raise TVT9008Error(f"Invalid XML response: {exc}; starts with {preview!r}") from exc


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class TVT9008Client:
    def __init__(self, host: str, port: int, username: str, password: str, timeout: float = 8.0) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.reader: ApplicationObjectReader | None = None
        self._send_lock = threading.Lock()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    def __enter__(self) -> "TVT9008Client":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        if self.sock is not None:
            return
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(self.timeout)
        reader = ApplicationObjectReader(sock)
        hello = reader.read_exact(HEAD_SIZE)
        if hello[:4] != HEAD_MAGIC:
            sock.close()
            raise TVT9008Error(f"Expected 'head' greeting, got {hello[:4]!r}")
        key = hello[KEY_OFFSET:KEY_OFFSET + KEY_SIZE]
        if key == b"\x00" * KEY_SIZE:
            sock.close()
            raise TVT9008Error("Camera returned an invalid zero login key")
        self.sock = sock
        self.reader = reader
        try:
            self.send(KIND_LOGIN_REQUEST, LOGIN_REQUEST_ID, _login_body(self.username, self.password, key))
            self.wait_for(KIND_LOGIN_RESPONSE, LOGIN_REQUEST_ID, self.timeout)
            sock.settimeout(0.75)
            self._heartbeat_stop.clear()
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self._heartbeat_stop.set()
        heartbeat_thread, self._heartbeat_thread = self._heartbeat_thread, None
        if heartbeat_thread is not None and heartbeat_thread is not threading.current_thread():
            heartbeat_thread.join(timeout=1)
        sock, self.sock = self.sock, None
        self.reader = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(4.0):
            try:
                with self._send_lock:
                    if self.sock is None:
                        return
                    self.sock.sendall(OUTER_MAGIC + b"\x00\x00\x00\x00")
            except OSError:
                return

    def send(self, kind: int, request_id: int, body: bytes = b"") -> None:
        with self._send_lock:
            if self.sock is None:
                raise TVT9008Error("Client is not connected")
            self.sock.sendall(_command(kind, request_id, body))

    def read_frame(self) -> InnerFrame | None:
        if self.reader is None:
            raise TVT9008Error("Client is not connected")
        item = self.reader.read_object()
        if item is None:
            return None
        return _parse_inner(*item)

    def wait_for(self, kind: int, request_id: int | None, timeout: float) -> InnerFrame:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                frame = self.read_frame()
            except socket.timeout:
                continue
            if frame is None or frame.kind != kind:
                continue
            if request_id is None or frame.request_id == request_id:
                return frame
        raise TimeoutError(f"Timed out waiting for kind 0x{kind:08X}")

    def config_command(self, name: str, extra: bytes = b"") -> InnerFrame:
        body = _command_field(name) + extra + (b"\x00\x00" if extra else b"")
        self.send(KIND_CONFIG_REQUEST, CONFIG_REQUEST_ID, body)
        return self.wait_for(KIND_CONFIG_RESPONSE, CONFIG_REQUEST_ID, self.timeout)

    def recording_dates(self) -> list[str]:
        xml_bytes, root = _parse_xml(self.config_command("SearchRecordDate").body)
        dates: set[str] = set()
        for element in root.iter():
            text = (element.text or "").strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                dates.add(text)
        dates.update(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", xml_bytes.decode("utf-8", errors="replace")))
        return sorted(dates)

    def search(self, start: datetime, stop: datetime) -> list[dict[str, object]]:
        if stop <= start:
            raise ValueError("Search stop must be after start")
        _, root = _parse_xml(self.config_command("SearchByTime", _search_xml(start, stop)).body)
        results: list[dict[str, object]] = []
        for element in root.iter():
            if _local_name(element.tag) != "item":
                continue
            text = (element.text or "").strip()
            seconds_text = element.attrib.get("seconds")
            if not text or seconds_text is None:
                continue
            try:
                item_start = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
                seconds = int(seconds_text)
            except (TypeError, ValueError):
                continue
            if seconds <= 0:
                continue
            item_stop = item_start + timedelta(seconds=seconds)
            results.append({
                "start": item_start.isoformat(timespec="seconds"),
                "stop": item_stop.isoformat(timespec="seconds"),
                "seconds": seconds,
                "type": element.attrib.get("recType", "unknown"),
            })
        return results

    @staticmethod
    def _playback_body(start_epoch: int, stop_epoch: int) -> bytes:
        body = bytearray(32)
        struct.pack_into("<I", body, 0, 0x00145000)
        body[4] = 1
        struct.pack_into("<I", body, 8, 1)
        struct.pack_into("<QQI", body, 12, start_epoch, stop_epoch, 0)
        return bytes(body)

    @staticmethod
    def _extract_media(frame: InnerFrame) -> tuple[str, bytes, int, bool]:
        body = frame.body
        if len(body) < MEDIA_HEADER_SIZE:
            return "other", b"", 0, False
        marker = body[:4]
        timestamp_us = struct.unpack_from("<Q", body, 16)[0]
        data_length = struct.unpack_from("<I", body, 32)[0]
        if data_length > len(body) - MEDIA_HEADER_SIZE:
            return "other", b"", timestamp_us, False
        data = body[MEDIA_HEADER_SIZE:MEDIA_HEADER_SIZE + data_length]
        if marker == AUDIO_MARKER:
            if len(data) < AUDIO_PREFIX_SIZE:
                return "other", b"", timestamp_us, False
            return "audio", data[AUDIO_PREFIX_SIZE:], timestamp_us, False
        if data.startswith(b"\x00\x00\x00\x01") or data.startswith(b"\x00\x00\x01"):
            return "video", data, timestamp_us, marker == b"\x00\x00\x00\x01"
        return "other", b"", timestamp_us, False

    def capture(
        self,
        start: datetime,
        duration: int,
        output_directory: Path,
        *,
        stop_requested: Callable[[], bool] | None = None,
        request_id: int = 11,
        timing_interval_frames: int = 6,
    ) -> CaptureSummary:
        output_directory.mkdir(parents=True, exist_ok=True)
        video_path = output_directory / "video.h264"
        audio_path = output_directory / "audio.alaw"
        timing_path = output_directory / "timing.json"
        summary_path = output_directory / "summary.json"
        stop = start + timedelta(seconds=duration)
        # The camera and container share local wall-clock time. mktime deliberately
        # interprets the naive camera timestamp in the container's mounted timezone.
        start_epoch = int(time.mktime(start.timetuple()))
        stop_epoch = int(time.mktime(stop.timetuple()))
        summary = CaptureSummary()
        self.send(KIND_PLAYBACK_START, request_id, self._playback_body(start_epoch, stop_epoch))
        self.wait_for(KIND_PLAYBACK_START_RESPONSE, request_id, self.timeout)
        target_end_us = stop_epoch * 1_000_000
        # Archive delivery on real cameras can be substantially slower than realtime.
        # A generous overall deadline avoids killing a healthy slow stream, while the
        # mid-stream video watchdog below catches a connection that is actually stuck.
        deadline = time.monotonic() + duration * NATIVE_CAPTURE_DEADLINE_FACTOR + 60
        next_continue_frame = CONTINUATION_INTERVAL_FRAMES
        last_video_wall: float | None = None

        def write_timing() -> None:
            value = summary.as_dict()
            value["source_fps"] = (
                (summary.video_frames - 1) * 1_000_000 /
                (summary.last_video_time_us - summary.first_video_time_us)
                if summary.video_frames > 1 and summary.last_video_time_us > summary.first_video_time_us
                else 25.0
            )
            value["captured_seconds"] = max(
                0.0,
                (
                    max(summary.last_video_time_us, summary.last_audio_time_us)
                    - min(
                        value for value in (
                            summary.first_video_time_us,
                            summary.first_audio_time_us,
                        ) if value > 0
                    )
                ) / 1_000_000,
            ) if summary.first_video_time_us or summary.first_audio_time_us else 0.0
            _atomic_json(timing_path, value)

        write_timing()
        with video_path.open("wb", buffering=0) as video, audio_path.open("wb", buffering=0) as audio:
            while time.monotonic() < deadline:
                now = time.monotonic()
                if (
                    summary.video_frames
                    and last_video_wall is not None
                    and now - last_video_wall >= NATIVE_VIDEO_STALL_SECONDS
                ):
                    raise TimeoutError(
                        f"Archive video stalled for {NATIVE_VIDEO_STALL_SECONDS:.0f} seconds "
                        "after playback had already started"
                    )
                if stop_requested is not None and stop_requested():
                    break
                try:
                    frame = self.read_frame()
                except socket.timeout:
                    continue
                if frame is None or frame.kind != KIND_RECORDED_MEDIA or frame.request_id != request_id:
                    continue
                media_type, payload, timestamp_us, keyframe = self._extract_media(frame)
                if media_type == "video":
                    video.write(payload)
                    last_video_wall = time.monotonic()
                    summary.video_frames += 1
                    summary.video_bytes += len(payload)
                    summary.keyframes += int(keyframe)
                    if not summary.first_video_time_us:
                        summary.first_video_time_us = timestamp_us
                    summary.last_video_time_us = timestamp_us
                    if summary.video_frames >= next_continue_frame:
                        self.send(KIND_PLAYBACK_CONTINUE, request_id)
                        summary.continuation_commands += 1
                        while next_continue_frame <= summary.video_frames:
                            next_continue_frame += CONTINUATION_INTERVAL_FRAMES
                elif media_type == "audio":
                    audio.write(payload)
                    summary.audio_frames += 1
                    summary.audio_bytes += len(payload)
                    summary.has_audio = True
                    if not summary.first_audio_time_us:
                        summary.first_audio_time_us = timestamp_us
                    summary.last_audio_time_us = timestamp_us
                else:
                    summary.housekeeping_frames += 1
                if summary.video_frames and summary.video_frames % timing_interval_frames == 0:
                    write_timing()
                latest = max(summary.last_video_time_us, summary.last_audio_time_us)
                enough_video = summary.video_frames >= max(2, int(duration * 25 * 0.75))
                audio_ok = not summary.has_audio or summary.audio_frames >= max(2, int(duration * 25 * 0.60))
                if latest >= target_end_us - 40_000 and enough_video and audio_ok:
                    break
            else:
                summary.timed_out = True

        if self.reader is not None:
            summary.fragmented_objects = self.reader.fragmented_objects
            summary.fragment_chunks = self.reader.fragment_chunks
        # There is no proven playback-stop command. Closing the TCP session is
        # the clean termination mechanism used by this read-only client.
        write_timing()
        _atomic_json(summary_path, summary.as_dict())
        return summary

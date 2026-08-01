from __future__ import annotations

import json
import struct
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "host" / "app"
sys.path.insert(0, str(APP))

import native9008  # noqa: E402


def video_frame(timestamp_us: int) -> native9008.InnerFrame:
    payload = b"\x00\x00\x00\x01\x65" + b"\x88" * 16
    body = bytearray(native9008.MEDIA_HEADER_SIZE + len(payload))
    body[:4] = b"\x00\x00\x00\x01"
    struct.pack_into("<Q", body, 16, timestamp_us)
    struct.pack_into("<I", body, 32, len(payload))
    body[native9008.MEDIA_HEADER_SIZE :] = payload
    return native9008.InnerFrame(
        native9008.KIND_RECORDED_MEDIA,
        11,
        native9008.TARGET_CAMERA,
        bytes(body),
    )


class NativePlaybackTests(unittest.TestCase):
    def test_continuation_every_25_video_frames_and_no_0907(self) -> None:
        client = native9008.TVT9008Client("camera", 9008, "user", "password")
        sent: list[tuple[int, int, bytes]] = []
        client.send = lambda kind, request_id, body=b"": sent.append((kind, request_id, body))  # type: ignore[method-assign]
        client.wait_for = lambda kind, request_id, timeout: native9008.InnerFrame(kind, request_id or 0, 3, b"")  # type: ignore[method-assign]

        start = datetime(2026, 7, 30, 6, 49, 14)
        start_epoch = int(time.mktime(start.timetuple()))
        frames = iter(
            video_frame(start_epoch * 1_000_000 + index * 40_000)
            for index in range(51)
        )
        client.read_frame = lambda: next(frames)  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as directory:
            summary = client.capture(start, 2, Path(directory))
            timing = json.loads((Path(directory) / "timing.json").read_text())
            saved = json.loads((Path(directory) / "summary.json").read_text())

        kinds = [kind for kind, _, _ in sent]
        self.assertEqual(kinds[0], native9008.KIND_PLAYBACK_START)
        self.assertEqual(kinds[1:], [native9008.KIND_PLAYBACK_CONTINUE] * 2)
        self.assertEqual(summary.video_frames, 50)
        self.assertEqual(summary.continuation_commands, 2)
        self.assertEqual(saved["continuation_commands"], 2)
        self.assertGreaterEqual(timing["captured_seconds"], 1.95)
        self.assertNotIn(0x00000907, kinds)
        self.assertFalse(hasattr(native9008, "KIND_PLAYBACK_FLOW"))

    def test_connect_closes_socket_when_login_fails(self) -> None:
        class FakeSocket:
            def __init__(self) -> None:
                self.closed = False

            def setsockopt(self, *args) -> None:
                return None

            def settimeout(self, *args) -> None:
                return None

            def recv(self, size: int) -> bytes:
                greeting = bytearray(native9008.HEAD_SIZE)
                greeting[:4] = native9008.HEAD_MAGIC
                greeting[native9008.KEY_OFFSET : native9008.KEY_OFFSET + 4] = b"abcd"
                return bytes(greeting)

            def sendall(self, data: bytes) -> None:
                return None

            def shutdown(self, how: int) -> None:
                return None

            def close(self) -> None:
                self.closed = True

        fake = FakeSocket()
        original = native9008.socket.create_connection
        native9008.socket.create_connection = lambda *args, **kwargs: fake  # type: ignore[assignment]
        try:
            client = native9008.TVT9008Client("camera", 9008, "user", "password")
            client.wait_for = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("login failed"))  # type: ignore[method-assign]
            with self.assertRaisesRegex(RuntimeError, "login failed"):
                client.connect()
            self.assertTrue(fake.closed)
            self.assertIsNone(client.sock)
            self.assertIsNone(client.reader)
        finally:
            native9008.socket.create_connection = original


if __name__ == "__main__":
    unittest.main()

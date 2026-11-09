from __future__ import annotations

import io
import struct
import sys
import tempfile
import time
import unittest
import unittest.mock
from datetime import UTC, datetime, timedelta
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "custom_components" / "tvt_archive" / "bridge"
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
    def test_search_uses_the_requested_record_types(self) -> None:
        start = datetime(2026, 9, 5)
        xml = native9008._search_xml(start, start + timedelta(hours=1), ("motion", "a<b"))
        self.assertIn(b"<item>motion</item><item>a&lt;b</item>", xml)
        self.assertNotIn(b"schedule", xml)

    def test_clock_rule_matches_the_camera_clock(self) -> None:
        fields = {
            "timeZone": "EET-2",
            "startTime": "M4.5.5/0",
            "endTime": "M10.5.5/0",
            "offSet": "60",
            "daylightSwitch": "2",
        }
        dst = "EET-2DST-3:00,M4.5.5/0,M10.5.5/0"

        def rule(local: str, utc: str, **changes: str) -> str | None:
            now = datetime.fromisoformat(utc).replace(tzinfo=UTC).timestamp()
            return native9008.clock_rule({**fields, "currentTime": local, **changes}, now)

        self.assertEqual(rule("2026-11-09 02:52:13", "2026-11-09 00:52:14"), dst)
        self.assertEqual(rule("2026-07-09 03:52:13", "2026-07-09 00:52:14"), dst)
        self.assertEqual(rule("2026-07-09 02:52:13", "2026-07-09 00:52:14"), "EET-2")
        self.assertEqual(
            rule("2026-07-09 02:52:13", "2026-07-09 00:52:14", daylightSwitch="0"), "EET-2"
        )
        self.assertEqual(
            rule("2026-11-09 08:52:13", "2026-11-09 00:52:14", timeZone="GMT+08:00"), "CAM-8:00"
        )
        self.assertEqual(rule("2000-01-01 00:00:09", "2026-11-09 00:52:14"), dst)
        self.assertEqual(native9008.clock_rule({"timeZone": "EET-2"}, 0), "EET-2")
        self.assertEqual(
            rule("2026-11-09 02:52:13", "2026-11-09 00:52:14", daylightSwitch="false"), "EET-2"
        )
        full = "EET-2EEST,M3.5.0/3,M10.5.0/4"
        nvms = {"timeZone": full, "currentTime": "2026-07-09 03:52:13", "daylightSwitch": "true"}
        summer = datetime(2026, 7, 9, 0, 52, 14, tzinfo=UTC).timestamp()
        self.assertEqual(native9008.clock_rule(nvms, summer), full)
        winter = {**nvms, "currentTime": "2026-11-09 02:52:13", "daylightSwitch": "false"}
        self.assertEqual(native9008.clock_rule(winter, summer + 123 * 86400), "EET-2")
        self.assertIsNone(native9008.clock_rule({}, 0))
        zone = native9008.posix_zone(dst)
        for local, offset in (
            ("2026-04-24 00:30", 7200),
            ("2026-04-24 01:30", 10800),
            ("2026-10-30 00:30", 7200),
        ):
            self.assertEqual(
                datetime.fromisoformat(local).replace(tzinfo=zone).utcoffset(),
                timedelta(seconds=offset),
            )

    def test_clock_rule_reads_date_and_time(self) -> None:
        body = (
            b'<?xml version="1.0" encoding="UTF-8"?><config version="1.7"><time><timezoneInfo>'
            b'<timeZone type="string" maxLen="127"><![CDATA[EET-2]]></timeZone>'
            b'<startTime type="string" maxLen="64"><![CDATA[M4.5.5/0]]></startTime>'
            b'<endTime type="string" maxLen="64"><![CDATA[M10.5.5/0]]></endTime>'
            b'<offSet type="uint16" min="30" max="120">60</offSet>'
            b'<daylightSwitch type="uint32">2</daylightSwitch></timezoneInfo><synchronizeInfo>'
            b'<currentTime type="string"><![CDATA[2026-11-09 02:52:13]]></currentTime>'
            b"</synchronizeInfo></time></config>"
        )
        client = native9008.TVT9008Client("camera", 9008, "admin", "secret")
        client.config_command = lambda name: native9008.InnerFrame(0, 0, 0, body)
        now = datetime(2026, 11, 9, 0, 52, 14, tzinfo=UTC).timestamp()
        with unittest.mock.patch.object(native9008.time, "time", return_value=now):
            self.assertEqual(client.clock_rule(), "EET-2DST-3:00,M4.5.5/0,M10.5.5/0")
        failed = b'<?xml version="1.0"?><config version="1.0" status="failed" errorCode="499"/>'
        client.config_command = lambda name: native9008.InnerFrame(0, 0, 0, failed)
        self.assertIsNone(client.clock_rule())

    def test_sd_card_status(self) -> None:
        client = native9008.TVT9008Client("192.0.2.1", 9008, "user", "pass")
        replies = {
            b"normal": (
                b"<config><types><sdCardStatus><enum>no card</enum></sdCardStatus></types>"
                b'<sdCardInfo><totalSize type="uint32">118763</totalSize>'
                b'<usedSize type="uint32">106883</usedSize>'
                b'<status type="sdCardStatus">normal</status></sdCardInfo></config>'
            ),
            b"no card": b"<config><sdCardInfo><status>no card</status></sdCardInfo></config>",
        }
        client.config_command = lambda name: native9008.InnerFrame(0, 0, 3, replies[key])
        key = b"normal"
        self.assertEqual(
            client.sd_card(), {"status": "normal", "total_gb": 116.0, "used_gb": 104.4}
        )
        key = b"no card"
        self.assertEqual(
            client.sd_card(), {"status": "no_sd_card", "total_gb": None, "used_gb": None}
        )

    def test_wifi_reads_the_connected_network(self) -> None:
        client = native9008.TVT9008Client("192.0.2.1", 9008, "user", "pass")
        replies = {
            "GetDeviceInfo": b'<config><supportWifi type="boolean">true</supportWifi></config>',
            "GetWifiListInfo": (
                b'<config><wifiList total="2" type="list"><itemType><ssid/><quality type="uint32"/>'
                b"</itemType><item><ssid><![CDATA[other]]></ssid><channel>1</channel>"
                b"<quality>99</quality><rates>300</rates><isConnected>0</isConnected></item>"
                b"<item><ssid><![CDATA[mine]]></ssid><channel>9</channel><quality>84</quality>"
                b"<rates>150</rates><isConnected>1</isConnected></item></wifiList></config>"
            ),
        }
        client.config_command = lambda name: native9008.InnerFrame(0, 0, 3, replies[name])
        self.assertEqual(client.wifi(), {"quality": 84, "rate_mbps": 150, "channel": 9})

        replies["GetWifiListInfo"] = b'<config><wifiList total="0" type="list"/></config>'
        self.assertIsNone(client.wifi())

        replies["GetWifiListInfo"] = b'<response status="failed" errorCode="499"/>'
        self.assertIsNone(client.wifi())

    def test_wifi_skips_devices_without_it(self) -> None:
        client = native9008.TVT9008Client("192.0.2.1", 9008, "user", "pass")
        for info in (
            b'<config><supportWifi type="boolean">false</supportWifi></config>',
            b"<config><model>wired</model></config>",
            b'<response status="failed" errorCode="499"/>',
        ):
            asked = []

            def command(name, info=info, asked=asked):
                asked.append(name)
                return native9008.InnerFrame(0, 0, 3, info)

            client.config_command = command
            self.assertIsNone(client.wifi())
            self.assertEqual(asked, ["GetDeviceInfo"])

    def test_record_types_tolerate_unquoted_attributes(self) -> None:
        client = native9008.TVT9008Client("192.0.2.1", 9008, "user", "pass")
        reply = (
            b'<?xml version="1.0"?>\r\n<config><recTypeCaps type="list" count=2>'
            b"<item>schedule</item><item>motion</item></recTypeCaps></config>"
        )
        client.config_command = lambda name: native9008.InnerFrame(0, 0, 3, reply)
        self.assertEqual(client.record_types(), ("schedule", "motion"))

    @staticmethod
    def _fragment(
        object_id: int,
        chunk_count: int,
        total_length: int,
        chunk_index: int,
        chunk: bytes,
    ) -> bytes:
        return (
            native9008.OUTER_MAGIC
            + struct.pack("<I", 0xFFFFFFFF)
            + struct.pack(
                "<IIIIII",
                object_id,
                chunk_count,
                total_length,
                chunk_index,
                len(chunk),
                0,
            )
            + chunk
        )

    def test_read_object_resumes_after_timeout_mid_object(self) -> None:
        frame = native9008._outer(struct.pack("<IIII", 1, 2, 3, 40) + b"x" * 40)

        class Flaky:
            def __init__(self, data: bytes) -> None:
                self.data, self.pos, self.failed = data, 0, False

            def read(self, size: int) -> bytes:
                if not self.failed and self.pos > 12:
                    self.failed = True
                    raise TimeoutError("timed out")
                chunk = self.data[self.pos : self.pos + min(size, 10)]
                self.pos += len(chunk)
                return chunk

        reader = native9008.ApplicationObjectReader(Flaky(frame + frame))
        with self.assertRaises(TimeoutError):
            reader.read_object()
        self.assertEqual(reader.read_object(), (frame[8:], False, None))
        self.assertEqual(reader.read_object(), (frame[8:], False, None))

    def test_fragment_padding_remains_compatible(self) -> None:
        reader = native9008.ApplicationObjectReader(
            io.BytesIO(self._fragment(1, 1, 5, 1, b"helloPAD"))
        )
        payload, fragmented, object_id = reader.read_object()
        self.assertEqual(payload, b"hello")
        self.assertTrue(fragmented)
        self.assertEqual(object_id, 1)
        self.assertEqual(reader.fragment_bytes, 0)

    def test_duplicate_fragment_does_not_double_count_memory(self) -> None:
        stream = (
            self._fragment(1, 2, 4, 1, b"ab")
            + self._fragment(1, 2, 4, 1, b"ab")
            + self._fragment(1, 2, 4, 2, b"cd")
        )
        reader = native9008.ApplicationObjectReader(io.BytesIO(stream))
        payload, _, _ = reader.read_object()
        self.assertEqual(payload, b"abcd")
        self.assertEqual(reader.fragment_bytes, 0)
        self.assertEqual(reader.fragment_chunks, 3)

    def test_conflicting_duplicate_fragment_is_rejected_and_released(self) -> None:
        stream = self._fragment(1, 2, 4, 1, b"ab") + self._fragment(1, 2, 4, 1, b"xy")
        reader = native9008.ApplicationObjectReader(io.BytesIO(stream))
        with self.assertRaisesRegex(native9008.TVT9008Error, "duplicate chunk"):
            reader.read_object()
        self.assertEqual(reader.fragment_bytes, 0)
        self.assertFalse(reader.fragments)

    def test_fragment_global_memory_limit_is_enforced(self) -> None:
        original = native9008.MAX_INFLIGHT_FRAGMENT_BYTES
        native9008.MAX_INFLIGHT_FRAGMENT_BYTES = 4
        try:
            stream = self._fragment(1, 2, 4, 1, b"abc") + self._fragment(2, 2, 4, 1, b"def")
            reader = native9008.ApplicationObjectReader(io.BytesIO(stream))
            with self.assertRaisesRegex(native9008.TVT9008Error, "memory limit"):
                reader.read_object()
            self.assertLessEqual(reader.fragment_bytes, 4)
        finally:
            native9008.MAX_INFLIGHT_FRAGMENT_BYTES = original

    def test_fragment_object_count_limit_is_enforced(self) -> None:
        original = native9008.MAX_INFLIGHT_FRAGMENT_OBJECTS
        native9008.MAX_INFLIGHT_FRAGMENT_OBJECTS = 1
        try:
            stream = self._fragment(1, 2, 4, 1, b"a") + self._fragment(2, 2, 4, 1, b"b")
            reader = native9008.ApplicationObjectReader(io.BytesIO(stream))
            with self.assertRaisesRegex(native9008.TVT9008Error, "Too many incomplete"):
                reader.read_object()
        finally:
            native9008.MAX_INFLIGHT_FRAGMENT_OBJECTS = original

    def test_stale_fragment_memory_is_expired(self) -> None:
        reader = native9008.ApplicationObjectReader(io.BytesIO())
        reader.fragments[7] = native9008.FragmentAssembly(
            2,
            4,
            {1: b"ab"},
            2,
            time.monotonic() - 120,
        )
        reader.fragment_bytes = 2
        reader._expire_fragments()
        self.assertFalse(reader.fragments)
        self.assertEqual(reader.fragment_bytes, 0)

    def _capture_client(self, frames):
        client = native9008.TVT9008Client("camera", 9008, "user", "password")
        sent: list[tuple[int, int, bytes]] = []
        client.send = lambda kind, request_id, body=b"": sent.append((kind, request_id, body))
        client.wait_for = lambda kind, request_id, timeout: native9008.InnerFrame(
            kind, request_id or 0, 3, b""
        )
        iterator = iter(frames)
        client.read_frame = lambda: next(iterator)
        return client, sent

    def test_continuation_follows_the_bag_end_marker(self) -> None:
        start = datetime(2026, 7, 30, 6, 49, 14)
        base = int(time.mktime(start.timetuple())) * 1_000_000
        marker = native9008.InnerFrame(
            native9008.KIND_RECORDED_MEDIA, 11, 3, bytes([0, 0, 0, 2]) + bytes(36)
        )
        frames = [video_frame(base + index * 40_000) for index in range(40)]
        frames.insert(20, marker)
        frames.append(video_frame(base + 60 * 40_000))
        client, sent = self._capture_client(frames)
        with tempfile.TemporaryDirectory() as directory:
            summary = client.capture(start, 2, Path(directory))
        kinds = [kind for kind, _, _ in sent]
        self.assertEqual(
            kinds,
            [
                native9008.KIND_PLAYBACK_START,
                native9008.KIND_PLAYBACK_CONTINUE,
                native9008.KIND_PLAYBACK_STOP,
            ],
        )
        self.assertEqual(summary.continuation_commands, 1)
        self.assertEqual(summary.housekeeping_frames, 0)
        self.assertNotIn(0x00000907, kinds)

    def test_continuation_nudges_a_silent_camera_without_markers(self) -> None:
        start = datetime(2026, 7, 30, 6, 49, 14)
        base = int(time.mktime(start.timetuple())) * 1_000_000
        first = [video_frame(base + index * 40_000) for index in range(100)]
        rest = [video_frame(base + index * 40_000) for index in range(100, 160)]
        client, sent = self._capture_client(first)
        produced = iter(first)

        def read_frame():
            try:
                return next(produced)
            except StopIteration:
                if any(kind == native9008.KIND_PLAYBACK_CONTINUE for kind, _, _ in sent) and rest:
                    return rest.pop(0)
                time.sleep(0.05)
                raise TimeoutError("timed out") from None

        client.read_frame = read_frame
        with tempfile.TemporaryDirectory() as directory:
            summary = client.capture(start, 6, Path(directory))
        kinds = [kind for kind, _, _ in sent]
        self.assertEqual(kinds.count(native9008.KIND_PLAYBACK_CONTINUE), 1)
        self.assertGreaterEqual(summary.video_frames, 150)

    def test_midstream_video_stall_raises_timeout(self) -> None:
        client = native9008.TVT9008Client("camera", 9008, "user", "password")
        client.send = lambda *args, **kwargs: None
        client.wait_for = lambda kind, request_id, timeout: native9008.InnerFrame(
            kind, request_id or 0, 3, b""
        )

        start = datetime(2026, 7, 30, 6, 49, 14)
        start_epoch = int(time.mktime(start.timetuple()))
        first = True

        def read_frame():
            nonlocal first
            if first:
                first = False
                return video_frame(start_epoch * 1_000_000)
            raise native9008.socket.timeout()

        client.read_frame = read_frame
        original = native9008.NATIVE_VIDEO_STALL_SECONDS
        native9008.NATIVE_VIDEO_STALL_SECONDS = 0.02
        try:
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(TimeoutError, "Archive video stalled"):
                    client.capture(start, 10, Path(directory))
        finally:
            native9008.NATIVE_VIDEO_STALL_SECONDS = original

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
        native9008.socket.create_connection = lambda *args, **kwargs: fake
        try:
            client = native9008.TVT9008Client("camera", 9008, "user", "password")
            client.wait_for = lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("login failed")
            )
            with self.assertRaisesRegex(RuntimeError, "login failed"):
                client.connect()
            self.assertTrue(fake.closed)
            self.assertIsNone(client.sock)
            self.assertIsNone(client.reader)
            client = native9008.TVT9008Client("camera", 9008, "user", "password")
            client.wait_for = lambda *args, **kwargs: native9008.InnerFrame(
                native9008.KIND_LOGIN_FAILED, native9008.LOGIN_REQUEST_ID, 3, b"\x02\x00\x00\x00"
            )
            with self.assertRaisesRegex(native9008.TVT9008AuthError, r"code 2"):
                client.connect()
        finally:
            native9008.socket.create_connection = original


if __name__ == "__main__":
    unittest.main()

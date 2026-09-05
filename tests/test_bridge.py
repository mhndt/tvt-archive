from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "host" / "app"
TEMP = tempfile.TemporaryDirectory()
BASE = Path(TEMP.name)
CONFIG = BASE / "config"
STATE = BASE / "state"
CACHE = BASE / "cache"
CONFIG.mkdir()
(CONFIG / "config.json").write_text(
    json.dumps(
        {
            "server": {"bind": "127.0.0.1", "port": 18099, "token": "test-token"},
            "processing": {"encoder": "software", "max_parallel_jobs": 1},
            "cameras": [
                {
                    "id": "front_door",
                    "name": "Front Door",
                    "host": "192.0.2.10",
                    "port": 9008,
                    "username": "test",
                    "password": "not-a-real-secret",
                }
            ],
        }
    ),
    encoding="utf-8",
)
os.environ.update(
    {
        "TVT_ARCHIVE_BASE": str(ROOT / "host"),
        "CREDENTIALS_DIRECTORY": str(CONFIG),
        "STATE_DIRECTORY": str(STATE),
        "CACHE_DIRECTORY": str(CACHE),
    }
)
sys.path.insert(0, str(APP))
bridge = importlib.import_module("bridge")


class BridgeTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        bridge.EXECUTOR.shutdown(wait=False, cancel_futures=True)
        bridge.PLAYBACK_EXECUTOR.shutdown(wait=False, cancel_futures=True)
        TEMP.cleanup()

    def test_load_config_creates_file_with_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            created = bridge.load_config(path)
            self.assertGreaterEqual(len(created["server"]["token"]), 32)
            self.assertEqual(created["cameras"], [])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), created)

    def test_load_config_migrates_accelerator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "server": {"token": "t"},
                        "processing": {"accelerator": "vaapi_full", "vaapi_driver": "iHD"},
                    }
                ),
                encoding="utf-8",
            )
            processing = bridge.load_config(path)["processing"]
            self.assertEqual(processing, {"encoder": "vaapi"})

    def test_load_config_drops_rtsp_camera_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "server": {"token": "t"},
                        "cameras": [{"id": "a", "archive_backend": "rtsp", "rtsp_port": 554}],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(bridge.load_config(path)["cameras"], [{"id": "a"}])

    def test_authorization_requires_bearer_header(self) -> None:
        self.assertTrue(bridge.authorized("Bearer test-token"))
        self.assertFalse(bridge.authorized("Bearer wrong"))
        self.assertFalse(bridge.authorized("test-token"))
        self.assertFalse(bridge.authorized(""))

    def test_job_public_reports_real_percent(self) -> None:
        job = bridge.Job("a" * 32, "front_door", "cache", {"duration": 60})
        job.status = "running"
        job.progress = 0.437
        job.captured_seconds = 26.2
        public = job.public()
        self.assertEqual(public["progress_percent"], 44)
        self.assertEqual(public["captured_seconds"], 26.2)
        job.status = "ready"
        self.assertEqual(job.public()["progress_percent"], 100)

    def test_capture_span_prefers_explicit_value(self) -> None:
        self.assertEqual(bridge._captured_span_seconds({"captured_seconds": 12.5}), 12.5)
        self.assertAlmostEqual(
            bridge._captured_span_seconds(
                {
                    "first_video_time_us": 1_000_000,
                    "first_audio_time_us": 1_250_000,
                    "last_video_time_us": 4_000_000,
                    "last_audio_time_us": 3_900_000,
                }
            ),
            3.0,
        )

    def test_readable_export_filename(self) -> None:
        name = bridge.export_filename("Front Door / East", datetime(2026, 7, 30, 6, 49, 14), 16)
        self.assertEqual(
            name,
            "Front-Door-East_2026-07-30_06-49-14_to_2026-07-30_06-49-30.mp4",
        )

    def test_low_quality_uses_software_x264_by_default(self) -> None:
        pre, out = bridge.video_args("low", "software")
        self.assertEqual(pre, [])
        self.assertIn("libx264", out)
        self.assertIn("scale=854:480:flags=fast_bilinear", out)

    def test_vaapi_encoder_uses_full_gpu_pipeline(self) -> None:
        pre, out = bridge.video_args("low", "vaapi")
        joined = " ".join(pre + out)
        self.assertIn("-hwaccel_output_format vaapi", joined)
        self.assertIn("scale_vaapi=w=854:h=480:format=nv12", joined)
        self.assertIn("-c:v h264_vaapi", joined)

    def test_copy_mode_never_encodes(self) -> None:
        self.assertEqual(bridge.video_args("original", "copy"), ([], ["-c:v", "copy"]))

    def test_playback_copies_only_with_short_learned_gop(self) -> None:
        capability = bridge._camera_capabilities_path("front_door")
        capability.unlink(missing_ok=True)
        self.assertEqual(bridge.playback_video_mode("front_door"), bridge.ENCODER)
        bridge._remember_capability("front_door", "gop_seconds", 2.0)
        self.assertEqual(bridge.playback_video_mode("front_door"), "copy")
        bridge._remember_capability("front_door", "gop_seconds", 8.0)
        self.assertEqual(bridge.playback_video_mode("front_door"), bridge.ENCODER)

    def test_metadata_and_media_locks_are_independent(self) -> None:
        self.assertIsNot(
            bridge.camera_session_slot("front_door"), bridge.metadata_lock("front_door")
        )

    def test_one_camera_session_at_a_time_by_default(self) -> None:
        slot = bridge.camera_session_slot("front_door")
        self.assertEqual(bridge.NATIVE_SESSION_LIMIT, 1)
        self.assertTrue(slot.acquire(blocking=False))
        try:
            self.assertFalse(slot.acquire(blocking=False))
        finally:
            slot.release()

    def test_frame_record_round_trip(self) -> None:
        record = bridge.frame_record(bridge.REC_VIDEO, bridge.KEYFRAME_FLAG, 1234567, b"abc")
        magic, kind, flags, pts, length = bridge.FRAME_HEADER.unpack_from(record)
        self.assertEqual((magic, kind, flags, pts, length), (b"TF", 1, 1, 1234567, 3))
        self.assertEqual(record[bridge.FRAME_HEADER.size :], b"abc")

    def test_next_bag_waits_for_the_viewer(self) -> None:
        self.assertTrue(bridge.should_release_bag(True, 100, 0, True))
        self.assertFalse(bridge.should_release_bag(True, 250, 0, True))
        self.assertTrue(bridge.should_release_bag(True, 250, 120, True))
        self.assertFalse(bridge.should_release_bag(True, 100, 0, False))
        self.assertFalse(bridge.should_release_bag(False, 0, 0, True))

    def test_access_units_split_on_aud(self) -> None:
        aud = b"\x00\x00\x00\x01\x09\xf0"
        idr = b"\x00\x00\x00\x01\x65\x88"
        p_frame = b"\x00\x00\x01\x41\x9a"
        stream = bytearray(aud + idr + aud + p_frame + aud + p_frame[:3])
        units = bridge.split_access_units(stream)
        self.assertEqual(units, [aud + idr, aud + p_frame])
        self.assertEqual(bytes(stream), aud + p_frame[:3])
        self.assertTrue(bridge.access_unit_is_keyframe(units[0]))
        self.assertFalse(bridge.access_unit_is_keyframe(units[1]))

    def test_stream_request_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            bridge.create_frame_session("front_door", {"start": "not a time"})
        with self.assertRaises(ValueError):
            bridge.create_frame_session(
                "front_door", {"start": "2026-09-05T02:00:00", "quality": "4k"}
            )
        with self.assertRaises(KeyError):
            bridge.create_frame_session("nope", {"start": "2026-09-05T02:00:00"})

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_completed_export_cross_device_fallback_is_atomic(self) -> None:
        source = bridge.WORK / "cross-device-source.mp4"
        destination = bridge.CACHE / "cross-device-destination.mp4"
        payload = b"test-export" * 1024
        source.write_bytes(payload)
        destination.unlink(missing_ok=True)
        real_replace = bridge.os.replace
        calls = {"count": 0}

        def replace_with_exdev_once(src, dst):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError(18, "Invalid cross-device link")
            return real_replace(src, dst)

        with patch.object(bridge.os, "replace", side_effect=replace_with_exdev_once):
            bridge.promote_completed_file(source, destination)

        self.assertFalse(source.exists())
        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(calls["count"], 2)
        destination.unlink(missing_ok=True)

    def test_hls_playlist_readiness_is_sticky(self) -> None:
        with tempfile.TemporaryDirectory(dir=bridge.WORK) as directory:
            session = bridge.PlaybackSession(
                "b" * 32,
                "front_door",
                {"duration": 60, "quality": "original"},
                directory,
            )
            path = Path(directory)
            (path / "segment-00000.m4s").write_bytes(b"x")
            (path / "index.m3u8").write_text(
                "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-PLAYLIST-TYPE:EVENT\n#EXTINF:4.2,\nsegment-00000.m4s\n",
                encoding="utf-8",
            )
            self.assertTrue(session.playlist_ready())
            (path / "index.m3u8").unlink()
            self.assertTrue(session.playlist_ready())

    def test_hls_encode_forces_segment_length_gop(self) -> None:
        _, out = bridge.video_args("original", "software", 25)
        self.assertIn("libx264", out)
        self.assertEqual(out[out.index("-g") + 1], "25")
        self.assertIn("-force_key_frames", out)
        self.assertIn("-sc_threshold", out)

    def test_timing_probe_uses_reported_fps_and_audio_without_waiting_full_legacy_deadline(
        self,
    ) -> None:
        class RunningProcess:
            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory(dir=bridge.WORK) as directory:
            path = Path(directory)
            timing = {
                "video_frames": bridge.HLS_TIMING_SAMPLE_FRAMES,
                "audio_frames": bridge.HLS_TIMING_SAMPLE_FRAMES - 1,
                "first_video_time_us": 1_000_000,
                "last_video_time_us": 1_000_000,
                "first_audio_time_us": 1_253_000,
                "last_audio_time_us": 1_600_000,
                "source_fps": 25.0,
                "has_audio": True,
            }
            (path / "timing.json").write_text(json.dumps(timing), encoding="utf-8")
            phases: list[str] = []
            started = __import__("time").monotonic()
            fps, offset, has_audio = bridge._wait_for_capture_timing(
                path,
                RunningProcess(),
                __import__("threading").Event(),
                phase_callback=phases.append,
            )
            elapsed = __import__("time").monotonic() - started
            self.assertLess(elapsed, 0.5)
            self.assertAlmostEqual(fps, 25.0)
            self.assertEqual(offset, 253)
            self.assertTrue(has_audio)
            self.assertEqual(phases, ["Measuring archive frame timing"])

    def test_timing_probe_defaults_are_short_after_first_video(self) -> None:
        self.assertEqual(bridge.HLS_FIRST_MEDIA_TIMEOUT_SECONDS, 30.0)
        self.assertEqual(bridge.HLS_TIMING_MAX_SECONDS, 2.5)
        self.assertEqual(bridge.HLS_AUDIO_PROBE_MEDIA_SECONDS, 2.0)

    def test_recording_audio_defaults_to_auto_and_learns_positive_capability(self) -> None:
        capability = bridge._camera_capabilities_path("front_door")
        capability.unlink(missing_ok=True)
        self.assertEqual(bridge.recording_audio_mode("front_door"), "auto")
        self.assertFalse(bridge._learned_archive_audio("front_door"))
        bridge._remember_archive_audio("front_door")
        self.assertTrue(bridge._learned_archive_audio("front_door"))

    def test_archive_audio_alignment_uses_source_timeline(self) -> None:
        timing = {
            "first_video_time_us": 1_000_000,
            "last_video_time_us": 3_100_000,
            "first_audio_time_us": 3_050_000,
        }
        self.assertEqual(bridge._audio_video_elapsed_samples(timing), 16_800)
        self.assertEqual(bridge._audio_offset_samples(timing), 16_400)
        self.assertAlmostEqual(bridge._video_media_elapsed_seconds(timing), 2.1)

    def test_archive_audio_capability_is_per_camera(self) -> None:
        front = bridge._camera_capabilities_path("front_door")
        garage = bridge._camera_capabilities_path("garage")
        front.unlink(missing_ok=True)
        garage.unlink(missing_ok=True)
        bridge._remember_archive_audio("front_door")
        self.assertTrue(bridge._learned_archive_audio("front_door"))
        self.assertFalse(bridge._learned_archive_audio("garage"))

    def test_hls_feeder_alignment_prevents_double_positive_delay(self) -> None:
        with tempfile.TemporaryDirectory(dir=bridge.WORK) as directory:
            session = bridge.PlaybackSession(
                "c" * 32,
                "front_door",
                {"duration": 60, "quality": "original", "gain_db": 0},
                directory,
            )
            session.source_fps = 25.0
            session.has_audio = True
            session.audio_offset_ms = 2053
            session.audio_alignment_in_feeder = True
            session.video = "software"
            command = bridge._hls_command("pipe:3", "pipe:4", session)
            joined = " ".join(command)
            self.assertNotIn("adelay=2053", joined)
            self.assertIn("aresample=async=1:first_pts=0", joined)


if __name__ == "__main__":
    unittest.main()

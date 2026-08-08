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
            "processing": {"accelerator": "auto", "max_parallel_jobs": 1},
            "cameras": [{
                "id": "front_door",
                "name": "Front Door",
                "host": "192.0.2.10",
                "port": 9008,
                "channel": 0,
                "username": "test",
                "password": "not-a-real-secret",
                "archive_backend": "native_9008",
                "live_profiles": [],
            }],
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
        name = bridge.export_filename(
            "Front Door / East", datetime(2026, 7, 30, 6, 49, 14), 16, "original"
        )
        self.assertEqual(
            name,
            "Front-Door-East_2026-07-30_06-49-14_to_2026-07-30_06-49-30_Original.mp4",
        )

    def test_full_vaapi_command_uses_proven_gpu_only_pipeline(self) -> None:
        with patch.object(bridge, "acceleration_capabilities", return_value={"selected": "vaapi_full"}):
            pre, out, selected = bridge.transcode_video_args("balanced")
        self.assertEqual(selected, "vaapi_full")
        joined = " ".join(pre + out)
        self.assertIn("-hwaccel_output_format vaapi", joined)
        self.assertIn("scale_vaapi=w=1280:h=720:format=nv12", joined)
        self.assertIn("-c:v h264_vaapi", joined)
        self.assertNotIn("hwdownload", joined)
        self.assertNotIn("hwupload", joined)

    def test_original_video_is_always_copied(self) -> None:
        pre, out, selected = bridge.transcode_video_args("original")
        self.assertEqual(pre, [])
        self.assertEqual(out, ["-c:v", "copy"])
        self.assertEqual(selected, "copy")

    def test_metadata_and_media_locks_are_independent(self) -> None:
        self.assertIsNot(bridge.camera_session_slot("front_door"), bridge.metadata_lock("front_door"))

    def test_two_native_sessions_are_allowed_but_third_waits(self) -> None:
        slot = bridge.camera_session_slot("front_door")
        self.assertEqual(bridge.NATIVE_SESSION_LIMIT, 2)
        self.assertTrue(slot.acquire(blocking=False))
        self.assertTrue(slot.acquire(blocking=False))
        try:
            self.assertFalse(slot.acquire(blocking=False))
        finally:
            slot.release()
            slot.release()

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
                "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-PLAYLIST-TYPE:EVENT\n#EXTINF:4.2,\nsegment-00000.m4s\n", encoding="utf-8"
            )
            self.assertTrue(session.playlist_ready())
            (path / "index.m3u8").unlink()
            self.assertTrue(session.playlist_ready())

    def test_original_hls_uses_short_gop_encoder_while_exports_still_copy(self) -> None:
        with patch.object(bridge, "acceleration_capabilities", return_value={"selected": "software"}):
            pre, out, selected = bridge.hls_video_pipeline("original", source_fps=25.0)
        self.assertEqual(pre, [])
        self.assertEqual(selected, "software")
        self.assertIn("libx264", out)
        self.assertNotIn("copy", out)
        self.assertEqual(out[out.index("-g") + 1], "25")
        self.assertIn("-force_key_frames", out)
        file_pre, file_out, file_selected = bridge.transcode_video_args("original")
        self.assertEqual((file_pre, file_out, file_selected), ([], ["-c:v", "copy"], "copy"))

    def test_hls_gop_tracks_segment_length(self) -> None:
        with patch.object(bridge, "acceleration_capabilities", return_value={"selected": "software"}):
            _, out, _ = bridge.hls_video_pipeline("data_saver", source_fps=25.0)
        index = out.index("-g")
        self.assertEqual(out[index + 1], str(round(25.0 * bridge.HLS_SEGMENT_SECONDS)))

    def test_timing_probe_uses_reported_fps_and_audio_without_waiting_full_legacy_deadline(self) -> None:
        class RunningProcess:
            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory(dir=bridge.WORK) as directory:
            path = Path(directory)
            timing = {
                "backend": "native_9008",
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

    def test_v083_hls_startup_defaults_and_retry_are_persisted(self) -> None:
        text = (ROOT / "host" / "app" / "bridge.py").read_text(encoding="utf-8")
        self.assertIn('PROCESSING.get("hls_start_buffer_seconds", 2)', text)
        self.assertIn('PROCESSING.get("hls_timing_sample_frames", 8)', text)
        self.assertIn('PROCESSING.get("hls_first_media_timeout_seconds", 30.0)', text)
        self.assertIn('PROCESSING.get("hls_first_media_retries", 1)', text)
        self.assertIn('"split_by_time+temp_file"', text)
        self.assertIn("Retrying camera archive", text)

    def test_hls_completion_requires_successful_capture_process(self) -> None:
        text = (ROOT / "host" / "app" / "bridge.py").read_text(encoding="utf-8")
        self.assertIn("ffmpeg_rc == 0 and capture_rc == 0 and session.playlist_ready()", text)
        self.assertNotIn("elif ffmpeg_rc == 0 and session.playlist_ready()", text)
        self.assertIn("The media pipeline ended before archive capture completed.", text)

    def test_recording_audio_defaults_to_auto_and_learns_positive_capability(self) -> None:
        capability = bridge._camera_capabilities_path("front_door")
        capability.unlink(missing_ok=True)
        self.assertEqual(bridge.recording_audio_mode("front_door"), "auto")
        self.assertFalse(bridge._learned_archive_audio("front_door"))
        bridge._remember_archive_audio("front_door")
        self.assertTrue(bridge._learned_archive_audio("front_door"))

    def test_archive_audio_alignment_uses_source_timeline(self) -> None:
        timing={"first_video_time_us":1_000_000,"last_video_time_us":3_100_000,"first_audio_time_us":3_050_000}
        self.assertEqual(bridge._audio_video_elapsed_samples(timing),16_800)
        self.assertEqual(bridge._audio_offset_samples(timing),16_400)
        self.assertAlmostEqual(bridge._video_media_elapsed_seconds(timing),2.1)

    def test_archive_audio_capability_is_per_camera(self) -> None:
        front=bridge._camera_capabilities_path("front_door"); garage=bridge._camera_capabilities_path("garage")
        front.unlink(missing_ok=True); garage.unlink(missing_ok=True)
        bridge._remember_archive_audio("front_door")
        self.assertTrue(bridge._learned_archive_audio("front_door")); self.assertFalse(bridge._learned_archive_audio("garage"))

    def test_hls_feeder_alignment_prevents_double_positive_delay(self) -> None:
        with tempfile.TemporaryDirectory(dir=bridge.WORK) as directory:
            session=bridge.PlaybackSession("c"*32,"front_door",{"duration":60,"quality":"original","gain_db":0},directory)
            session.source_fps=25.0; session.has_audio=True; session.audio_offset_ms=2053; session.audio_alignment_in_feeder=True
            with patch.object(bridge,"acceleration_capabilities",return_value={"selected":"software"}): command,_=bridge._hls_command("pipe:3","pipe:4",session)
            joined=" ".join(command); self.assertNotIn("adelay=2053",joined); self.assertIn("aresample=async=1:first_pts=0",joined)


if __name__ == "__main__":
    unittest.main()

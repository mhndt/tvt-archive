from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "host" / "app"
sys.path.insert(0, str(APP))
archive_capture = importlib.import_module("archive_capture")


class ReleaseStaticTests(unittest.TestCase):
    def test_entrypoint_persists_vaapi_driver_preference(self) -> None:
        text = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn('"vaapi_driver": os.environ.get("TVT_ARCHIVE_VAAPI_DRIVER", "auto")', text)
        self.assertIn('processing.get("vaapi_driver", "auto")', text)

    def test_low_latency_archive_defaults_are_persisted(self) -> None:
        entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        bridge = (ROOT / "host" / "app" / "bridge.py").read_text(encoding="utf-8")
        self.assertIn('"hls_start_buffer_seconds": 3', entrypoint)
        self.assertIn('PROCESSING.get("hls_start_buffer_seconds", 3)', bridge)
        self.assertIn('"hls_timing_sample_frames": 10', entrypoint)
        self.assertIn('PROCESSING.get("hls_timing_sample_frames", 10)', bridge)
        self.assertIn('"hls_first_media_timeout_seconds": 15.0', entrypoint)
        self.assertIn('"hls_timing_max_seconds": 2.5', entrypoint)
        self.assertIn('"hls_audio_detect_seconds": 1.0', entrypoint)
        self.assertIn('PROCESSING.get("hls_first_media_timeout_seconds", 15.0)', bridge)
        self.assertIn('PROCESSING.get("hls_timing_max_seconds", 2.5)', bridge)
        self.assertIn('PROCESSING.get("hls_audio_detect_seconds", 1.0)', bridge)

    def test_home_assistant_job_errors_are_translated(self) -> None:
        text = (ROOT / "custom_components" / "tvt_archive" / "http.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("class CreateJobView", text)
        self.assertIn("class JobView", text)
        self.assertGreaterEqual(text.count("except TVTArchiveApiError as error:"), 6)
        self.assertIn("_MEDIA_URL_TTL_SECONDS = 2 * 60 * 60", text)

    def test_optional_intel_driver_build_does_not_block_other_architectures(self) -> None:
        text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("INTEL_MEDIA_DRIVER_VERSION=25.1.2", text)
        self.assertNotIn("INTEL_MEDIA_DRIVER_VERSION=25.1.4", text)
        self.assertIn('if [ "$(uname -m)" = "x86_64" ]', text)
        self.assertIn('Skipping optional Intel media-driver build', text)
        self.assertIn('sysconfig.get_config_var("MULTIARCH")', text)
        self.assertNotIn('LIBVA_DRIVERS_PATH=/usr/lib/x86_64-linux-gnu/dri', text)
        self.assertIn('driver_path="/opt/intel/lib/${multiarch}/dri/iHD_drv_video.so"', text)
        self.assertIn('driver_path="/usr/lib/${multiarch}/dri/iHD_drv_video.so"', text)
        self.assertIn('libigfxcmrt.so*', text)


    def test_hlsjs_is_pinned_verified_and_served_locally(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        bridge = (ROOT / "host" / "app" / "bridge.py").read_text(encoding="utf-8")
        api = (ROOT / "custom_components" / "tvt_archive" / "api.py").read_text(encoding="utf-8")
        http = (ROOT / "custom_components" / "tvt_archive" / "http.py").read_text(encoding="utf-8")
        self.assertIn("HLS_JS_VERSION=1.6.16", dockerfile)
        self.assertIn("HLS_JS_TARBALL_SHA512=552211a4", dockerfile)
        self.assertIn("sha512sum -c -", dockerfile)
        self.assertIn("HLSJS-LICENSE.txt", dockerfile)
        self.assertIn("package/dist/hls.min.js", dockerfile)
        self.assertIn('path == "/api/player/hls.js"', bridge)
        self.assertIn("async def open_player_script", api)
        self.assertIn("class HLSLibraryView", http)
        self.assertIn('result["player_script_url"]', http)

    def test_playback_urls_are_stable_for_the_session(self) -> None:
        bridge = (ROOT / "host" / "app" / "bridge.py").read_text(encoding="utf-8")
        http = (ROOT / "custom_components" / "tvt_archive" / "http.py").read_text(encoding="utf-8")
        self.assertIn('"created_at_unix": int(self.created_at)', bridge)
        self.assertIn('created_at = int(result.get("created_at_unix"', http)
        self.assertIn('expires = created_at + _HLS_URL_TTL_SECONDS', http)

    def test_prepared_file_disconnects_are_handled_cleanly(self) -> None:
        text = (ROOT / "host" / "app" / "bridge.py").read_text(encoding="utf-8")
        self.assertIn('server_version = "TVTArchiveBridge/0.8.1"', text)
        self.assertIn('except (BrokenPipeError, ConnectionResetError):', text)
        self.assertIn('Prepared-file client disconnected', text)

    def test_normal_compose_pulls_prebuilt_image(self) -> None:
        compose = (ROOT / "compose" / "compose.yaml").read_text(encoding="utf-8")
        build_override = (ROOT / "compose" / "build-local.yaml").read_text(encoding="utf-8")
        self.assertIn("ghcr.io/mhndt/tvt-archive:0.8.1", compose)
        self.assertNotIn("build:", compose)
        self.assertIn("build:", build_override)

    def test_native_capture_retries_initial_network_failures(self) -> None:
        text = (ROOT / "host" / "app" / "archive_capture.py").read_text(encoding="utf-8")
        self.assertIn('TVT_CAPTURE_CONNECT_ATTEMPTS', text)
        self.assertIn('Archive connection failed after {attempts} attempts', text)
        bridge = (ROOT / "host" / "app" / "bridge.py").read_text(encoding="utf-8")
        self.assertIn('Camera is temporarily unreachable on the LAN', bridge)

    def test_ffmpeg_progress_reads_microseconds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.txt"
            path.write_text("out_time_us=12500000\nprogress=continue\n", encoding="utf-8")
            self.assertEqual(archive_capture.ffmpeg_progress_seconds(path), 12.5)
            path.write_text("out_time_ms=7250000\nprogress=continue\n", encoding="utf-8")
            self.assertEqual(archive_capture.ffmpeg_progress_seconds(path), 7.25)


if __name__ == "__main__":
    unittest.main()

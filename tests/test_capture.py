from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "host" / "app"
sys.path.insert(0, str(APP))

import archive_capture  # noqa: E402


class CaptureTests(unittest.TestCase):
    def test_ffmpeg_progress_reads_microseconds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.txt"
            path.write_text("out_time_us=12500000\nprogress=continue\n", encoding="utf-8")
            self.assertEqual(archive_capture.ffmpeg_progress_seconds(path), 12.5)
            path.write_text("out_time_ms=7250000\nprogress=continue\n", encoding="utf-8")
            self.assertEqual(archive_capture.ffmpeg_progress_seconds(path), 7.25)


if __name__ == "__main__":
    unittest.main()

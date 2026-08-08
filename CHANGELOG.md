# Changelog

## 0.8.2

### Playback and reliability

- Added source-aware adaptive playback speed with a 0.45x emergency floor.
- Added held-frame starvation recovery while capture and HLS loading continue in the background.
- Removed repeated one-second edge backtracking during slow archive delivery.
- Reduced HLS startup target from 3 seconds to 2 seconds and timing sample from 10 frames to 8 frames.
- Increased first-media tolerance from 15 seconds to 30 seconds and added one fresh archive-session retry.
- Switched HLS fragment publication to `split_by_time+temp_file`.
- Retained native TCP/9008 `0x090A` continuation every 25 received video frames with no routine `0x0907`.

### Platform and compatibility

- Moved the generalized container media stack to Debian Trixie.
- Added Debian `intel-media-va-driver-non-free` on supported amd64 builds while retaining packaged VAAPI drivers and software H.264 fallback.
- Kept complete-pipeline probing for Intel/AMD VAAPI, Intel QSV, NVIDIA CUDA/NVENC, hybrid hardware paths, and software fallback.
- Verified Intel HD Graphics 530 with Debian Trixie FFmpeg 7.1.5, libva 2.22.0, and Intel media-driver 25.2.3.

### Security and hardening

- Validated camera IDs before filesystem use and avoided camera-controlled temporary path names.
- Stopped returning raw internal bridge exceptions for public HTTP 500 responses.
- Escaped dynamic recording/live-profile values in the Home Assistant panel.
- Removed an unnecessary quality response header and added hardening regression tests.

### Packaging and documentation

- Updated public version references to 0.8.2.
- Documented the Trixie media stack, GPU selection, startup/retry behavior, adaptive playback, held-frame recovery, and camera-side archive throughput limits.

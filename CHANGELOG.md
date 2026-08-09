# Changelog

## 0.8.4

### Recordings-only scope

- Removed Live view from the Recordings panel.
- Removed Home Assistant live-profile setup and management from TVT Archive.
- Stopped exposing or normalizing live-profile mappings in the bridge; existing camera archive settings continue to work normally.
- Live viewing is now intentionally left to Home Assistant camera integrations and dashboard cards.

## 0.8.3

### Mobile playback

- Improve mobile timeline spacing and keep native playback controls hidden during internal buffering pauses.

### Reliability

- Recover recording playback after mid-stream archive stalls instead of treating partial HLS output as a completed recording.

### Recording audio

- Added per-camera Native TCP/9008 recording-audio modes: Auto, Always expect audio, and Disabled.
- Made Auto learn only positive archive-audio capability and persist it independently for each configured camera.
- Replaced the one-second wall-clock audio decision with recording-timestamp-aware probing inside the existing startup timing window.
- Added media-timeline silence alignment so known late-starting G.711 A-law audio can be exposed from the beginning of HLS playback without duplicating the source offset.
- Preserved learned audio capability when ordinary camera details are edited and removed it when the camera is deleted.

### Home Assistant and packaging

- Render Live camera entities directly through Home Assistant camera WebRTC/HLS APIs, with MJPEG fallback, so the standalone sidebar panel does not depend on Lovelace card helpers.
- Version the frontend panel module URL from its file contents so Home Assistant clients do not reuse stale panel JavaScript after an update.
- Fixed the HACS release ZIP layout so integration files and local brand assets are at the expected ZIP root.
- Corrected container diagnostic commands and clarified the update workflow.
- Sanitized bridge proxy errors returned to clients while retaining detailed failures in Home Assistant logs.

### Player

- Recording player controls now hide automatically after two seconds of inactivity and reappear when the pointer moves over the player.

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

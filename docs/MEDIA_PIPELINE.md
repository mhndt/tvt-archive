# Media pipeline

This document covers what happens after the native reader has recovered the camera's raw archive media. Wire-level TCP/9008 details are kept in [Native TCP/9008 archive protocol](NATIVE_9008_PROTOCOL.md).

## Native capture inputs

The capture stage supplies:

```text
video.h264   H.264 Annex B from the camera
audio.alaw   mono 8 kHz G.711 A-law
timing.json  rolling timestamps, offsets, FPS, and progress
summary.json final capture statistics
```

The bridge waits only long enough to establish source timing before starting FFmpeg. The timing probe uses eight video frames, and playback is normally exposed once about two seconds of HLS media exist. A native archive session may wait up to 30 seconds for its first video. If the session opens but produces no first video, the bridge can make one completely fresh startup retry.

## Browser playback

Browsers do not directly play a growing raw H.264 file alongside raw G.711 audio. FFmpeg converts the capture into a growing fragmented-MP4 HLS event stream:

```text
raw H.264 + G.711 A-law
→ apply measured A/V timing
→ H.264 + AAC
→ init.mp4 + .m4s fragments + index.m3u8
```

G.711 A-law is converted to AAC, 48 kHz mono. The measured initial audio offset is applied before muxing, and asynchronous resampling handles small clock differences.

The event playlist retains the generated fragments for the lifetime of the playback session. Already received footage therefore remains seekable while later fragments continue to arrive.

Home Assistant proxies the playlist, initialization file, and media fragments through authenticated local routes. The panel uses hls.js to reload the growing playlist and request new fragments.

## Playback qualities

### Original

Interactive Original playback keeps the source resolution and encodes browser-ready H.264 with regular keyframes for steady fragment publication.

### Balanced (720p)

Balanced resizes to 1280×720 and encodes H.264 using the selected hardware pipeline when available, with software H.264 as the fallback.

### Data Saver (480p)

Data Saver resizes to 854×480 and uses the same hardware-probe and software-fallback logic.

GPU selection and probing are described in [GPU pipeline](GPU.md).

## Buffering and playback rate

Archive data may arrive above or below real time. The player estimates incoming archive supply from buffered-media growth and progressively reduces playback speed as the buffer becomes thin, down to a 0.45x floor.

If playback reaches the growing edge, the browser pauses on the last decoded frame while native capture, FFmpeg, hls.js, and fragment downloads continue in the background. Playback resumes automatically after new forward media has been buffered beyond the held position.

Previously generated footage remains seekable. When camera-side capture is complete, the playlist is finalized and the requested interval becomes fully seekable.

## MP4 exports

Exports use the same native capture files but produce a normal downloadable MP4.

- **Original** copies the stored H.264 video without re-encoding and converts G.711 A-law to AAC when audio is present.
- **Balanced (720p)** resizes and encodes through the selected hardware or software pipeline.
- **Data Saver (480p)** does the same at 854×480.

The completed file uses fast-start MP4 metadata and is probed before the job is marked ready.

## Progress

Capture progress is based on the latest received media timestamp relative to the requested start and end times.

Processing progress comes from FFmpeg's output-time reporting. The panel combines capture and processing into the export progress shown to the user.

## Cleanup and isolation

Each playback or export receives its own working directory and process state. Completion, cancellation, timeout, and failure paths terminate FFmpeg, close the native socket, and release the per-camera session slot.

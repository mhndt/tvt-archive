# Media pipeline

The capture helper writes what the camera sends:

```text
video.h264   H.264 Annex B
audio.alaw   mono 8 kHz G.711 A-law
timing.json  rolling timestamps, frame rate, keyframe interval, progress
summary.json final statistics
```

The bridge waits for the first video frame, measures the frame rate and the audio offset from a few frames, then starts ffmpeg.

## Playback

ffmpeg turns the growing capture into an fMP4 HLS event playlist with one-second segments. Audio is converted to AAC and shifted by the measured offset. Home Assistant proxies the playlist and segments through signed URLs; the panel plays them with hls.js, or natively on iOS.

Video handling depends on quality and on the camera's keyframe interval, which the bridge learns from each capture and stores per camera:

| Quality | Keyframe interval ≤ 2 s | Longer |
|---|---|---|
| Original | stream copy | re-encode at source resolution |
| Low | re-encode to 854×480 | re-encode to 854×480 |

Re-encoding uses x264, or VAAPI when `encoder` is `vaapi`. Setting the camera's I-frame interval to 2 s or less makes Original playback free on any host.

The camera can send recordings slower than real time. The panel slows playback down to 0.45× as its buffer thins and pauses on the last frame when it runs out, then resumes once new media has arrived.

## Exports

Exports copy the H.264 stream into a fast-start MP4 and convert audio to AAC. The file is probed before the job is marked ready and kept for `cache_hours`.

## Cleanup

Each playback session and export has its own working directory. Ending, cancelling, timing out, or failing terminates ffmpeg and the capture helper and releases the per-camera session slot.

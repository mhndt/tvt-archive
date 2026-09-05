# Media pipeline

## Playback

The bridge opens the camera archive at the selected time and forwards what the camera sends, frame by frame, over one long HTTP response:

```text
"TF" kind flags pts_us length payload      little endian; kind 0 info, 1 video, 2 audio, 3 seek marker, 4 end
```

Video is the camera's H.264 as recorded, audio its mono 8 kHz G.711 A-law. The panel decodes video with WebCodecs and paints it on a canvas, decodes the audio itself, and reports how many frames it has shown. The bridge requests the next bag of 100 frames from the camera only while the viewer is fewer than 150 frames behind, so pausing stops the camera and the camera never runs far ahead of the picture.

Frames are shown by the rule TVT's own players use: when the wall clock has advanced as far as the frame's timestamp, or at once if the frame arrived late. Playback never changes speed and never drops frames. When the camera delivers slower than real time the picture advances at the camera's pace and the panel says so; when nothing arrives the last frame stays.

Seeking sends a new start on the same camera connection; the bridge switches to the new stream at its first frame and the panel resets its decoder on the marker. Home Assistant proxies the frame stream through a signed URL and the control calls through its normal API.

| Quality | Video |
|---|---|
| Original | untouched, no ffmpeg |
| Low | re-encoded to 854×480 per frame by ffmpeg (x264, or VAAPI when `encoder` is `vaapi`) |

Browsers without WebCodecs (iOS before 16.4, Firefox before 130) fall back to the previous path: the capture helper writes the stream to files, ffmpeg turns them into an fMP4 HLS event playlist, and the panel plays it with hls.js. That path re-encodes Original only when the camera's keyframe interval is over 2 s.

## Exports

Exports copy the H.264 stream into a fast-start MP4 and convert audio to AAC. The file is probed before the job is marked ready and kept for `cache_hours`. The camera delivers exports at its own playback rate, so an export takes at least as long as the recording it contains.

## Cleanup

Each playback session and export has its own working directory or camera connection. Ending, cancelling, timing out, or failing closes the connection, terminates ffmpeg where used, and releases the per-camera session slot. One session per camera runs at a time; a second request waits.

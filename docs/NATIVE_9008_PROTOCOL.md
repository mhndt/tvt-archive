This document describes the read-only subset implemented by TVT Archive. Integer fields are little-endian unless stated otherwise.

## Session layout

NVMS archive access uses one TCP connection to camera port `9008`. Authentication, metadata requests, recorded video, recorded audio, heartbeats, and continuation messages all share that connection.

## Server greeting

After connection, the camera sends a 64-byte greeting beginning with ASCII `head`.

A four-byte per-connection key is stored at offsets 28–31. The login request uses this key for the username and password transformation.

## Outer framing

A normal protocol object uses:

```text
4 bytes  ASCII "1111"
4 bytes  payload length
N bytes  inner object
```

A zero payload length is a heartbeat.

A payload length of `0xFFFFFFFF` introduces a fragmented object. The following six 32-bit fields describe the fragment:

```text
object ID
chunk count
total object length
chunk index
chunk length
reserved
```

The client groups chunks by object ID and reconstructs the complete inner object before decoding it.

## Inner message header

Each reconstructed object begins with:

```text
uint32 kind
uint32 request or session ID
uint32 target
uint32 body length
body bytes
```

The normal camera target observed for the implemented operations is `3`.

## Login

The login request kind is:

```text
0x00000101
```

The corresponding successful response kind is:

```text
0x01000101
```

The request body is 116 bytes and includes fixed-width username and password fields. Each field is transformed with a repeating XOR using the four key bytes from the server greeting.

The bridge sends heartbeat frames periodically while the session remains open.

## Recording metadata

Metadata requests use kind:

```text
0x00000411
```

Metadata responses use:

```text
0x0100040F
```

The response body may contain an HTTP-like prefix before the XML document.

### Recording dates

The command:

```text
SearchRecordDate
```

returns an XML list of dates containing recordings. The native client extracts, deduplicates, and sorts the returned `YYYY-MM-DD` values.

### Recording ranges

The command:

```text
SearchByTime
```

accepts local `starttime` and `endtime` values plus the recording types to include. Its XML response contains recording items with:

- a local start timestamp;
- a duration in seconds;
- the camera's recording-type value.

The bridge turns each item into a start and end time, clips it to the requested window, sorts the ranges, and merges overlapping or adjacent entries.

Those merged ranges are used for:

- the days shown as having recordings;
- the green availability bars in the selected timeline;
- the earliest and latest available footage;
- validating a requested playback or export interval.

Metadata results are cached briefly so ordinary panel refreshes do not repeatedly query the camera.

## Starting recorded playback

Playback begins with request kind:

```text
0x0000090B
```

The successful acknowledgement is:

```text
0x01000909
```

The request body is 32 bytes:

```text
+0x00  uint32  0x00145000
+0x04  byte    1
+0x05  byte    0
+0x06  byte    0
+0x07  byte    0
+0x08  uint32  1
+0x0C  uint64  start Unix epoch
+0x14  uint64  end Unix epoch
+0x1C  uint32  0
```

After acknowledgement, the camera returns recorded-media messages using kind:

```text
0x01000C05
```

Video and audio objects are interleaved on the same connection.

## Recorded video

The video payload is H.264 Annex B. NAL units use start codes such as:

```text
00 00 00 01
```

The client preserves:

- the encoded H.264 bytes;
- media timestamps;
- frame count;
- keyframe markers;
- byte count.

Large media messages may arrive through the fragmented outer-object format and are reassembled before the H.264 payload is extracted.

## Recorded audio

The audio payload is mono 8 kHz G.711 A-law. On the tested TVT TD-C12 it is normally delivered in 320-byte packets, approximately one packet every 40 ms.

The native media header is removed before the A-law samples are written to the capture output. The client records the audio timestamps, packet count, and byte count so the original offset relative to video can be preserved.

## Timing and capture state

The native reader tracks the first and latest video and audio timestamps, keyframes, frame counts, byte counts, and a rolling source-frame-rate estimate.

This state is used to:

- estimate source FPS;
- calculate the initial audio/video offset;
- determine how much of the requested interval has arrived;
- report capture progress from media time rather than wall-clock time;
- stop close to the requested end time.

A native capture produces:

```text
video.h264   raw H.264 Annex B
audio.alaw   raw 8 kHz mono G.711 A-law
timing.json  rolling timing and progress state
summary.json final capture statistics
```

The next conversion stage is described in [Media pipeline](MEDIA_PIPELINE.md).

## Playback continuation

The camera may pause archive delivery unless the client sends continuation messages.

The continuation command used by the bridge is:

```text
0x0000090A
```

Controlled tests on the TVT TD-C12 found that sending it every 25 received video frames provided continuous delivery. The implementation therefore:

1. sends `0x0000090B` once to start playback;
2. sends `0x0000090A` after every 25 received video frames;
3. does not send `0x00000907` during ordinary playback or export;
4. ends playback by closing the TCP session.

The cadence may need device-specific adjustment if other firmware behaves differently.

## Scope

The implementation searches recording metadata and reads historical media. It does not change camera configuration or delete recordings.

The protocol has been validated against a TVT TD-C12. Other models and firmware may differ.

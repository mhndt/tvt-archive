# Protocol notes

Notes on the archive protocol observed on a TVT TD-C12. Archive sessions use TCP port 9008. Multi-byte integers are little-endian unless noted.

## Framing

The camera begins each connection with a 64-byte greeting. The first four bytes are ASCII `head`; bytes 28-31 contain the key used for login.

Application objects use:

```text
4 bytes  ASCII "1111"
4 bytes  payload length
N bytes  payload
```

A zero payload length is a heartbeat.

A payload length of `0xFFFFFFFF` marks a fragmented object. The outer header is followed by six 32-bit fields:

```text
object ID
chunk count
total object length
chunk index
chunk length
reserved
```

Chunk indexes are 1-based. The reserved field is zero on the TD-C12. Chunks are grouped by object ID and reassembled in index order.

## Message header

Each application payload begins with:

```text
uint32 kind
uint32 request ID
uint32 target
uint32 body length
body
```

Archive requests use target `3`.

| Operation | Request | Response |
|---|---:|---:|
| Login | `0x00000101` | `0x01000101` |
| Recording metadata | `0x00000411` | `0x0100040F` |
| Start playback | `0x0000090B` | `0x01000909` |
| Continue playback | `0x0000090A` | — |
| Recorded media | — | `0x01000C05` |

## Login

The login request uses request ID `0x00000105` and a 116-byte body.

```text
+0x04  username and terminating NUL, XOR transformed
+0x24  32-byte password field, zero-padded then XOR transformed
+0x68  6-byte client tag
+0x70  uint32 3
```

The username and password are XORed with the four-byte key from the greeting, repeated across the field.

After login, a zero-length outer frame is sent every four seconds as a heartbeat.

## Recording metadata

Metadata requests use request ID `0x0000FFFF`.

The body begins with a 64-byte NUL-padded ASCII command name.

### Recording dates

`SearchRecordDate` returns dates containing recordings. Dates are represented as `YYYY-MM-DD`.

### Recording ranges

`SearchByTime` appends an XML request containing `starttime`, `endtime`, and the recording types to include:

```text
manual
nic broken
schedule
motion
sensor
intel detection
```

Returned recording items contain:

```text
text       local start time, YYYY-MM-DD HH:MM:SS
seconds    duration
recType    recording type
```

Responses may contain an HTTP-like prefix before the XML document.

## Playback

Playback starts with `0x0000090B`. The request body is 32 bytes:

```text
+0x00  uint32  0x00145000
+0x04  byte    1
+0x05  byte    0
+0x06  byte    0
+0x07  byte    0
+0x08  uint32  1
+0x0C  uint64  start epoch
+0x14  uint64  end epoch
+0x1C  uint32  0
```

The start and end epochs are derived from camera-local timestamps.

A successful request is acknowledged with `0x01000909`. Recorded media then arrives as `0x01000C05` using the playback request ID.

## Recorded media

Media messages have a 40-byte header followed by the declared payload.

```text
+0x00  4-byte marker
+0x08  uint32 width
+0x0C  uint32 height
+0x10  uint64 timestamp, microseconds
+0x20  uint32 payload length
+0x28  payload
```

### Video

Video is H.264 Annex B. Payloads begin with a three- or four-byte Annex B start code.

Marker `00 00 00 01` denotes a keyframe.

### Audio

Audio uses marker:

```text
01 22 00 01
```

The payload begins with a four-byte native prefix followed by mono 8 kHz G.711 A-law samples.

The TD-C12 normally returns 320 bytes of A-law audio per media message, about 40 ms.

## Flow control

Recorded media is delivered in bags of 100 video frames with interleaved audio.

A bag-end message has its first two body bytes set to zero and body byte 3 set to `2`. The next bag is requested with `0x0000090A`, using the active playback request ID and an empty body.

A message with its first two body bytes set to zero and body byte 3 set to `3` marks the end of the recording.

## Seeking

Seeking uses another `0x0000090B` request on the existing connection with a new request ID and start time.


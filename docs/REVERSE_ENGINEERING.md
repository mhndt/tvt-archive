## Goal

The camera already records continuously to its built-in SD card, while normal Home Assistant camera integrations expose only live video. The research goal was to find a practical way to:

- discover which days contain recordings;
- obtain the recorded time ranges within a selected day;
- request a historical start and end time;
- recover the stored video and audio;
- play the result in a browser and export it as MP4.

The investigation moved through recorded RTSP, a vendor Linux SDK prototype, and packet analysis of NVMS before the final direct implementation was built.

## Recorded RTSP

The first working archive path used the camera's recorded-playback RTSP URL:

```text
rtsp://USER:PASS@CAMERA:554/chID=0&date=YYYY-MM-DD&time=HH:MM:SS&timelen=SECONDS&streamType=main&action=playback
```

This retrieved historical H.264 video from the SD card. On the tested TVT TD-C12 it did not provide usable recorded audio, so RTSP was kept as an optional video fallback rather than the main archive path.

## Vendor Linux SDK prototype

A vendor Linux SDK was then used as a research tool. Its playback callback proved that the same SD-card recordings contained:

- H.264 video;
- mono 8 kHz G.711 A-law audio;
- media timestamps;
- searchable recording ranges.

That established that the missing RTSP audio was a limitation of the tested RTSP archive route rather than the recording itself. The SDK prototype was not retained in the public runtime because it depended on a platform-specific vendor library.

## Packet analysis

NVMS archive sessions were captured with Wireshark while performing individual operations such as logging in, opening the calendar, searching a time range, starting playback, and downloading a clip.

Small Python canaries then reproduced one operation at a time. This made it possible to separate the archive protocol from unrelated application traffic and confirm which messages were actually required.

The analysis showed that NVMS used one TCP connection to port `9008` for:

- authentication;
- recording-date and time-range queries;
- historical video and audio delivery;
- heartbeats;
- playback continuation.

The same media path was used for both playback and download. There was no separate RTSP stream or bulk-download socket involved in the tested workflow.

## Building the direct implementation

The captured operations were reconstructed in stages:

1. establish a TCP/9008 session and authenticate;
2. query recording metadata;
3. request a selected historical interval;
4. reconstruct fragmented protocol objects;
5. separate the interleaved video, audio, and timestamps;
6. keep long archive reads moving with the observed continuation command;
7. pass the recovered media into the browser-playback and export pipeline.

The exact framing, message kinds, metadata commands, playback request, media formats, and continuation behavior are documented in [Native TCP/9008 archive protocol](NATIVE_9008_PROTOCOL.md).

The conversion from raw camera media into HLS playback and MP4 exports is documented separately in [Media pipeline](MEDIA_PIPELINE.md).

## Final approach

Native TCP/9008 became the default because it provides the complete SD-card archive path used by the project: metadata, stored video, stored audio, and timestamps. Recorded RTSP remains available as a fallback, while neither NVMS nor the vendor SDK is required at runtime.

The implemented subset is read-only. It searches archive metadata and retrieves historical media; it does not change camera settings or delete recordings.

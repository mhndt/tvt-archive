<p align="center">
  <img src="https://raw.githubusercontent.com/mhndt/tvt-archive/main/assets/TVTArchiveLogo.png" alt="" width="160">
</p>

# TVT Archive

[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Release](https://img.shields.io/github/v/release/mhndt/tvt-archive)](https://github.com/mhndt/tvt-archive/releases)
[![License](https://img.shields.io/github/license/mhndt/tvt-archive)](LICENSE)

TVT Archive lets you browse, play, and export the recordings on a TVT camera's SD card from Home Assistant. It adds a Recordings panel to the sidebar and, for each camera, a sensor that shows whether it is recording and sensors for how much footage is on the card.

A small bridge container on your network reads the recordings straight from the camera. Nothing leaves your LAN and no vendor account is needed.

> [!IMPORTANT]
> Tested with the TVT TD-C12. Other TVT and OEM cameras may work but are untested.

![Recordings panel](docs/images/panel.png)

# Installation

## Prerequisites

- The bridge running on a machine on the camera's network. See below.
- The camera's local address and an account that is allowed to play back recordings.

### Bridge

The bridge needs Docker with Compose v2 on an x86-64 or arm64 host.

```bash
git clone https://github.com/mhndt/tvt-archive.git
cd tvt-archive
./setup.sh
```

The script writes `.env`, starts the container, and prints the bridge URL and access token used in the next step. Add `--vaapi` on an Intel or AMD host to encode on the GPU; see [Bridge settings](#bridge-settings).

Print the token again:

```bash
docker exec tvt-archive tvt-archive show-token
```

Update the bridge:

```bash
git pull && docker compose pull && docker compose up -d
```

<details>
<summary>Manual Compose setup</summary>

```bash
cp .env.example .env
docker compose up -d
```

For VAAPI set `COMPOSE_FILE=compose/compose.yaml:compose/vaapi.yaml`, `TVT_ARCHIVE_ENCODER=vaapi`, and the group IDs from `stat -c %g /dev/dri/renderD128 /dev/dri/card0`.

</details>

## Install

[![Open TVT Archive in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mhndt&repository=tvt-archive&category=integration)

1. In HACS, add `https://github.com/mhndt/tvt-archive` as a custom repository of type Integration.
2. Install TVT Archive.
3. Restart Home Assistant.

To install manually, copy `custom_components/tvt_archive` into your `config/custom_components` directory and restart.

## Configure

Go to Settings > Devices & services > Add integration and search for TVT Archive.

| Field | Description |
|---|---|
| Bridge URL | `http://<bridge-host>:8099`, as printed by the setup script |
| Access token | The token printed by the setup script |

You are then asked for the first camera:

| Field | Description |
|---|---|
| Camera name | Shown in the panel and used for entity names |
| Address | The camera's local IP address or hostname |
| Port | Normally 9008 |
| Username, Password | A camera account with playback permission |
| Recording audio | Auto, Always expect audio, or Disabled |

Cameras are stored by the bridge, not in Home Assistant. Add, edit, or remove them later under Settings > Devices & services > TVT Archive > Configure.

# Usage

## Entities

One device is created per camera with these entities:

| Entity | Description |
|---|---|
| `binary_sensor.<camera>_recording` | On while the camera is currently recording |
| `sensor.<camera>_recorded_today` | Hours recorded today |
| `sensor.<camera>_available_history` | Hours between the oldest and latest recording |
| `sensor.<camera>_oldest_recording` | Start of the oldest recording on the card |
| `sensor.<camera>_latest_recording` | End of the latest recording |

Sensors update every two minutes.

![Camera entities](docs/images/entities.png)

## Recordings panel

Open Recordings in the sidebar. Choose a camera and a day. Green sections of the timeline are recordings. Click one and press Play from here, or set a start and end time and press Download original.

Quality:

- Original: the camera's own H.264 stream. Copied as is when the camera's keyframe interval is 2 s or less, re-encoded otherwise.
- Low (480p): re-encoded for slow connections.

Exports always contain the original video with audio converted to AAC.

# Bridge settings

Settings live in `config.json` in the `tvt-archive-config` volume. Cameras are managed from Home Assistant; the optional `processing` keys are:

| Key | Default | Description |
|---|---|---|
| `encoder` | `software` | `software` (x264) or `vaapi` (Intel or AMD through `/dev/dri`) |
| `dri_device` | `/dev/dri/renderD128` | VAAPI render node |
| `playback_max_seconds` | `900` | Longest playback session |
| `download_max_seconds` | `3600` | Longest export |
| `cache_hours` | `6` | How long exports are kept |
| `availability_days` | `45` | How far back the calendar looks |
| `max_parallel_jobs` | `1` | Concurrent exports |
| `max_parallel_playback_sessions` | `2` | Concurrent playback sessions |
| `stream_audio_delay_ms` | `0` | Extra audio delay |

`TVT_ARCHIVE_ENCODER`, `TVT_ARCHIVE_DRI_DEVICE`, and `TVT_ARCHIVE_STREAM_AUDIO_DELAY_MS` in the environment override the file. `TVT_ARCHIVE_TOKEN` is used only when the config is first created.

The bridge encodes only for Low quality and for Original playback from cameras with a long keyframe interval. Setting the camera's I-frame interval to 2 s or less (25 to 50 frames at 25 fps) lets Original play without any CPU cost.

# Limitations

- The bridge speaks plain HTTP with a bearer token and is meant for a trusted network. Keep ports 8099 and 9008 off the Internet.
- Cameras on weak Wi-Fi can send recordings slower than real time. The player slows down and pauses when that happens.
- Software encoding of Low quality keeps up on x86 hosts and on a Raspberry Pi 4 or newer.

# Troubleshooting

Could not connect to the bridge: check that the bridge URL is reachable from the Home Assistant host and that the token matches the output of `docker exec tvt-archive tvt-archive show-token`.

The camera could not be added: the bridge logs in to the camera before saving it. Check the address, that port 9008 is reachable from the bridge host, and that the account may play back recordings. Details are in `docker compose logs tvt-archive`.

Playback keeps pausing: the camera is delivering slower than real time, usually over Wi-Fi. Try Low quality. If the panel's Video field says "camera keyframes every N s", lower the I-frame interval in the camera's encode settings.

# Removing

Delete the integration under Settings > Devices & services. To remove the bridge and its data, including stored camera credentials and cached exports, run `docker compose down -v` in the checkout.

# Compatibility

Only the TVT TD-C12 has been tested. If it works on another TVT camera, recorder, or OEM device, open a compatibility report so it can be listed.

[docs/protocol.md](docs/protocol.md) contains the protocol and reverse-engineering notes, and [docs/media-pipeline.md](docs/media-pipeline.md) describes how recordings are turned into playback and exports.

# Development

```bash
bash tests/run-tests.sh
ruff check . && ruff format --check .
./setup.sh --build-local
```

The Home Assistant tests need the test harness:

```bash
pip install pytest-homeassistant-custom-component home-assistant-frontend
pytest
```

# Contributing

Issues and pull requests are welcome. Reports from other TVT models are especially useful.

# License

MIT. Third-party components are listed in [THIRD_PARTY.md](THIRD_PARTY.md). This is an unofficial project; product names belong to their owners.

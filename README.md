# TVT Archive

[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration) [![Release](https://img.shields.io/github/v/release/mhndt/tvt-archive)](https://github.com/mhndt/tvt-archive/releases) [![CI](https://github.com/mhndt/tvt-archive/actions/workflows/ci.yml/badge.svg)](https://github.com/mhndt/tvt-archive/actions/workflows/ci.yml)

Browse, play, and export recordings from a TVT camera's SD card in Home Assistant. The integration adds a Recordings panel to the sidebar with a day timeline, motion highlighting, and keyboard shortcuts, lists recordings in the Media browser, and adds entities for recording status, available footage, and the SD card.

Recordings are read directly from the camera over your LAN. Nothing leaves your network and no vendor account is needed.

# Installation

[![Open TVT Archive in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mhndt&repository=tvt-archive&category=integration)

1. Add `https://github.com/mhndt/tvt-archive` to HACS as a custom Integration repository.
2. Install TVT Archive and restart Home Assistant.
3. Open Settings > Devices & services > Add integration > TVT Archive and choose **Built-in**.
4. Enter your first camera.

For a manual install, copy `custom_components/tvt_archive` from the release ZIP into `config/custom_components` and restart Home Assistant.

The built-in bridge runs as a small background process inside Home Assistant and needs `ffmpeg`, which Home Assistant OS and Container already include. On Home Assistant Core, install `ffmpeg` on the host.

<details>
<summary>Optional app or container</summary>

The bridge can also run separately, as a Home Assistant app or a Docker container. Use this to encode Low quality on an Intel or AMD GPU with VAAPI, or to run the bridge on another machine.

**Home Assistant app**

[![Add repository to the app store](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fmhndt%2Ftvt-archive)

Install and start the app. The integration is discovered under Settings > Devices & services.

**Docker Compose**

On an x86-64 or arm64 Docker host on the camera's network:

```bash
git clone https://github.com/mhndt/tvt-archive.git
cd tvt-archive
./setup.sh
```

The script prints the bridge URL and access token. Use `./setup.sh --vaapi` to enable VAAPI. Print the token again with `docker exec tvt-archive tvt-archive show-token`, and update with `git pull && docker compose pull && docker compose up -d`.

Then add the integration and choose **Connect to the TVT Archive app**.

</details>

## Upgrading from 0.9

Existing setups that use the app keep working. To switch to the built-in bridge, delete the TVT Archive integration entry, add it again as Built-in, re-enter your cameras, and remove the app.

## Configuration

| Field | Description |
|---|---|
| Camera name | Name shown in Home Assistant |
| Address | Camera IP address or hostname |
| Port | Normally 9008 |
| Username, Password | Camera account with playback permission |
| Recording audio | Auto, Always expect audio, or Disabled |

Add, edit, or remove cameras and choose motion sensors under Settings > Devices & services > TVT Archive > Configure.

# Usage

## Recordings panel

Open Recordings in the sidebar, choose a camera and day, then select a time on the timeline. Green shows recorded footage and amber shows motion. Press Play from here to watch, or choose a start and end time to download.

Playback quality:

- Original: the camera's H.264 video.
- Low (480p): re-encoded for slower connections.

Downloads keep the original video and convert audio to AAC.

<details>
<summary>Keyboard shortcuts</summary>

| Key | Action |
|---|---|
| Space, K | Play or pause |
| J, L | Back or forward 10 seconds |
| ←, → | Back or forward 5 seconds |
| `,` `.` | Previous or next frame |
| ↑, ↓ | Volume |
| M | Mute |
| F | Full screen |

On touch screens, double-tap either side of the video to seek 10 seconds.

</details>

## Media browser

Recordings also appear under Media > TVT Archive, with thumbnails and a Motion folder for each day.

## Motion

Motion comes from the camera when it records motion separately, otherwise from Home Assistant motion sensors on the same camera address, such as ONVIF. Choose sensors under Configure > Motion sensors.

## Saving recordings

The Save recording action saves an MP4 to `media/tvt_archive`, for example from an automation when motion is detected.

## Entities

Each camera is added as a device with these entities:

| Entity | Description |
|---|---|
| `binary_sensor.<camera>_recording` | Whether the camera is currently recording |
| `sensor.<camera>_recorded_today` | Hours recorded today |
| `sensor.<camera>_available_history` | Hours between the oldest and latest recording |
| `sensor.<camera>_oldest_recording` | Start of the oldest recording on the card |
| `sensor.<camera>_latest_recording` | End of the latest recording |
| `sensor.<camera>_sd_card` | Normal, No SD card, Unformatted, Formatting, or Error, with total and used GB |

Sensors update every two minutes. Problems such as a missing SD card or a rejected camera login appear under Settings > Repairs.

<details>
<summary>Bridge settings</summary>

Settings are stored in `config.json`: in `/config/.storage/tvt_archive` for the built-in bridge, in the app data directory for the app, or in the tvt-archive-config volume for Docker Compose.

| Key | Default | Description |
|---|---|---|
| `encoder` | software | Software or VAAPI encoding |
| `dri_device` | `/dev/dri/renderD128` | VAAPI render device |
| `playback_max_seconds` | 900 | Maximum playback session length |
| `download_max_seconds` | 3600 | Maximum download length |
| `cache_hours` | 6 | How long downloads are kept |
| `availability_days` | 45 | How far back the calendar looks |
| `max_parallel_jobs` | 1 | Concurrent downloads |
| `max_parallel_playback_sessions` | 2 | Concurrent playback sessions |
| `max_native_sessions_per_camera` | 1 | Camera connections per camera |
| `stream_seek_timeout_seconds` | 8 | Seek wait before reconnecting |
| `stream_audio_delay_ms` | 0 | Extra audio delay |

`TVT_ARCHIVE_ENCODER`, `TVT_ARCHIVE_DRI_DEVICE`, and `TVT_ARCHIVE_STREAM_AUDIO_DELAY_MS` override the matching settings when set in the environment.

</details>

# Compatibility

Tested with the TVT TD-C12. Other TVT cameras, recorders, and OEM devices may also work. If you test another model, please open an issue so it can be added here.

# Development

Protocol and reverse-engineering notes are in [PROTOCOL.md](PROTOCOL.md).

```bash
bash tests/run-tests.sh
pytest -q
ruff check . && ruff format --check .
./setup.sh --build-local
```

# Contributing

Issues and pull requests are welcome. Reports from other TVT models are especially useful.

# License

MIT. See [LICENSE](LICENSE).

TVT is a trademark of its respective owner. This project is independent and is not affiliated with or endorsed by TVT.

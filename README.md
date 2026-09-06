# TVT Archive

[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration) [![Release](https://img.shields.io/github/v/release/mhndt/tvt-archive)](https://github.com/mhndt/tvt-archive/releases) [![CI](https://github.com/mhndt/tvt-archive/actions/workflows/ci.yml/badge.svg)](https://github.com/mhndt/tvt-archive/actions/workflows/ci.yml)

Browse, play, and export recordings from a TVT camera's SD card in Home Assistant. The integration adds a Recordings panel to the sidebar and creates entities for recording status and available footage.

A small bridge on your network reads recordings directly from the camera. Nothing leaves your LAN and no vendor account is needed.

# Installation

## Home Assistant app

On Home Assistant OS or Supervised, add this repository under Settings > Apps and install the app:

[![Add repository to the app store](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fmhndt%2Ftvt-archive)

Start the app, then install the integration through HACS. Once installed, the integration should automatically appear under Settings > Devices & services.

<details>
<summary>Docker Compose</summary>

For Home Assistant Container or Core, run the bridge on an x86-64 or arm64 Docker host on the camera's network:

```bash
git clone https://github.com/mhndt/tvt-archive.git
cd tvt-archive
./setup.sh
```

The setup script starts the bridge and prints the URL and access token used when configuring the integration. Use `./setup.sh --vaapi` to enable VAAPI on an Intel or AMD host.

To print the token again:

```bash
docker exec tvt-archive tvt-archive show-token
```

To update:

```bash
git pull && docker compose pull && docker compose up -d
```

</details>

## Integration

[![Open TVT Archive in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mhndt&repository=tvt-archive&category=integration)

1. Add `https://github.com/mhndt/tvt-archive` to HACS as a custom Integration repository.
2. Install the integration.
3. Restart Home Assistant.

For a manual install, copy `custom_components/tvt_archive` into `config/custom_components` and restart Home Assistant.

## Configuration

With the Home Assistant app, open Settings > Devices & services and configure the discovered integration.

With Docker Compose, add the integration and enter the bridge URL and access token printed by `setup.sh`.

Configure your first camera with:

| Field | Description |
|---|---|
| Camera name | Name shown in Home Assistant |
| Address | Camera IP address or hostname |
| Port | Normally 9008 |
| Username, Password | Camera account with playback permission |
| Recording audio | Auto, Always expect audio, or Disabled |

Add, edit, or remove cameras later under Settings > Devices & services > TVT Archive > Configure.

# Usage

## Recordings panel

Open Recordings in the sidebar, choose a camera and day, then select a recording on the timeline. Use Play from here to watch it or choose a start and end time to download it.

Playback quality:

- Original: the camera's H.264 video.
- Low (480p): re-encoded by the bridge for slower connections.

Downloads keep the original video and convert audio to AAC.

## Entities

Each camera is added as a device with these entities:

| Entity | Description |
|---|---|
| `binary_sensor.<camera>_recording` | Whether the camera is currently recording |
| `sensor.<camera>_recorded_today` | Hours recorded today |
| `sensor.<camera>_available_history` | Hours between the oldest and latest recording |
| `sensor.<camera>_oldest_recording` | Start of the oldest recording on the card |
| `sensor.<camera>_latest_recording` | End of the latest recording |

Sensors update every two minutes.

<details>
<summary>Bridge settings</summary>

Bridge settings are stored in `config.json` in the app data directory, or in the tvt-archive-config volume when using Docker Compose.

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
| `stream_audio_delay_ms` | 0 | Extra audio delay |

`TVT_ARCHIVE_ENCODER`, `TVT_ARCHIVE_DRI_DEVICE`, and `TVT_ARCHIVE_STREAM_AUDIO_DELAY_MS` override the matching settings when set in the environment.

</details>

# Compatibility

Tested with the TVT TD-C12. Other TVT cameras, recorders, and OEM devices may also work. If you test another model, please open an issue so it can be added here.

# Development

Protocol and reverse-engineering notes are in [PROTOCOL.md](PROTOCOL.md).

```bash
bash tests/run-tests.sh
ruff check . && ruff format --check .
./setup.sh --build-local
```

# Contributing

Issues and pull requests are welcome. Reports from other TVT models are especially useful.

# License

MIT. See [LICENSE](LICENSE).

TVT is a trademark of its respective owner. This project is independent and is not affiliated with or endorsed by TVT.

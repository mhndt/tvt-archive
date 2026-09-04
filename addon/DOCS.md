# TVT Archive

Runs the TVT Archive bridge, which reads recordings from a TVT camera's SD card for the TVT Archive integration.

## Setup

1. Start the add-on.
2. Install the TVT Archive integration from HACS and restart Home Assistant.
3. Open Settings > Devices & services. The bridge appears as discovered; press Configure and add your first camera.

The camera must be reachable from the Home Assistant host. Cameras, credentials, and cached exports are stored in the add-on's data directory.

## Options

| Option | Default | Description |
|---|---|---|
| `encoder` | `software` | `software` uses x264. `vaapi` encodes on an Intel or AMD GPU through `/dev/dri`. |

Encoding is only used for Low quality and for Original playback from cameras whose keyframe interval is longer than 2 s. Setting the camera's I-frame interval to 2 s or less lets Original play without encoding.

## Docker Compose instead

On Home Assistant Container or Core there are no add-ons. Run the same image with Docker Compose as described in the [README](https://github.com/mhndt/tvt-archive#readme).

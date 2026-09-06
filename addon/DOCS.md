# TVT Archive

Runs the bridge that gives Home Assistant access to recordings stored on compatible TVT cameras.

## Setup

1. Start the app.
2. Install TVT Archive from HACS and restart Home Assistant.
3. Open Settings > Devices & services and configure the discovered integration.

The camera must be reachable from the Home Assistant host.

## Configuration

| Option | Default | Description |
|---|---|---|
| `encoder` | software | Software or VAAPI encoding for Intel and AMD GPUs |

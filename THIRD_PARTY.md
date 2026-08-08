# Third-party components

TVT Archive is licensed under the MIT License. The container and integration also use or redistribute the components below under their respective licenses.

## FFmpeg, FFprobe, libva, and Debian packages

The runtime image installs FFmpeg, FFprobe, libva tools, and general VAAPI drivers from Debian 13 (Trixie). These packages and any enabled codec components remain subject to their own upstream and Debian licensing terms. TVT Archive invokes the packaged executables and libraries; it does not copy their source into this repository.

On `linux/amd64`, the image also installs Debian's `intel-media-va-driver-non-free` package for Intel iHD support. The verified Intel HD Graphics 530 environment reports FFmpeg 7.1.5, libva 2.22.0, and Intel media-driver 25.2.3.

Exact package versions for a published image are available through container diagnostics and the image SBOM.

NVIDIA driver libraries are supplied by the host through NVIDIA Container Toolkit when the NVIDIA Compose override is used.

## hls.js 1.6.16

The image includes hls.js `1.6.16`, distributed under the Apache License 2.0. The Docker build downloads the versioned npm tarball, verifies its pinned SHA-512 digest, extracts `dist/hls.min.js`, and retains the upstream license as:

```text
/opt/tvt-archive/static/HLSJS-LICENSE.txt
```

No hls.js asset is fetched from a CDN at runtime.

## Home Assistant

The custom integration uses Home Assistant's public integration, config-flow, entity, HTTP, panel, selector, and frontend interfaces. No Home Assistant source code is copied into this repository.

## Names and trademarks

TVT Archive is an unofficial community project. Product names and trademarks belong to their respective owners.

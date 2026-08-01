# Third-party components

TVT Archive is licensed under the MIT License. The container and integration also use or redistribute the components below under their respective licenses.

## FFmpeg, FFprobe, libva, and Debian packages

The runtime image installs FFmpeg, FFprobe, libva tools, and general VAAPI drivers from Debian 13 (Trixie). These packages and any enabled codec components remain subject to their own upstream and Debian licensing terms. TVT Archive invokes the packaged executables and libraries; it does not copy their source into this repository.

The exact package versions in a published image are available through the container diagnostics and image SBOM.

## Intel gmmlib 22.7.1 and media-driver 25.1.2

On `linux/amd64`, the Docker build compiles Intel gmmlib `22.7.1` and Intel media-driver `25.1.2` from their versioned upstream source releases. Their upstream licenses are retained inside the image under:

```text
/usr/share/doc/tvt-archive/third-party/
```

Intel media-driver is distributed upstream under MIT and BSD-3-Clause terms. The selected full-feature build enables Intel's closed-source media-kernel binaries through `ENABLE_NONFREE_KERNELS=ON`. Those binaries are part of the upstream media-driver release and remain governed by the upstream notices and terms. Review the upstream licensing information before redistributing a modified image.

The pinned driver exists because that exact userspace stack passed the complete decode, scale, and encode pipeline on the verified Skylake host. Other architectures skip this source-build stage and use their platform packages.

The runtime does not assume Intel hardware. It probes complete VAAPI, QSV, NVIDIA, hybrid, and software pipelines before selecting one.

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


# GPU acceleration

TVT Archive is designed to run with Intel, AMD, NVIDIA, or no supported GPU. This means it provides generalized probes and a software fallback; it does not mean every GPU/driver combination is already verified.

## Selection order

Automatic mode tests complete pipelines and prefers:

1. full VAAPI decode, scale, and encode;
2. full Intel QSV;
3. full NVIDIA CUDA/NVENC;
4. hybrid VAAPI or NVIDIA hardware encode;
5. software x264.

A path is not selected merely because FFmpeg lists an encoder or a device node exists.

## Intel and AMD

Use `compose/intel-amd.yaml` to pass `/dev/dri` and the host render/video group IDs.

The x86-64 image includes a pinned Intel iHD 25.1.2 compatibility driver because that exact stack passed full VAAPI scaling and encoding on the verified Skylake HD 530 host. Debian's other VAAPI drivers remain available and are selected only when their probe succeeds.

Other Intel generations, QSV, and AMD VAAPI are experimental until compatibility reports are received.

## NVIDIA

Use `compose/nvidia.yaml` with NVIDIA Container Toolkit. The bridge checks for the CUDA/NVENC path and falls back when it is not usable.

NVIDIA support is currently experimental because the development environment did not include an NVIDIA Docker host.

## CPU fallback

Software x264 requires no GPU device mapping. It is the final fallback and can be selected explicitly:

```text
TVT_ARCHIVE_ACCELERATOR=software
```

CPU load depends on camera resolution, frame rate, number of concurrent sessions, and selected quality.

## Diagnostics

```bash
docker compose exec tvt-archive /opt/tvt-archive/entrypoint.sh accelerator-info
```

The report includes available candidates, selected path, driver information, FFmpeg information, and probe results without camera credentials.

## Common issues

### Permission denied on `/dev/dri`

Set the group IDs from the host in `.env` and recreate the container.

### VAAPI initializes but scaling fails

This is exactly why TVT Archive runs a complete pipeline probe. The bridge should fall back rather than use a partially functional path.

### NVIDIA is detected but software is selected

Check NVIDIA Container Toolkit, mounted driver libraries, `NVIDIA_DRIVER_CAPABILITIES`, and whether the packaged FFmpeg exposes `h264_nvenc`.

### Unsupported architecture

Published images target `linux/amd64` and `linux/arm64`. The Intel compatibility build is x86-64 only. Other platforms use packaged drivers and software fallback.

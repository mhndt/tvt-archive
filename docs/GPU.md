# GPU acceleration

TVT Archive can run with Intel, AMD, NVIDIA, or no supported GPU. Hardware acceleration is selected only when a complete decode, resize, and encode probe succeeds.

## Selection order

Automatic mode prefers working pipelines in this order:

1. full VAAPI decode, scale, and encode;
2. full Intel QSV;
3. full NVIDIA CUDA/NVENC;
4. hybrid VAAPI or NVIDIA hardware encode;
5. software x264.

A path is not selected merely because FFmpeg lists an encoder or a device node exists.

## Intel and AMD

Use `compose/intel-amd.yaml` to pass `/dev/dri` and the host render/video group IDs.

The container uses Debian Trixie's packaged FFmpeg, libva, and VAAPI drivers. On `linux/amd64`, `intel-media-va-driver-non-free` is also installed for Intel iHD support.

The verified Intel HD Graphics 530 setup reports:

```text
FFmpeg 7.1.5
libva 2.22.0
Intel iHD media-driver 25.2.3
```

Other Intel generations, Intel QSV, and AMD VAAPI depend on their driver and FFmpeg support and remain subject to the runtime pipeline probe.

## NVIDIA

Use `compose/nvidia.yaml` with NVIDIA Container Toolkit. The bridge checks the CUDA/NVENC path and falls back when it is not usable.

NVIDIA driver libraries are supplied by the host through NVIDIA Container Toolkit rather than bundled into the TVT Archive image.

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

Set the render/video group IDs from the host in `.env`, then recreate the container.

### VAAPI initializes but scaling or encoding fails

The complete pipeline probe rejects partially working paths and falls back to another usable path.

### NVIDIA is detected but software is selected

Check NVIDIA Container Toolkit, mounted driver libraries, `NVIDIA_DRIVER_CAPABILITIES`, and whether FFmpeg exposes `h264_nvenc`.

### Unsupported architecture

Published images target `linux/amd64` and `linux/arm64`. Hardware support depends on the drivers and encoders available for that architecture; software x264 remains the fallback.

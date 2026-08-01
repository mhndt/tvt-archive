# syntax=docker/dockerfile:1
ARG DEBIAN_VERSION=trixie-slim

# The distribution's Intel 25.2.x stack failed the complete VAAPI scaling
# probe on the verified Skylake host. The complete real-archive test passed
# with Intel iHD 25.1.2, so x86_64 images build that exact driver release
# rather than a nearby unverified patch version. The final
# runtime still retains Debian's drivers and selects only pipelines that pass
# a real decode -> resize -> encode probe. Non-x86 images skip this optional
# Intel build and continue with their platform's packaged drivers.
FROM debian:${DEBIAN_VERSION} AS intel-media-builder
ARG INTEL_GMMLIB_VERSION=22.7.1
ARG INTEL_MEDIA_DRIVER_VERSION=25.1.2
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates cmake curl libdrm-dev libpciaccess-dev \
      libva-dev ninja-build pkg-config xz-utils && \
    rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    mkdir -p /src /opt/intel /export/dri /export/lib; \
    if [ "$(uname -m)" = "x86_64" ]; then \
      multiarch="$(gcc -dumpmachine)"; \
      curl -fsSL "https://github.com/intel/gmmlib/archive/refs/tags/intel-gmmlib-${INTEL_GMMLIB_VERSION}.tar.gz" \
        | tar -xz -C /src; \
      cmake -S "/src/gmmlib-intel-gmmlib-${INTEL_GMMLIB_VERSION}" -B /src/gmmlib-build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/opt/intel \
        -DCMAKE_INSTALL_LIBDIR="lib/${multiarch}"; \
      cmake --build /src/gmmlib-build; \
      cmake --install /src/gmmlib-build; \
      curl -fsSL "https://github.com/intel/media-driver/archive/refs/tags/intel-media-${INTEL_MEDIA_DRIVER_VERSION}.tar.gz" \
        | tar -xz -C /src; \
      PKG_CONFIG_PATH="/opt/intel/lib/${multiarch}/pkgconfig" \
      cmake -S "/src/media-driver-intel-media-${INTEL_MEDIA_DRIVER_VERSION}" -B /src/media-build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_PREFIX_PATH=/opt/intel \
        -DCMAKE_INSTALL_PREFIX=/opt/intel \
        -DCMAKE_INSTALL_LIBDIR="lib/${multiarch}" \
        -DENABLE_KERNELS=ON \
        -DENABLE_NONFREE_KERNELS=ON \
        -DBUILD_KERNELS=OFF; \
      cmake --build /src/media-build; \
      cmake --install /src/media-build; \
    else \
      echo "Skipping optional Intel media-driver build on $(uname -m)"; \
    fi
# Keep export/validation separate so a path-packaging mistake does not discard
# the expensive compiled layer on the next Docker build.
RUN set -eux; \
    if [ "$(uname -m)" = "x86_64" ]; then \
      multiarch="$(gcc -dumpmachine)"; \
      driver_path="/opt/intel/lib/${multiarch}/dri/iHD_drv_video.so"; \
      if [ ! -s "$driver_path" ]; then driver_path="/usr/lib/${multiarch}/dri/iHD_drv_video.so"; fi; \
      test -s "$driver_path"; \
      cp -a "$driver_path" /export/dri/; \
      cp -a /opt/intel/lib/${multiarch}/libigdgmm.so* /export/lib/; \
      cp -a /opt/intel/lib/${multiarch}/libigfxcmrt.so* /export/lib/ 2>/dev/null || true; \
      mkdir -p /export/licenses; \
      gmmlib_license="$(find "/src/gmmlib-intel-gmmlib-${INTEL_GMMLIB_VERSION}" -maxdepth 2 -type f -iname 'LICENSE*' | head -n1)"; \
      media_license="$(find "/src/media-driver-intel-media-${INTEL_MEDIA_DRIVER_VERSION}" -maxdepth 2 -type f -iname 'LICENSE*' | head -n1)"; \
      test -n "$gmmlib_license" -a -s "$gmmlib_license"; \
      test -n "$media_license" -a -s "$media_license"; \
      install -m 0644 "$gmmlib_license" /export/licenses/INTEL-GMMLIB-LICENSE; \
      install -m 0644 "$media_license" /export/licenses/INTEL-MEDIA-DRIVER-LICENSE; \
    fi


FROM debian:${DEBIAN_VERSION} AS hlsjs-builder
ARG HLS_JS_VERSION=1.6.16
ARG HLS_JS_TARBALL_SHA512=552211a4b7d1c250007462f8c224ee731d927118a9a3479dd4505ab56932b7cdf68c2e0245e2acb606bac888585701aef4b3818ee5fdc3399131e41d049b7310
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /export /tmp/hlsjs && \
    curl -fsSL "https://registry.npmjs.org/hls.js/-/hls.js-${HLS_JS_VERSION}.tgz" -o /tmp/hlsjs.tgz && \
    echo "${HLS_JS_TARBALL_SHA512}  /tmp/hlsjs.tgz" | sha512sum -c - && \
    tar -xzf /tmp/hlsjs.tgz -C /tmp/hlsjs && \
    install -m 0644 /tmp/hlsjs/package/dist/hls.min.js /export/hls.min.js && \
    install -m 0644 /tmp/hlsjs/package/LICENSE /export/HLSJS-LICENSE.txt && \
    test "$(wc -c < /export/hls.min.js)" -gt 400000

FROM debian:${DEBIAN_VERSION}
ARG INTEL_MEDIA_DRIVER_VERSION=25.1.2
ARG APP_VERSION=0.8.1
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="TVT Archive" \
      org.opencontainers.image.description="Home Assistant archive playback and export bridge for compatible TVT-family cameras" \
      org.opencontainers.image.url="https://github.com/mhndt/tvt-archive" \
      org.opencontainers.image.source="https://github.com/mhndt/tvt-archive" \
      org.opencontainers.image.documentation="https://github.com/mhndt/tvt-archive" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TVT_ARCHIVE_BASE=/opt/tvt-archive \
    TVT_ARCHIVE_CONFIG_DIRECTORY=/config \
    STATE_DIRECTORY=/state \
    CACHE_DIRECTORY=/cache \
    TVT_ARCHIVE_INTEL_MEDIA_DRIVER_VERSION=${INTEL_MEDIA_DRIVER_VERSION} \
    TVT_ARCHIVE_HLS_JS_VERSION=1.6.16

# va-driver-all keeps the image usable on packaged Intel i965, AMD/Mesa, and
# other VAAPI hosts. On x86_64, the optional source-built iHD driver replaces
# only Debian's iHD file. NVIDIA libraries are supplied by NVIDIA Container
# Toolkit at runtime. Every accelerator remains gated by a complete probe.
COPY --from=intel-media-builder /export /tmp/intel-export
COPY --from=hlsjs-builder /export/ /opt/tvt-archive/static/
COPY LICENSE THIRD_PARTY.md /usr/share/doc/tvt-archive/
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 ffmpeg ca-certificates vainfo va-driver-all && \
    rm -rf /var/lib/apt/lists/* && \
    multiarch="$(python3 -c 'import sysconfig; print(sysconfig.get_config_var("MULTIARCH") or "")')" && \
    if [ -s /tmp/intel-export/dri/iHD_drv_video.so ] && [ -n "$multiarch" ]; then \
      install -D -m 0644 /tmp/intel-export/dri/iHD_drv_video.so "/usr/lib/${multiarch}/dri/iHD_drv_video.so"; \
      cp -a /tmp/intel-export/lib/. "/usr/lib/${multiarch}/"; \
    fi && \
    if [ -d /tmp/intel-export/licenses ]; then \
      mkdir -p /usr/share/doc/tvt-archive/third-party && \
      cp -a /tmp/intel-export/licenses/. /usr/share/doc/tvt-archive/third-party/; \
    fi && \
    rm -rf /tmp/intel-export && \
    ldconfig && \
    groupadd --gid 10001 tvt-archive && \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin tvt-archive && \
    mkdir -p /config /state /cache /opt/tvt-archive/app /opt/tvt-archive/static && \
    chown 10001:10001 /config /state /cache && \
    find / -xdev -type f -perm /6000 -exec chmod a-s {} +

WORKDIR /opt/tvt-archive
COPY --chown=root:root host/app/bridge.py ./app/bridge.py
COPY --chown=root:root host/app/native9008.py ./app/native9008.py
COPY --chown=root:root host/app/archive_capture.py ./app/archive_capture.py
COPY --chown=root:root docker/entrypoint.sh ./entrypoint.sh
RUN chmod 0755 ./app/bridge.py ./app/native9008.py ./app/archive_capture.py ./entrypoint.sh

USER 10001:10001
EXPOSE 8099
ENTRYPOINT ["/opt/tvt-archive/entrypoint.sh"]

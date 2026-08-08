# syntax=docker/dockerfile:1
ARG DEBIAN_VERSION=trixie-slim

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
ARG APP_VERSION=0.8.3
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
    TVT_ARCHIVE_INTEL_MEDIA_DRIVER_VERSION=debian-trixie \
    TVT_ARCHIVE_HLS_JS_VERSION=1.6.16

COPY --from=hlsjs-builder /export/ /opt/tvt-archive/static/
COPY LICENSE THIRD_PARTY.md /usr/share/doc/tvt-archive/

# Keep one generalized image:
# - packaged VAAPI drivers remain available for Intel i965, AMD/Mesa and other hosts;
# - amd64 additionally gets Debian Trixie's full-feature Intel iHD package;
# - NVIDIA runtime libraries are supplied by NVIDIA Container Toolkit;
# - every hardware path is selected only after a complete application probe;
# - software H.264 remains the final fallback.
RUN set -eux; \
    sed -ri 's/^Components: .*/Components: main contrib non-free non-free-firmware/' \
      /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    packages="python3 ffmpeg ca-certificates vainfo va-driver-all"; \
    if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
      packages="$packages intel-media-va-driver-non-free"; \
    fi; \
    apt-get install -y --no-install-recommends $packages; \
    rm -rf /var/lib/apt/lists/*; \
    ldconfig; \
    groupadd --gid 10001 tvt-archive; \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin tvt-archive; \
    mkdir -p /config /state /cache /opt/tvt-archive/app /opt/tvt-archive/static; \
    chown 10001:10001 /config /state /cache; \
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

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
ARG APP_VERSION=0.8.4
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
    CACHE_DIRECTORY=/cache

COPY --from=hlsjs-builder /export/ /opt/tvt-archive/static/
COPY LICENSE THIRD_PARTY.md /usr/share/doc/tvt-archive/

# va-driver-all plus the Intel iHD driver on amd64 serve the optional vaapi encoder.

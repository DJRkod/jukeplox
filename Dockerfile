# ── airplay binary selector ──────────────────────────────────────────────────
# Tiny stage that picks the right-arch cliap2 + cliraop binary out of vendor/
# so the runtime stage only carries the target arch's binaries (~7 MB saved
# on the off-arch). TARGETARCH is "amd64" or "arm64"; the upstream MA binary
# names use "x86_64" and "aarch64", so this stage performs the mapping once
# and exposes a stable name to runtime COPY.
FROM alpine:3 AS airplay-bin
ARG TARGETARCH
COPY vendor/airplay/cliap2-linux-x86_64   /staging/cliap2-amd64
COPY vendor/airplay/cliap2-linux-aarch64  /staging/cliap2-arm64
COPY vendor/airplay/cliraop-linux-x86_64  /staging/cliraop-amd64
COPY vendor/airplay/cliraop-linux-aarch64 /staging/cliraop-arm64
RUN cp /staging/cliap2-${TARGETARCH}  /cliap2  && \
    cp /staging/cliraop-${TARGETARCH} /cliraop && \
    chmod +x /cliap2 /cliraop

# ── builder ──────────────────────────────────────────────────────────────────
# Pinned to bookworm (Debian 12) because the vendored Music Assistant cliap2
# binary was built against FFmpeg 5.x and dynamically links
# libavformat.so.59 / libavcodec.so.59 / libavutil.so.57 — those ABIs are
# the bookworm ffmpeg versions. The unpinned python:3.12-slim tag has
# rolled forward to Debian 13 (trixie) which ships ffmpeg 7.x and only
# provides libavformat.so.61, which the binary cannot load.
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build
COPY pyproject.toml .

# GI dev headers, Cairo (required by pycairo which PyGObject depends on), and
# build tools — compile-time only, not copied to the runtime stage.
# Package name is libgirepository1.0-dev (no hyphen between repository and 1.0)
# on bookworm; trixie introduces libgirepository-1.0-dev with the hyphen.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgirepository1.0-dev \
    libcairo2-dev \
    gcc \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir hatchling && \
    pip install --no-cache-dir --prefix=/install "PyGObject<3.50" && \
    pip install --no-cache-dir --prefix=/install .
# Image: djrkod/jukeplox:latest

# ── runtime ──────────────────────────────────────────────────────────────────
# Same bookworm pin as the builder — see comment at the top of the file.
FROM python:3.12-slim-bookworm

# GStreamer stack + GObject Introspection runtime libraries.
# The compiled PyGObject (_gi.so) is copied from the builder stage and links
# against libgirepository-1.0-1 (listed explicitly; no longer a plugins-bad transitive dep).
#
# libavformat/libavcodec/libavutil/libswresample/libswscale are FFmpeg's
# shared libraries — required at runtime by the vendored cliap2 / cliraop
# binaries (which dynamically link them). The `ffmpeg` package brings the
# CLI but not always all transitive libav*; listing them explicitly
# guarantees cliap2 finds libavformat.so.59 et al. at exec time.
#
# `flac` provides metaflac, used by app/api/stream.py to add a SEEKTABLE to the
# OGG->FLAC transcode. ffmpeg's FLAC muxer writes none, and without one a
# constrained Cast receiver (JBL Charge 5) blind-byte-estimates a seek, desyncs,
# and ERRORs at end-of-track (2026-06-17).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-pulseaudio \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    libgirepository-1.0-1 \
    ffmpeg \
    flac \
    libplist3 \
    libevent-2.1-7 \
    libevent-pthreads-2.1-7 \
    libcurl4 \
    libconfuse2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY --from=airplay-bin /cliap2  /usr/local/bin/cliap2
COPY --from=airplay-bin /cliraop /usr/local/bin/cliraop

WORKDIR /app
COPY app/ ./app/
COPY static/ ./static/

# Build-info ARGs — passed via `--build-arg` at docker build time so the
# running container can report exactly which commit + build timestamp it
# came from. Without this, "did my image actually update?" is unanswerable
# from inside the container. The app exposes both via the startup log
# line and the GET /api/version endpoint. Defaults are sentinels that make
# "you forgot --build-arg" obvious in production logs.
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
ARG IMAGE_TAG=unknown

VOLUME ["/data"]
# PORT is the HTTP listen port. It MUST be runtime-configurable: under
# `--network host` (required for mDNS discovery) there is no Docker port
# translation, so the container binds this port directly on the host. The
# host's default port 80 is frequently taken (the TrueNAS Scale web UI uses
# it), which would crash-loop uvicorn — set PORT to a free host port instead.
ENV DATA_DIR=/data \
    LOG_LEVEL=info \
    BIND_HOST=0.0.0.0 \
    PORT=80 \
    JUKEPLOX_GIT_SHA=${GIT_SHA} \
    JUKEPLOX_BUILD_TIME=${BUILD_TIME} \
    JUKEPLOX_IMAGE_TAG=${IMAGE_TAG}

# Documentation hint only (EXPOSE can't read runtime env, and host networking
# ignores it). The real listen port is ${PORT} below.
EXPOSE 80

# Shell form (not JSON) is REQUIRED so ${BIND_HOST}/${PORT} expand at runtime.
CMD uvicorn app.main:app --host ${BIND_HOST} --port ${PORT:-80}

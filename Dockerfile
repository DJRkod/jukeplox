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
# TARGETARCH is auto-populated by BuildKit/buildx (the normal multi-arch CI
# path). Fall back to `uname -m` when it is empty so the build ALSO works on the
# classic (non-BuildKit) builder — otherwise the arch suffix is blank and the cp
# fails with "can't stat /staging/cliap2-" (hit building natively on the arm64
# rig, 2026-08-12).
RUN set -eux; \
    a="${TARGETARCH:-}"; \
    if [ -z "$a" ]; then \
        case "$(uname -m)" in \
            x86_64) a=amd64 ;; \
            aarch64|arm64) a=arm64 ;; \
            *) echo "unsupported build arch $(uname -m)"; exit 1 ;; \
        esac; \
    fi; \
    cp /staging/cliap2-"$a"  /cliap2  && \
    cp /staging/cliraop-"$a" /cliraop && \
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
#
# `gstreamer1.0-libav` gives GStreamer the libav (avdec_*) decoders — notably
# avdec_aac for AAC/HE-AAC, which a large share of internet-radio stations use.
# Without it the Direct backend fails such stations with "missing a plug-in"
# (rig-caught 2026-08-11). It depends on the SAME bookworm libav .59 the image
# already ships for cliap2, so there is no ABI conflict with AirPlay.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-pulseaudio \
    gstreamer1.0-alsa \
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
    snapserver \
    snapclient \
    && rm -rf /var/lib/apt/lists/*

# ── snapserver version gate (2026-08-11 plan U3) ──────────────────────────────
# The base image is pinned to bookworm, whose apt suite ships snapcast **0.26.0**
# (verified 2026-08-12 at image build — the plan's "0.27.0" assumption was wrong).
# So `apt-get install snapserver` above deterministically lands in-range without
# an exact `=version` pin (which would be fragile across amd64/arm64 binNMU
# revisions). This RUN is the HARD BUILD GATE: if a future base-image bump rolls
# the suite into 0.30.x (a known-incompatible series) or below 0.26, the build
# FAILS here rather than silently shipping an untested snapserver. 0.26 supports
# the tcp source + JSON-RPC control + idle_threshold the backend needs (proven by
# the U10 in-image e2e). `snapclient` is bundled as the hardware-free receiver.
RUN set -eux; \
    v="$(snapserver --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1)"; \
    echo "snapserver version: ${v}"; \
    major="$(echo "$v" | cut -d. -f1)"; minor="$(echo "$v" | cut -d. -f2)"; \
    if [ "$major" != "0" ] || [ "$minor" -lt 26 ] || [ "$minor" -ge 30 ]; then \
        echo "ERROR: snapserver ${v} is outside the supported [0.26, 0.30) range (plan U3)"; \
        exit 1; \
    fi

COPY --from=builder /install /usr/local
COPY --from=airplay-bin /cliap2  /usr/local/bin/cliap2
COPY --from=airplay-bin /cliraop /usr/local/bin/cliraop

WORKDIR /app
COPY app/ ./app/
COPY static/ ./static/
# Stub snapserver.conf (2026-08-11 plan U3/U4): snapserver silently ignores its
# CLI --stream.* args when its config file is missing, so the embedded
# supervisor always launches it with --config=/app/config/snapserver.conf. The
# stub carries only empty section headers; all real config is passed on the CLI.
COPY config/ ./config/

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

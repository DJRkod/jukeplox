"""Transcode-content-type helpers — shared by the stream proxy (`app/api/stream.py`)
and the output backends (`app/output/chromecast.py`, `app/output/dlna.py`).

These pure functions decide whether `/api/stream` will transcode a source to FLAC
and what content-type a renderer should be told. They live in a neutral top-level
module (not the HTTP layer) so the output backends don't import from `app/api/*`
(code-review #10, 2026-06-18 — was a wrong-layer dependency).
"""
from __future__ import annotations

_OGG_EXTS = (".ogg", ".oga", ".opus")


def transcodes_to_flac(part_path: str) -> bool:
    """Whether ``/api/stream`` will transcode a source at ``part_path`` to FLAC.

    The part-path file extension is the authoritative signal — Plex mislabels
    Ogg parts' content-types (see the 2026-06-14 learning), the extension never
    lies. This is the single source of truth shared with the Cast/DLNA backends:
    they call it to decide the content-type they advertise to a renderer, so the
    declared type stays in lockstep with the bytes this proxy actually serves.
    Advertising the source type (``audio/ogg``) for a stream we hand over as
    FLAC made the Chromecast receiver mis-initialize its decode pipeline — ~1s
    of audio, then an error and a dropped control channel (2026-06-17).
    """
    path = part_path.split("?", 1)[0].lower()
    return path.endswith(_OGG_EXTS)


def device_stream_content_type(stream_url: str, part_path: str, native: str) -> str:
    """The content-type a Cast/DLNA renderer will actually receive for a track.

    ``/api/stream`` transcodes OGG-family sources to FLAC and passes everything
    else through unchanged. A renderer fetching through the proxy must therefore
    be told ``audio/flac`` for an OGG source (not the source ``native`` type);
    a direct (non-proxied) Plex URL is never transcoded, so ``native`` stands.

    Detection mirrors the serve-side ``_needs_transcode``: BOTH the file
    extension AND an ``audio/ogg`` native content-type trigger a FLAC transcode.
    Checking the extension alone misreported the rare OGG part Plex serves
    without an OGG-family extension — the proxy would transcode it to FLAC while
    the backend advertised ``audio/ogg`` (code-review #11, 2026-06-18).
    """
    if "/api/stream" in stream_url and (
        transcodes_to_flac(part_path) or (native or "").lower().startswith("audio/ogg")
    ):
        return "audio/flac"
    return native

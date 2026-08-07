"""Music-source provider package (multi-source rebuild, 2026-06-27).

Each connected media source (Plex, Jellyfin, local files) is a ``MusicSource``
implementation behind one interface (``base.py``). The ``registry`` (added in
U3) holds 0..N providers and routes by ``source_id``; it replaces the legacy
``get_plex_client`` singleton. No browse/queue/playback code path assumes Plex
specifically — Plex is just one provider (``plex.py``).
"""

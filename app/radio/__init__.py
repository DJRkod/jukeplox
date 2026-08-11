"""Radio Mode — internet radio via the Radio Browser directory.

This package lives OUTSIDE ``app/sources`` and the ``SourceRegistry`` on purpose:
a station is an endless live stream with no album/artist/track identity, so it does
not fit the ``MusicSource`` / ``StreamTarget`` (finite, seekable) contract. Radio
Browser is an anonymous service — there are no credentials to store or seal.

``app/radio/client.py`` holds the directory client (mirror discovery + failover,
browse/search, resolve-to-playable-URL, fire-and-forget click reporting, and an
SWR cache) used by the radio API layer. Later units add the SSRF URL validator
(``urlcheck.py``), the session/takeover manager (``session.py``), the transcode
proxy (``stream.py``), and the ICY title reader (``icy.py``).
"""

from __future__ import annotations

from app.radio.client import (
    RadioBrowserClient,
    RadioDirectoryUnavailable,
    Station,
    get_radio_client,
)

__all__ = [
    "RadioBrowserClient",
    "RadioDirectoryUnavailable",
    "Station",
    "get_radio_client",
]

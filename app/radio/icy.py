"""Bounded in-band ICY ``StreamTitle`` reader (radio plan U6).

Surfaces the live "now playing" title a station embeds in its stream, for the
backends that do NOT get it for free. GStreamer-direct reads titles off the
playbin bus for nothing (``GST_TAG_TITLE`` — see ``app/output/direct.py``); the
Cast/DLNA/AirPlay paths hand the audio to a device or a transcode-proxy that does
not surface the title, so the jukebox reads it *itself* with a small, bounded
server-side read fed into device metadata + the WS broadcast.

Missing title is the NORMAL case — most stations send ``L=0`` (no metadata) on any
given cycle, and many send no ``icy-metaint`` header at all. Every "no title"
branch returns ``None`` cleanly; the UI degrades to station-name-only (AE5).

ICY protocol facts this builds to (https://cast.readme.io/docs/icy)
------------------------------------------------------------------
- Request header ``Icy-MetaData: 1`` asks the server to interleave metadata.
- Response header ``icy-metaint: N`` gives the audio-byte interval. **Absent ⇒
  the server sends no metadata; do NOT parse — return None.**
- Stream layout, repeating: ``N`` audio bytes, then 1 length byte ``L``, then
  ``L * 16`` metadata bytes (null-padded, latin-1). ``L`` is one byte so the
  metadata block is inherently bounded to ``255 * 16 = 4080`` bytes. ``L == 0``
  means "no metadata this cycle" (common) ⇒ no title.
- The metadata block is ``Key='value';`` fields; the one we want is
  ``StreamTitle='Artist - Title';``.

SEC-004 — the ``StreamTitle`` is untrusted third-party text
-----------------------------------------------------------
Treat it as PLAIN TEXT at every sink. :func:`sanitize_title` normalizes it
(strips control chars, collapses whitespace, bounds length) but does NOT HTML/XML
escape — escaping is the *sink's* job so each sink escapes for its own context:

- **WS path (U7 ``RadioStateEvent.live_title``):** the value is carried as a plain
  JSON string. The client MUST render it via ``element.textContent`` (never
  ``innerHTML``) — a JSON string is inert in JS, so no HTML runs. This is the
  contract U7's client owns; stated here so the U7 implementer does not inject it
  as HTML.
- **Cast/DLNA device metadata (DIDL ``dc:title`` / Cast media ``title``):** the
  title is embedded in XML, so the sink XML-escapes it (``html.escape``). DLNA's
  ``_didl_metadata`` already ``html.escape``s the title; :func:`xml_escape_title`
  is the shared helper for the Cast metadata path and any future DIDL sink.

``ICY 200 OK`` deferral
-----------------------
SHOUTcast-v1 servers answer with a raw ``ICY 200 OK`` status line instead of
``HTTP/1.x 200 OK``, which httpx/h11 rejects at the status line — those stations
will raise here and yield no title. A raw-socket fallback for them is DEFERRED
(plan Open Questions: verify prevalence first). A normal httpx GET is fine for the
common Icecast/HTTP case U6 targets.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urlsplit

import httpx

_log = logging.getLogger("jukeplox.radio")


def _redact_url(url: str) -> str:
    """Reduce a station URL to just ``scheme://host[:port]`` for logging — the
    path/query can carry a credential token (SEC / F1), so never log the raw URL.
    Falls back to ``<redacted>`` if the URL can't be parsed."""
    try:
        parts = urlsplit(url or "")
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    except Exception:  # pragma: no cover - urlsplit is very tolerant
        pass
    return "<redacted>"

# ── bounds (a lying/hostile server must never hang or exhaust memory) ────────────
#
# L is a single byte, so a metadata block is at most 255 * 16 = 4080 bytes. We
# enforce it explicitly (defence in depth) rather than trusting the byte.
_ICY_META_BLOCK_MAX = 255 * 16  # 4080

# Total bytes we will ever pull from the stream before giving up. icy-metaint is
# typically 8192-16000; one full cycle is metaint + 1 + 4080. Cap generously above
# a normal cycle but far below "stream forever" so a server that lies about
# icy-metaint (or never emits the length byte) can't stream unboundedly. If we hit
# this cap without a complete block, we return None (no title) rather than hang.
_ICY_TOTAL_READ_CAP = 512 * 1024  # 512 KiB

# Read granularity while discarding audio bytes / collecting the block.
_ICY_READ_CHUNK = 16 * 1024

# Bound the whole bounded-read op (connect + read one block) so a slow-loris
# upstream can't wedge the periodic reader task.
_ICY_TIMEOUT_S = 8.0

# Sanitized title length bound — a title is a one-line "now playing" label, not a
# payload. Anything longer is truncated (untrusted source, SEC-004).
_TITLE_MAX_LEN = 400

# StreamTitle='...'; — value is single-quote-delimited, terminated by ';'. Some
# servers omit the trailing ';' on the last field, so tolerate end-of-block too.
_STREAMTITLE_RE = re.compile(rb"StreamTitle='(.*?)'(?:;|$)", re.DOTALL)

# Control chars (except nothing — a title is single-line) collapsed away in the
# sanitizer. Kept module-level so the compile is one-time.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")


def parse_stream_title(block: bytes) -> Optional[str]:
    """Extract the ``StreamTitle`` from one raw ICY metadata block (pure helper).

    ``block`` is the ``L * 16`` metadata bytes (null-padded). Returns the RAW
    title string (latin-1 decoded, NUL-trimmed, stripped) or ``None`` when the
    block carries no ``StreamTitle`` (e.g. an empty block, or only other keys).
    Does NOT sanitize — callers pass the result through :func:`sanitize_title`
    before any sink. Separately unit-testable; never raises on garbage input
    (latin-1 decodes every byte, and a non-match returns None).
    """
    if not block:
        return None
    m = _STREAMTITLE_RE.search(block)
    if not m:
        return None
    # latin-1 decodes any byte 0-255 without error — the ICY spec's declared
    # encoding, and "errors tolerated" per the plan even for non-conforming bytes.
    raw = m.group(1).decode("latin-1", errors="replace")
    # ICY blocks are NUL-padded to the 16-byte boundary; a NUL can also land
    # inside a short value's tail — trim trailing NULs then strip.
    raw = raw.replace("\x00", "").strip()
    return raw or None


def sanitize_title(s: Optional[str]) -> Optional[str]:
    """Normalize an untrusted ``StreamTitle`` to a safe PLAIN-TEXT label (SEC-004).

    Applied before the title reaches ANY sink. Strips control characters (a title
    is a single-line label — CR/LF/NUL/etc. have no place and could break a log
    line or a DIDL body), collapses runs of whitespace, and bounds the length.
    Does NOT HTML/XML-escape — that is each sink's responsibility for its own
    context (see the module docstring). Returns ``None`` for empty/blank input so
    "no title" stays a single sentinel everywhere.
    """
    if not s:
        return None
    cleaned = _CONTROL_RE.sub(" ", s)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    if not cleaned:
        return None
    if len(cleaned) > _TITLE_MAX_LEN:
        cleaned = cleaned[:_TITLE_MAX_LEN].rstrip()
    return cleaned or None


def xml_escape_title(s: Optional[str]) -> str:
    """XML-escape a (already-sanitized) title for a DIDL / Cast metadata sink.

    The Cast/DLNA device-metadata path embeds the title in XML, so ``<``, ``&``,
    ``"`` etc. MUST be escaped or they break the DIDL body / could inject markup
    (SEC-004). ``None`` ⇒ empty string (the sink advertises no title). DLNA's
    ``_didl_metadata`` already does this inline; this is the shared helper for the
    Cast metadata path and any future DIDL caller.
    """
    import html

    return html.escape(s or "", quote=True)


async def read_stream_title(
    url: str, *, client: Optional[httpx.AsyncClient] = None
) -> Optional[str]:
    """Bounded, best-effort read of the current ``StreamTitle`` from ``url``.

    Opens the stream with ``Icy-MetaData: 1``; if the response has no
    ``icy-metaint`` header, returns ``None`` WITHOUT parsing (the server sends no
    metadata). Otherwise streams bytes: discards exactly ``icy-metaint`` audio
    bytes, reads the 1 length byte, reads ``L * 16`` metadata bytes, parses the
    ``StreamTitle``, and closes the connection after ONE block. Returns the
    sanitized title, or ``None`` (no metaint / ``L == 0`` / no ``StreamTitle`` /
    any failure).

    Best-effort and non-raising by contract — this runs OFF the critical path
    (periodic reader for Cast/DLNA/AirPlay) and must never block playback start or
    propagate an error. Read against the ORIGINAL station URL (the transcode-proxy
    re-encodes to mp3 and strips ICY). ``client`` is injectable for tests (a
    ``httpx.MockTransport``-backed client); a fresh short-lived client is created
    when omitted.

    Guards: ``L * 16`` clamped to :data:`_ICY_META_BLOCK_MAX` (defence in depth —
    ``L`` is one byte so it's already ``<= 4080``); a total-read cap
    (:data:`_ICY_TOTAL_READ_CAP`) so a server that lies about ``icy-metaint`` or
    never emits the length byte can't stream forever; latin-1 decode tolerates
    undecodable bytes; an overall timeout bounds a slow upstream.
    """
    own_client = client is None
    if own_client:
        # follow_redirects=False (F1/SSRF): a redirect during the bounded title
        # read must NOT be followed — it is an SSRF vector (a public station could
        # 302 an ICY read to an internal host). The caller pre-validates the host;
        # this read is one hop only, no redirect chase.
        client = httpx.AsyncClient(
            timeout=_ICY_TIMEOUT_S, http2=False, follow_redirects=False
        )
    try:
        return await _read_one_block(url, client)
    except Exception:
        # Untrusted upstream + best-effort read: ICY 200 OK (h11 reject), a
        # timeout, a connection drop, a short body — all degrade to "no title".
        # Redact the URL (path/query may carry a token) — log only the host (F1).
        _log.debug("radio: ICY title read failed for %s (no title)",
                   _redact_url(url), exc_info=True)
        return None
    finally:
        if own_client and client is not None:
            await client.aclose()


async def _read_one_block(url: str, client: httpx.AsyncClient) -> Optional[str]:
    """Streamed read of exactly one ICY metadata block (the core of the reader)."""
    headers = {"Icy-MetaData": "1", "Accept": "*/*"}
    async with client.stream("GET", url, headers=headers) as resp:
        # icy-metaint absent ⇒ no interleaved metadata; do not parse (AE5).
        metaint_raw = resp.headers.get("icy-metaint")
        if not metaint_raw:
            return None
        try:
            metaint = int(metaint_raw)
        except (TypeError, ValueError):
            return None
        if metaint <= 0 or metaint > _ICY_TOTAL_READ_CAP:
            # A non-positive or absurd interval is a lying/broken server.
            return None

        aiter = resp.aiter_bytes(_ICY_READ_CHUNK)
        buf = bytearray()
        total = 0

        async def pull() -> bool:
            """Pull one chunk into ``buf``; False on EOF or when the total-read
            cap is hit (a lying server can't stream forever)."""
            nonlocal total
            if total >= _ICY_TOTAL_READ_CAP:
                return False
            try:
                chunk = await aiter.__anext__()
            except StopAsyncIteration:
                return False
            if not chunk:
                return False
            buf.extend(chunk)
            total += len(chunk)
            return True

        # 1) Discard exactly `metaint` audio bytes.
        while len(buf) < metaint:
            if not await pull():
                return None  # stream ended before the first metadata point
        del buf[:metaint]

        # 2) Read the 1 length byte L.
        while len(buf) < 1:
            if not await pull():
                return None
        length = buf[0]
        del buf[:1]
        if length == 0:
            return None  # no metadata this cycle (common) — station-name-only

        # 3) Read L*16 metadata bytes (bounded — L is one byte so <= 4080).
        block_len = min(length * 16, _ICY_META_BLOCK_MAX)
        while len(buf) < block_len:
            if not await pull():
                return None  # truncated block — no title
        block = bytes(buf[:block_len])

    # Connection closed by the `async with` on exit — we read exactly one block.
    return sanitize_title(parse_stream_title(block))

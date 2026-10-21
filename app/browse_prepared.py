"""Pure helpers for the prepared browse answer: input signature + bytes.

Everything here is a pure function. No database, no app state, no network — so
every rule below is directly testable, following the convention set by
``app/album_fold.py`` and ``app/normalize.py``.

Why a signature at all
----------------------
The prepared answer is served only when a signature of every input it derives
from matches the signature stored beside it. Readers *compare* rather than
trust a freshness stamp, which is what makes a missed invalidation **slow**
instead of **wrong**: an input nobody remembered to wire still changes the
signature, the comparison fails, and the request falls back to live derivation.
That inversion is the whole reason the rest of the design is safe.

Three properties are load-bearing, and each exists because the first draft of
this design got it wrong:

**Content, not clocks.** Components describing data are digests over the rows
actually installed, never the wall-clock completion stamps this repo already
writes. Those stamps advance on every refresh whether or not a row changed, and
with a six-hour TTL plus an ungated refresh at startup a stamp-derived
signature would self-invalidate several times a day and again on every restart
— forcing a full re-derivation and a full multi-megabyte re-download each time,
which is exactly what the conditional-request behaviour exists to prevent.

**Canonical framing.** Components reduce operator-controlled strings, so a
delimiter-free join lets two distinct states digest identically — reachable by
renaming a server so a boundary shifts. Every part is length-prefixed in a
fixed order. Per-component tests cannot catch this class of bug, because each
varies one component with the rest held fixed.

**One-way, because this is a guest-facing header.** The signature doubles as
the HTTP validator sent to unauthenticated clients, and two components reduce
objects carrying library titles, server names and the server owner's username —
which this repo contractually keeps off any guest-reachable payload. Note the
precedent deliberately *not* followed: the artist-grouping map's rule signature
is ``repr()``, which is correct for an in-memory comparison and unusable as a
header.

LOGIC_VERSION
-------------
Bump when the grouping, the ordering, or the emitted field set changes —
anything that makes previously-stored bytes wrong while every input digest
stays the same. Its blast radius is the album grouping in ``app/api/guest.py``
and the catalog fold in ``app/catalog/views.py``.

Forgetting the bump is not guarded by "does the constant reach the signature"
(that passes forever); it is guarded by the pinned-output test in
``tests/test_browse_prepared.py``, which fails when derived output changes and
tells you to bump and re-pin.
"""
from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json

from fastapi.encoders import jsonable_encoder

LOGIC_VERSION = 1

FACET_ARTISTS = "artists"
FACET_ALBUMS = "albums"

# Which of the two serving paths would answer. Part of the signature because a
# stored answer from one path must never be served by the other, and because
# the selector is derived from live source registry membership rather than from
# configuration — so it can move without any operator action.
PATH_NATIVE = "native"
PATH_CATALOG = "catalog"

_DIGEST_CHARS = 16
_SIGNATURE_CHARS = 32


@dataclasses.dataclass(frozen=True)
class SignatureInputs:
    """Every input a prepared answer derives from.

    Callers hand in already-reduced digests rather than raw values. That keeps
    this module pure, keeps the hot path from re-reading the database, and
    means no configured name can survive into the guest-facing validator even
    if a caller is careless.

    Several components are scoped to one facet or one serving path. That is not
    tidiness: including the credit roster in the albums signature would
    invalidate the albums answer every time an unrelated crawl finished, and
    including server order in the artists signature would invalidate it for an
    input that path never reads.
    """

    path: str
    index_content: str
    rules: str
    exclusions: str
    enabled_libraries: str
    server_order: str = ""        # albums only
    credit_roster: str = ""       # artists only
    catalog_content: str = ""     # catalog path only
    registry: str = ""            # catalog path only

    def replace(self, **changes) -> "SignatureInputs":
        return dataclasses.replace(self, **changes)


def _framed(*parts: str) -> bytes:
    """Length-prefix every part so no two part lists can collide.

    ``("ab", "c")`` becomes ``2:ab;1:c;`` and ``("a", "bc")`` becomes
    ``1:a;2:bc;`` — distinct, where a bare join would make them identical.
    """
    out = bytearray()
    for part in parts:
        raw = ("" if part is None else str(part)).encode("utf-8")
        out += str(len(raw)).encode("ascii")
        out += b":"
        out += raw
        out += b";"
    return bytes(out)


def digest_values(*values: str) -> str:
    """Reduce an ordered collection of strings to an opaque, framed digest.

    Use this for every component built from operator-controlled strings —
    library titles, server names, exclusion entries — so the composed
    signature cannot carry them.
    """
    return hashlib.sha256(_framed(*values)).hexdigest()[:_DIGEST_CHARS]


def signature(facet: str, inputs: SignatureInputs,
              logic_version: int | None = None) -> str:
    """The one composition authority.

    Composed in two places — the rebuild and the request path — a single byte
    of disagreement means the stored answer never matches, the app falls back
    forever, and every test still passes while it is strictly worse than before
    (each guest paying live derivation *and* re-arming a rebuild). So both
    sides call this, and nothing composes a digest locally.
    """
    version = LOGIC_VERSION if logic_version is None else logic_version
    parts = [
        "v", str(version),
        "facet", facet,
        "path", inputs.path,
        "index", inputs.index_content,
        "rules", inputs.rules,
        "excl", inputs.exclusions,
        "libs", inputs.enabled_libraries,
    ]
    if facet == FACET_ALBUMS:
        parts += ["order", inputs.server_order]
    if facet == FACET_ARTISTS:
        parts += ["credit", inputs.credit_roster]
    if inputs.path == PATH_CATALOG:
        # Registry membership is reachability-derived, so a source that is
        # briefly unreachable at rebuild time would otherwise produce rows with
        # downgraded source attribution that then match and serve fast.
        parts += ["cat", inputs.catalog_content, "reg", inputs.registry]
    return hashlib.sha256(_framed(*parts)).hexdigest()[:_SIGNATURE_CHARS]


def serialize(rows) -> bytes:
    """The bytes a guest receives, identical to what the route emits today.

    The browse routes declare no response model, so the framework encodes with
    non-ASCII left unescaped and compact separators. A plain ``json.dumps``
    differs on *both* defaults — on a real library full of accented and CJK
    names that is a mismatch across a large fraction of the payload, not an
    edge case — so the framework's own encoder is used rather than reproduced.
    """
    return json.dumps(
        jsonable_encoder(rows),
        ensure_ascii=False,
        allow_nan=False,
        indent=None,
        separators=(",", ":"),
    ).encode("utf-8")


def compress(data: bytes) -> bytes:
    """Gzip once, at rebuild time, off the event loop.

    ``mtime=0`` keeps the output deterministic; gzip otherwise embeds the
    current time, so identical input would produce different bytes and defeat
    any byte-level comparison in tests or diagnostics.
    """
    return gzip.compress(data, compresslevel=6, mtime=0)


def decompress(data: bytes) -> bytes:
    """Only the compressed copy is stored, so a client that does not accept
    compression is served by expanding it — in the executor, never on the
    event loop."""
    return gzip.decompress(data)

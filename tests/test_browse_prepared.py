"""U1: the input signature and the serialization helpers.

This module is the correctness spine of the prepared-browse plan, so its tests
carry the weight. Three of them exist because a document review found the
original design broken:

* **One signature per input set, stable across calls.** The stored answer is
  served only when the current signature matches the stored one, so a
  signature that is not a pure function of its inputs means either never
  serving (silent total failure, all tests green) or serving a wrong answer.
* **Content digests, never completion timestamps.** An earlier design sourced
  the library-contents component from the freshness stamp this repo already
  writes -- a wall-clock value advanced on every refresh whether or not a row
  changed. With a six-hour TTL and an ungated refresh at startup that
  self-invalidates several times a day and again on every restart, forcing a
  full re-derivation and a full multi-megabyte re-download each time. The test
  that would have caught it is
  ``test_identical_content_yields_one_signature``.
* **Canonical framing.** Several components reduce operator-controlled strings.
  Concatenated without framing, two distinct states can digest identically --
  reachable by renaming a server so a delimiter boundary shifts. Note that the
  per-component tests below CANNOT catch this: each varies one component with
  the others fixed, which is exactly the case a framing bug survives. Hence
  ``test_boundary_shift_does_not_collide``.

The signature is also the HTTP validator sent to unauthenticated guests, so
``test_signature_leaks_no_configured_names`` pins that no library title,
server name or owner username survives into it.
"""
import gzip
import json

import pytest

from app import browse_prepared as bp


# ── helpers ──────────────────────────────────────────────────────────────────

def _inputs(**over):
    """A complete input set; tests override one component at a time."""
    base = dict(
        path=bp.PATH_NATIVE,
        index_content="idx-aaa",
        rules="rules-aaa",
        exclusions="excl-aaa",
        enabled_libraries="libs-aaa",
        server_order="order-aaa",
        credit_roster="credit-aaa",
        catalog_content="cat-aaa",
        registry="reg-aaa",
    )
    base.update(over)
    return bp.SignatureInputs(**base)


# ── stability and purity ─────────────────────────────────────────────────────

def test_identical_inputs_yield_identical_signatures():
    a = bp.signature(bp.FACET_ALBUMS, _inputs())
    b = bp.signature(bp.FACET_ALBUMS, _inputs())
    assert a == b
    assert isinstance(a, str) and a


def test_identical_content_yields_one_signature():
    """The timestamp lesson, as a test.

    Two refreshes that install identical content must produce one signature.
    This passes trivially here because the component IS a content digest --
    which is the point: had it been the completion stamp, two identical
    refreshes would differ and every guest would re-download the library.
    """
    first = bp.signature(bp.FACET_ALBUMS, _inputs(index_content="same-rows"))
    second = bp.signature(bp.FACET_ALBUMS, _inputs(index_content="same-rows"))
    assert first == second


def test_facets_do_not_share_a_signature():
    assert (bp.signature(bp.FACET_ALBUMS, _inputs())
            != bp.signature(bp.FACET_ARTISTS, _inputs()))


# ── every input is load-bearing, one failing-if-removed test each ────────────

@pytest.mark.parametrize("field,value", [
    ("path", bp.PATH_CATALOG),
    ("index_content", "idx-zzz"),
    ("rules", "rules-zzz"),
    ("exclusions", "excl-zzz"),
    ("enabled_libraries", "libs-zzz"),
])
def test_each_shared_input_changes_both_facets(field, value):
    """A component dropped from composition fails a named test rather than
    being discovered as a wrong answer in production."""
    for facet in (bp.FACET_ALBUMS, bp.FACET_ARTISTS):
        assert (bp.signature(facet, _inputs())
                != bp.signature(facet, _inputs(**{field: value}))), (facet, field)


def test_server_order_is_albums_only():
    """The artists path never reads server order, so including it there would
    invalidate the artists answer for nothing."""
    changed = _inputs(server_order="order-zzz")
    assert (bp.signature(bp.FACET_ALBUMS, _inputs())
            != bp.signature(bp.FACET_ALBUMS, changed))
    assert (bp.signature(bp.FACET_ARTISTS, _inputs())
            == bp.signature(bp.FACET_ARTISTS, changed))


def test_credit_roster_is_artists_only():
    """Rebuilding albums when the credit roster changes would be waste."""
    changed = _inputs(credit_roster="credit-zzz")
    assert (bp.signature(bp.FACET_ARTISTS, _inputs())
            != bp.signature(bp.FACET_ARTISTS, changed))
    assert (bp.signature(bp.FACET_ALBUMS, _inputs())
            == bp.signature(bp.FACET_ALBUMS, changed))


@pytest.mark.parametrize("field", ["catalog_content", "registry"])
def test_catalog_components_apply_only_to_the_catalog_path(field):
    """Registry membership is reachability-derived, so a multi-source install
    whose source is briefly unreachable must not match a stored answer built
    while it was present."""
    native = _inputs(path=bp.PATH_NATIVE)
    catalog = _inputs(path=bp.PATH_CATALOG)
    for facet in (bp.FACET_ALBUMS, bp.FACET_ARTISTS):
        assert (bp.signature(facet, catalog)
                != bp.signature(facet, catalog.replace(**{field: "zzz"})))
        assert (bp.signature(facet, native)
                == bp.signature(facet, native.replace(**{field: "zzz"})))


def test_logic_version_participates():
    sig = bp.signature(bp.FACET_ALBUMS, _inputs())
    assert sig != bp.signature(bp.FACET_ALBUMS, _inputs(), logic_version=99)


# ── framing: the case per-component tests cannot reach ──────────────────────

def test_boundary_shift_does_not_collide():
    """Two distinct states whose naive concatenation is identical.

    Reachable in production by renaming a server or library so a delimiter-free
    boundary shifts. Every other test in this file varies one component with
    the rest fixed, which is precisely the shape a framing bug survives.
    """
    left = _inputs(index_content="AB", rules="C")
    right = _inputs(index_content="A", rules="BC")
    assert (bp.signature(bp.FACET_ALBUMS, left)
            != bp.signature(bp.FACET_ALBUMS, right))


def test_digest_values_is_order_sensitive_and_framed():
    assert bp.digest_values("a", "b") != bp.digest_values("b", "a")
    assert bp.digest_values("ab", "c") != bp.digest_values("a", "bc")
    assert bp.digest_values() == bp.digest_values()


# ── the signature is a guest-facing header ──────────────────────────────────

def test_signature_leaks_no_configured_names():
    """The signature is sent to unauthenticated guests as a validator, and two
    of its components reduce objects carrying library titles, server names and
    the server owner's username. This repo has a standing contract that those
    never reach a guest-reachable payload, so the value must be opaque.

    Note the precedent NOT followed here: the grouping map's rule signature is
    ``repr()`` -- fine for an in-memory comparison, disastrous as a header.
    """
    secrets = ["Wedding Playlist Library", "basement-plex-01", "someones.name"]
    inputs = _inputs(
        index_content=bp.digest_values(*secrets),
        server_order=bp.digest_values(*secrets),
        enabled_libraries=bp.digest_values(*secrets),
    )
    for facet in (bp.FACET_ALBUMS, bp.FACET_ARTISTS):
        sig = bp.signature(facet, inputs)
        for secret in secrets:
            assert secret not in sig, (facet, secret)
    # Also opaque at the component level, so a caller cannot hand a raw name
    # through and have it survive.
    for secret in secrets:
        assert secret not in bp.digest_values(*secrets)


# ── serialization: byte-identical to what the route emits today ─────────────

def test_serialize_matches_the_framework_encoder():
    """The routes declare no response model, so the framework applies its own
    encoder: non-ASCII left unescaped and compact separators. A plain
    json.dumps differs on BOTH defaults, which on a real library full of
    accented and CJK names is a mismatch across a large fraction of bytes.
    """
    from fastapi.encoders import jsonable_encoder
    from starlette.responses import JSONResponse

    rows = [
        {"id": "a1", "title": "Björk", "artist": "Björk", "year": 1997},
        {"id": "a2", "title": "章子怡", "artist": "Faye", "year": None},
        # The artists payload interleaves dataclass rows with plain dicts for
        # credit acts, so two row shapes must round-trip.
        {"id": "credit:x", "title": "Various", "thumb": None, "release_count": 3},
    ]
    assert bp.serialize(rows) == JSONResponse(jsonable_encoder(rows)).body


def test_serialize_round_trips_through_compression():
    rows = [{"id": "a", "title": "Tête", "artist": "A"}]
    raw = bp.serialize(rows)
    assert bp.decompress(bp.compress(raw)) == raw
    assert json.loads(raw.decode("utf-8"))[0]["title"] == "Tête"


def test_compression_is_actually_gzip_and_smaller():
    rows = [{"id": "a%d" % i, "title": "Repeated Title", "artist": "Act"}
            for i in range(400)]
    raw = bp.serialize(rows)
    packed = bp.compress(raw)
    assert gzip.decompress(packed) == raw
    assert len(packed) < len(raw) / 4


def test_empty_answer_serializes_and_round_trips():
    raw = bp.serialize([])
    assert raw == b"[]"
    assert bp.decompress(bp.compress(raw)) == raw


# ── the logic version must be enforced, not remembered ──────────────────────

def test_derived_output_is_pinned_against_the_logic_version():
    """Discipline, not mechanism.

    ``test_logic_version_participates`` proves the constant reaches the
    signature; it passes forever whether or not anyone bumps it. This pins the
    serialized output of a fixed fixture, so changing serialization without
    bumping the version fails HERE with an instruction.

    If this fails and the change to derived output was intentional: bump
    ``LOGIC_VERSION`` in app/browse_prepared.py and re-pin the digest below.
    Forgetting leaves every running instance serving the pre-change answer
    under a matching validator -- the exact shape of three fixes that once
    shipped with no observable effect.
    """
    fixture = [
        {"id": "al1", "title": "Résumé", "artist": "Act", "year": 2001,
         "thumb": None, "subtype": "album", "track_count": 9, "sources": None},
        {"id": "al2", "title": "Second", "artist": "Act", "year": None,
         "thumb": "t", "subtype": None, "track_count": None,
         "sources": [{"server_name": "Plex", "album_id": "k", "source_type": ""}]},
    ]
    assert (bp.LOGIC_VERSION, bp.digest_values(bp.serialize(fixture).decode())) == (
        1,
        "b3066f505a28cdd6",
    ), ("derived output or logic version changed -- if intentional, bump "
        "LOGIC_VERSION and re-pin this digest")

"""Unit tests for the pure unknown-count resolver (app/album_fold.py, #61)."""

import dataclasses

from app.album_fold import normalized_subtype, resolved_track_counts
from app.models import Album


def _d(identity, subtype, count):
    return {"id": identity, "sub": subtype, "n": count}


def _resolve(items):
    return resolved_track_counts(
        items,
        identity=lambda i: i["id"],
        subtype=lambda i: i["sub"],
        count=lambda i: i["n"],
    )


# ── the rule ──────────────────────────────────────────────────────────────────

def test_unknown_adopts_the_only_known_count():
    assert _resolve([_d("kid a", "album", 11), _d("kid a", "album", None)]) == [11, 11]


def test_every_unknown_in_the_group_adopts_it_not_just_the_first():
    got = _resolve([_d("x", "album", 9), _d("x", "album", None),
                    _d("x", "album", None), _d("x", "album", None)])
    assert got == [9, 9, 9, 9]


def test_competing_known_counts_block_the_fold():
    """The asymmetry that keeps this safe: no guessing between editions."""
    got = _resolve([_d("pg", "album", 8), _d("pg", "album", 10),
                    _d("pg", "album", None)])
    assert got == [8, 10, None]


def test_known_counts_are_never_rewritten():
    """Unknown folds into known; known never folds into known."""
    got = _resolve([_d("x", "album", 12), _d("x", "album", 14)])
    assert got == [12, 14]


def test_all_unknown_stays_all_unknown():
    """Nothing to fold into — these already grouped together downstream."""
    assert _resolve([_d("x", "album", None), _d("x", "album", None)]) == [None, None]


def test_a_lone_item_is_untouched():
    assert _resolve([_d("x", "album", None)]) == [None]
    assert _resolve([_d("x", "album", 7)]) == [7]


def test_resolution_does_not_cross_identities():
    got = _resolve([_d("a", "album", 5), _d("b", "album", None)])
    assert got == [5, None]


def test_resolution_does_not_cross_subtypes():
    """A Single must not adopt an Album's length (#16's regression)."""
    got = _resolve([_d("x", "album", 12), _d("x", "single", None)])
    assert got == [12, None]


def test_missing_subtype_reads_as_album():
    """None normalizes to 'album' so an inconsistent tag on one source never
    splits one genuine release."""
    got = _resolve([_d("x", "album", 12), _d("x", None, None)])
    assert got == [12, 12]


def test_subtype_normalization_is_case_and_space_insensitive():
    assert normalized_subtype(None) == "album"
    assert normalized_subtype("") == "album"
    assert normalized_subtype("  Single ") == "single"
    got = _resolve([_d("x", "EP", 4), _d("x", "ep", None)])
    assert got == [4, 4]


def test_zero_is_a_known_count_not_an_absent_one():
    """A real empty album must not be treated as unknown — 0 is falsy."""
    got = _resolve([_d("x", "album", 0), _d("x", "album", None)])
    assert got == [0, 0]


def test_result_is_positionally_aligned_with_the_input():
    items = [_d("a", "album", None), _d("b", "album", 3),
             _d("a", "album", 7), _d("b", "album", None)]
    assert _resolve(items) == [7, 3, 7, 3]


def test_empty_input():
    assert _resolve([]) == []


# ── purity: the inputs are cached objects ─────────────────────────────────────

def test_inputs_are_never_mutated():
    """The albums reaching the native caller come from the Plex client cache.
    Writing a resolved count onto one would make an INFERRED value
    indistinguishable from a reported one on every later read — the same trap as
    persisting it, reached by a different route."""
    cached = [Album(id="A:1", title="Kid A", artist="Radiohead", track_count=11),
              Album(id="B:1", title="Kid A", artist="Radiohead", track_count=None)]
    before = [dataclasses.replace(a) for a in cached]

    out = resolved_track_counts(
        cached,
        identity=lambda a: (a.title, a.artist),
        subtype=lambda a: a.subtype,
        count=lambda a: a.track_count,
    )

    assert out == [11, 11]
    assert cached == before, "the cached Album objects were mutated"
    assert cached[1].track_count is None


def test_resolving_twice_is_stable():
    """A second pass over the same cached objects must see the same inputs — no
    resolved count was left behind on them by the first."""
    cached = [Album(id="A:1", title="X", artist="Y", track_count=6),
              Album(id="B:1", title="X", artist="Y", track_count=None)]
    kw = dict(identity=lambda a: (a.title, a.artist),
              subtype=lambda a: a.subtype, count=lambda a: a.track_count)
    assert resolved_track_counts(cached, **kw) == resolved_track_counts(cached, **kw)


def test_dataclass_and_dict_shapes_agree():
    """Accessor-parameterised so the three callers can share one implementation
    rather than keeping three near-copies of a subtle predicate in step."""
    dicts = [_d(("X", "Y"), "album", 6), _d(("X", "Y"), "album", None)]
    albums = [Album(id="A:1", title="X", artist="Y", track_count=6),
              Album(id="B:1", title="X", artist="Y", track_count=None)]
    from_dicts = _resolve(dicts)
    from_albums = resolved_track_counts(
        albums,
        identity=lambda a: (a.title, a.artist),
        subtype=lambda a: a.subtype,
        count=lambda a: a.track_count,
    )
    assert from_dicts == from_albums == [6, 6]

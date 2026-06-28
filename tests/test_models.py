"""U1: source-neutral data models live in app.models; app.plex.models re-exports.

Characterization: the relocation must not change the dataclasses' shape or
behavior, and the back-compat shim must expose the *same* class objects so
existing `from app.plex.models import ...` call sites are unaffected.
"""

import app.models as m
import app.plex.models as pm


def test_app_models_exports_all_five():
    for name in ("Track", "Album", "Artist", "Library", "SearchResults"):
        assert hasattr(m, name), f"app.models missing {name}"


def test_plex_models_shim_reexports_identical_objects():
    # Identity, not just equality: importers via the shim must get the same
    # class object as app.models, or isinstance checks across the codebase break.
    assert pm.Track is m.Track
    assert pm.Album is m.Album
    assert pm.Artist is m.Artist
    assert pm.Library is m.Library
    assert pm.SearchResults is m.SearchResults


def test_track_carries_all_fields_with_defaults():
    t = m.Track(id="1:42", title="Song", artist="Act", album="Rec", duration_ms=210000)
    assert t.id == "1:42"
    assert t.duration_ms == 210000
    # defaults preserved verbatim from the pre-move dataclass
    assert t.genre is None
    assert t.stream_key == ""
    assert t.server_name == ""
    assert t.album_artist is None
    assert t.album_id is None
    assert t.disc_number == 1
    assert t.track_number is None


def test_album_and_artist_optional_fields():
    a = m.Album(id="a1", title="Rec", artist="Act")
    assert a.year is None and a.subtype is None and a.sources is None
    ar = m.Artist(id="ar1", title="Act")
    assert ar.thumb is None and ar.release_count is None


def test_searchresults_defaults_are_independent_lists():
    r1 = m.SearchResults()
    r2 = m.SearchResults()
    assert r1.tracks == [] and r1.albums == [] and r1.artists == []
    r1.tracks.append(m.Track(id="x", title="t", artist="a", album="al", duration_ms=1))
    assert r2.tracks == [], "default_factory must not share list state across instances"

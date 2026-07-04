"""U11: LocalSource — directory scan + tag read into the source-neutral models.

The tag read is the only I/O boundary, so it is injected: every structural test
(walk, grouping, id namespacing, ms durations, match-id scoping, graceful gaps,
art selection, corrupt-skip, containment, search) runs against a REAL temp file
tree with a fake reader — fast and deterministic. Two tests exercise the real
``read_tags`` mutagen path against a generated WAV (real duration + real ID3
frames, incl. MusicBrainz UFID/TXXX) and a non-audio file, so the mutagen
integration itself is proven.

match_ids use the SAME lowercased ``musicbrainz*`` scheme names U10 (Jellyfin)
emits, so a Jellyfin track and a local copy of the same recording ID-merge in the
catalog. Track entities carry the recording id only (never the album mbid) — the
U10 false-merge guard.
"""

import os

import pytest

from app.sources.base import Capabilities, StreamTarget
from app.sources.local import LocalSource, LocalTags, read_tags

SID = "loc"
LIB = f"{SID}:lib"


def _make_reader(root, mapping):
    """Fake reader: maps a file's posix relpath → LocalTags (or None = unreadable)."""
    def reader(abspath):
        rel = os.path.relpath(abspath, root).replace(os.sep, "/")
        return mapping.get(rel)
    return reader


def _build(tmp_path, audio, images=()):
    """Create an on-disk tree: ``audio`` maps relpath→LocalTags|None (None makes a
    real file the reader rejects); ``images`` are relpaths written as image bytes.
    Returns a LocalSource over the tree wired to the matching fake reader."""
    for rel in list(audio) + list(images):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"IMGDATA" if rel in images else b"")
    return LocalSource(
        root_dir=str(tmp_path), source_id=SID, server_name="Home",
        reader=_make_reader(str(tmp_path), audio),
    )


def _albums_by_title(albs):
    return {a.title: a for a in albs}


# ── identity + capabilities ──────────────────────────────────────────────────

async def test_identity_and_capabilities(tmp_path):
    s = _build(tmp_path, {})
    assert s.source_id == SID
    assert s.source_type == "local"
    caps = s.capabilities
    assert isinstance(caps, Capabilities)
    assert caps.genres is True
    assert not (caps.native_search or caps.similarity or caps.popular or caps.styles)


async def test_get_libraries_is_single_rooted_music_lib(tmp_path):
    s = _build(tmp_path, {"a.flac": LocalTags(title="A", album="Alb", album_artist="Art")})
    libs = await s.get_libraries()
    assert [lib.key for lib in libs] == [LIB]
    assert libs[0].type == "artist"
    assert libs[0].server_name == "Home"


# ── structural parse: artists → albums → tracks ──────────────────────────────

async def test_artists_albums_tracks_structure(tmp_path):
    audio = {
        "Art1/Alb1/01.flac": LocalTags(
            title="One", artist="Art1", album="Alb1", album_artist="Art1",
            duration_ms=180000, genre="Rock", year=2001,
            disc_number=1, track_number=1),
        "Art1/Alb1/02.flac": LocalTags(
            title="Two", artist="Art1", album="Alb1", album_artist="Art1",
            duration_ms=200000, genre="Rock", year=2001,
            disc_number=1, track_number=2),
        "Art2/AlbX/01.flac": LocalTags(
            title="Solo", artist="Art2", album="AlbX", album_artist="Art2",
            duration_ms=123000, year=1999, track_number=1),
    }
    s = _build(tmp_path, audio)

    artists = await s.get_artists(LIB)
    assert {a.title for a in artists} == {"Art1", "Art2"}
    assert all(a.id.startswith(f"{SID}:") for a in artists)

    albums = await s.get_albums(LIB)
    by = _albums_by_title(albums)
    assert set(by) == {"Alb1", "AlbX"}
    assert by["Alb1"].artist == "Art1"
    assert by["Alb1"].year == 2001
    assert by["Alb1"].track_count == 2

    tracks = await s.get_tracks(LIB, album_id=by["Alb1"].id)
    assert [t.title for t in tracks] == ["One", "Two"]   # disc/track ordered
    t1 = tracks[0]
    assert t1.stream_key == f"{SID}:Art1/Alb1/01.flac"   # relpath-bearing stream key
    assert t1.id != t1.stream_key                        # identity is a safe hash, not the path
    assert t1.duration_ms == 180000
    assert t1.album_id == by["Alb1"].id
    assert t1.server_name == "Home"


async def test_identities_are_id_safe(tmp_path):
    # ce-debug 2026-06-29 (Bug B): catalog identities flow through validate_plex_id
    # (enqueue/ratings/tags) and URL path segments, so they MUST be _ID_RE-safe
    # (no '/', '.', spaces; single colon; <=128 chars). A raw relpath identity 400s
    # on enqueue ("Failed to add track"). The relpath lives on stream_key instead.
    from app.api.guest import _ID_RE
    audio = {"Art Name/Alb Name/01 - A Song.flac":
             LocalTags(title="A Song", artist="Art Name", album="Alb Name", album_artist="Art Name")}
    s = _build(tmp_path, audio)
    [t] = await s.get_tracks(LIB)
    [alb] = await s.get_albums(LIB)
    [artist] = await s.get_artists(LIB)
    for ident in (t.id, t.album_id, alb.id, artist.id):
        assert _ID_RE.match(ident), f"identity {ident!r} is not _ID_RE-safe"
        assert len(ident) <= 128
    # the path (with '/', '.', spaces) rides stream_key, not the identity
    assert t.stream_key == f"{SID}:Art Name/Alb Name/01 - A Song.flac"
    assert t.id != t.stream_key
    assert t.album_id == alb.id   # track→album linkage intact under the new scheme


async def test_get_albums_filtered_by_artist(tmp_path):
    audio = {
        "A/AlbA/1.flac": LocalTags(title="x", album="AlbA", album_artist="A"),
        "B/AlbB/1.flac": LocalTags(title="y", album="AlbB", album_artist="B"),
    }
    s = _build(tmp_path, audio)
    artists = {a.title: a for a in await s.get_artists(LIB)}
    albs = await s.get_albums(LIB, artist_id=artists["A"].id)
    assert [a.title for a in albs] == ["AlbA"]


# ── match-id scoping (cross-source merge parity with Jellyfin) ────────────────

async def test_match_ids_use_jellyfin_scheme_and_scope(tmp_path):
    audio = {
        "A/Alb/1.flac": LocalTags(
            title="t", artist="A", album="Alb", album_artist="A",
            mb_recording="rec-1", mb_album="alb-1", mb_release_group="rg-1",
            mb_artist="art-1"),
    }
    s = _build(tmp_path, audio)

    [track] = await s.get_tracks(LIB)
    # track carries the RECORDING id only — never the album mbid (false-merge guard)
    assert track.match_ids == {"musicbrainztrack": "rec-1", "musicbrainzrecording": "rec-1"}

    [alb] = await s.get_albums(LIB)
    assert alb.match_ids == {"musicbrainzalbum": "alb-1", "musicbrainzreleasegroup": "rg-1"}

    [artist] = await s.get_artists(LIB)
    assert artist.match_ids == {"musicbrainzartist": "art-1"}


# ── graceful metadata gaps ───────────────────────────────────────────────────

async def test_missing_album_artist_and_genre_still_ingests(tmp_path):
    audio = {
        # no album_artist, no genre, no track number, no year
        "loose/song.mp3": LocalTags(title="Lonely", artist="Singer", album="Demos"),
    }
    s = _build(tmp_path, audio)
    [t] = await s.get_tracks(LIB)
    assert t.title == "Lonely"
    assert t.artist == "Singer"
    assert t.genre is None
    # album-artist falls back to the track artist for grouping
    albs = await s.get_albums(LIB)
    assert albs[0].artist == "Singer"


async def test_missing_title_falls_back_to_filename(tmp_path):
    audio = {"x/03 - untitled.flac": LocalTags(title="", artist="A", album="Alb", album_artist="A")}
    s = _build(tmp_path, audio)
    [t] = await s.get_tracks(LIB)
    assert t.title == "03 - untitled"


# ── art selection: embedded preferred, folder fallback, else none ────────────

async def test_art_prefers_embedded_then_folder_then_none(tmp_path):
    audio = {
        "withEmbedded/song.flac": LocalTags(
            title="E", album="EmbAlb", album_artist="A", has_embedded_art=True),
        "withFolder/song.flac": LocalTags(
            title="F", album="FolAlb", album_artist="A", has_embedded_art=False),
        "bare/song.flac": LocalTags(
            title="N", album="BareAlb", album_artist="A", has_embedded_art=False),
    }
    s = _build(tmp_path, audio, images=["withFolder/cover.jpg"])
    by = {a.title: a for a in await s.get_albums(LIB)}
    # embedded → thumb points at the audio file itself
    assert by["EmbAlb"].thumb == f"{SID}:withEmbedded/song.flac"
    # folder image → thumb points at the cover file
    assert by["FolAlb"].thumb == f"{SID}:withFolder/cover.jpg"
    # neither → no thumb
    assert by["BareAlb"].thumb is None


# ── corrupt / unreadable files are skipped, scan survives ─────────────────────

async def test_unreadable_file_skipped_scan_survives(tmp_path):
    audio = {
        "good/1.flac": LocalTags(title="Good", album="Alb", album_artist="A"),
        "bad/2.flac": None,  # reader returns None → skipped
    }
    s = _build(tmp_path, audio)
    tracks = await s.get_tracks(LIB)
    assert [t.title for t in tracks] == ["Good"]


# ── single-item lookups ──────────────────────────────────────────────────────

async def test_get_track_and_album_by_id(tmp_path):
    audio = {"A/Alb/1.flac": LocalTags(title="Solo", album="Alb", album_artist="A", duration_ms=99000)}
    s = _build(tmp_path, audio)
    [t0] = await s.get_tracks(LIB)
    t = await s.get_track(t0.id)
    assert t.title == "Solo" and t.duration_ms == 99000
    [alb] = await s.get_albums(LIB)
    a = await s.get_album(alb.id)
    assert a.title == "Alb"


async def test_get_track_missing_raises(tmp_path):
    s = _build(tmp_path, {"A/Alb/1.flac": LocalTags(title="x", album="Alb", album_artist="A")})
    with pytest.raises(KeyError):
        await s.get_track(f"{SID}:nope/missing.flac")


# ── search (substring; native_search is False so this is the local floor) ─────

async def test_search_matches_title_album_artist(tmp_path):
    audio = {
        "Beatles/Revolver/1.flac": LocalTags(
            title="Taxman", artist="The Beatles", album="Revolver", album_artist="The Beatles"),
    }
    s = _build(tmp_path, audio)
    res = await s.search(LIB, "taxman")
    assert [t.title for t in res.tracks] == ["Taxman"]
    res2 = await s.search(LIB, "revolver")
    assert [a.title for a in res2.albums] == ["Revolver"]
    res3 = await s.search(LIB, "beatles")
    assert [a.title for a in res3.artists] == ["The Beatles"]


# ── genres ───────────────────────────────────────────────────────────────────

async def test_genres_collected_from_tags(tmp_path):
    audio = {
        "A/Alb/1.flac": LocalTags(title="a", album="Alb", album_artist="A", genre="Jazz"),
        "A/Alb/2.flac": LocalTags(title="b", album="Alb", album_artist="A", genre="Rock"),
        "A/Alb/3.flac": LocalTags(title="c", album="Alb", album_artist="A", genre="Jazz"),
    }
    s = _build(tmp_path, audio)
    assert sorted(await s.get_genres(LIB)) == ["Jazz", "Rock"]


# ── stream resolution + basic containment (full traversal suite in U12) ───────

async def test_resolve_stream_returns_contained_path(tmp_path):
    s = _build(tmp_path, {"A/Alb/1.flac": LocalTags(title="x", album="Alb", album_artist="A")})
    target = s.resolve_stream(f"{SID}:A/Alb/1.flac")
    assert isinstance(target, StreamTarget)
    assert target.url is None
    assert target.path == os.path.realpath(str(tmp_path / "A" / "Alb" / "1.flac"))


async def test_resolve_stream_rejects_escape(tmp_path):
    s = _build(tmp_path, {"A/1.flac": LocalTags(title="x", album="Alb", album_artist="A")})
    target = s.resolve_stream(f"{SID}:../../etc/passwd")
    assert target.path is None and target.url is None


async def test_fetch_art_rejects_traversal(tmp_path):
    # U12/R23: a crafted art key escaping the root is rejected with no file read.
    s = _build(tmp_path, {"A/1.flac": LocalTags(title="x", album="Alb", album_artist="A")})
    (tmp_path.parent / "outside_secret.jpg").write_bytes(b"SECRET")
    with pytest.raises(FileNotFoundError):
        await s.fetch_art(f"{SID}:../outside_secret.jpg")


async def test_symlink_inside_root_allowed_outside_rejected(tmp_path):
    # U12/R23: realpath containment — a symlink pointing inside the root is fine;
    # one escaping the root is rejected (symlink-escape, not just textual "..").
    root = tmp_path / "root"
    root.mkdir()
    (root / "real.flac").write_bytes(b"")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.flac").write_bytes(b"SECRET")
    try:
        os.symlink(root / "real.flac", root / "alias.flac")
        os.symlink(outside / "secret.flac", root / "escape.flac")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")
    s = LocalSource(root_dir=str(root), source_id=SID)
    assert s.resolve_stream(f"{SID}:alias.flac").path is not None     # in-root target
    assert s.resolve_stream(f"{SID}:escape.flac").path is None        # escapes root


# ── fetch_art (folder image, real bytes) ─────────────────────────────────────

async def test_fetch_art_reads_folder_image(tmp_path):
    s = _build(tmp_path, {"A/Alb/1.flac": LocalTags(title="x", album="Alb", album_artist="A")},
               images=["A/Alb/cover.jpg"])
    data, ctype = await s.fetch_art(f"{SID}:A/Alb/cover.jpg")
    assert data == b"IMGDATA"
    assert ctype in ("image/jpeg", "image/jpg")


# ── enrichments degrade ──────────────────────────────────────────────────────

async def test_unsupported_enrichments_degrade_to_empty(tmp_path):
    s = _build(tmp_path, {})
    assert await s.get_styles_with_counts(LIB) == []
    assert await s.get_sonic_nearest(f"{SID}:x") == []
    assert await s.get_artist_similar_names(f"{SID}:x") == []
    assert await s.get_artist_popular_tracks(f"{SID}:x") == []


# ── REAL mutagen read path (generated WAV with real ID3 frames) ──────────────

def _write_wav_with_id3(path, *, seconds=0.2):
    import wave
    from mutagen.wave import WAVE
    from mutagen.id3 import TIT2, TPE1, TPE2, TALB, TDRC, TCON, TRCK, TPOS, UFID, TXXX

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))

    wav = WAVE(str(path))
    if wav.tags is None:
        wav.add_tags()
    t = wav.tags
    t.add(TIT2(encoding=3, text=["Real Title"]))
    t.add(TPE1(encoding=3, text=["Real Artist"]))
    t.add(TPE2(encoding=3, text=["Real AlbumArtist"]))
    t.add(TALB(encoding=3, text=["Real Album"]))
    t.add(TDRC(encoding=3, text=["2014"]))
    t.add(TCON(encoding=3, text=["Electronic"]))
    t.add(TRCK(encoding=3, text=["5/12"]))
    t.add(TPOS(encoding=3, text=["1/2"]))
    t.add(UFID(owner="http://musicbrainz.org", data=b"rec-real"))
    t.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=["alb-real"]))
    t.add(TXXX(encoding=3, desc="MusicBrainz Release Group Id", text=["rg-real"]))
    t.add(TXXX(encoding=3, desc="MusicBrainz Album Artist Id", text=["art-real"]))
    wav.save()


def test_read_tags_parses_real_wav(tmp_path):
    p = tmp_path / "song.wav"
    _write_wav_with_id3(p, seconds=0.25)
    tags = read_tags(str(p))
    assert tags is not None
    assert tags.title == "Real Title"
    assert tags.artist == "Real Artist"
    assert tags.album_artist == "Real AlbumArtist"
    assert tags.album == "Real Album"
    assert tags.year == 2014
    assert tags.genre == "Electronic"
    assert tags.track_number == 5
    assert tags.disc_number == 1
    assert tags.duration_ms == pytest.approx(250, abs=20)
    assert tags.mb_recording == "rec-real"
    assert tags.mb_album == "alb-real"
    assert tags.mb_release_group == "rg-real"
    assert tags.mb_artist == "art-real"


def test_read_tags_on_non_audio_returns_none(tmp_path):
    p = tmp_path / "notaudio.flac"
    p.write_bytes(b"this is not a real audio stream")
    assert read_tags(str(p)) is None


async def test_read_tags_integrates_with_default_reader_scan(tmp_path):
    # End-to-end with the REAL default reader (no fake): a WAV tree scans into the
    # provider models with a real ms duration and Jellyfin-scheme match_ids.
    (tmp_path / "Real AlbumArtist" / "Real Album").mkdir(parents=True)
    _write_wav_with_id3(tmp_path / "Real AlbumArtist" / "Real Album" / "05.wav")
    s = LocalSource(root_dir=str(tmp_path), source_id=SID, server_name="Home")
    [t] = await s.get_tracks(LIB)
    assert t.title == "Real Title"
    assert t.duration_ms > 0
    assert t.match_ids == {"musicbrainztrack": "rec-real", "musicbrainzrecording": "rec-real"}
    [alb] = await s.get_albums(LIB)
    assert alb.match_ids == {"musicbrainzalbum": "alb-real", "musicbrainzreleasegroup": "rg-real"}

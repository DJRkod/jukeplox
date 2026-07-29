"""Local-files provider — a ``MusicSource`` over a directory of tagged audio
files (plan U11).

A local source is a configured root directory. The scan walks it recursively,
reads each audio file's tags with ``mutagen`` (broad-format, pure-Python, no
native deps), and builds the source-neutral models. Durations come from the
decoder (``info.length`` seconds → ms). Cover art resolves from an embedded
picture first, then a folder image (``cover.jpg`` etc.), else nothing.

The tag read is the ONLY I/O boundary that touches mutagen, so it is injected:
``LocalSource(reader=...)`` defaults to :func:`read_tags` but tests pass a fake
reader, exercising every bit of walk/group/id/art logic against a real temp tree
without synthesizing audio. This mirrors the catalog's pure/I-O split.

Identity for cross-source dedup (R9/R10): MusicBrainz tags map to the SAME
lowercased ``musicbrainz*`` scheme names U10 (Jellyfin) emits, so a Jellyfin
track and a local copy of the same recording ID-merge in the catalog. As in U10,
a track entity carries only the recording id — never the album mbid — so two
tracks of one album never false-merge (plan R10: prefer a visible duplicate over
a false merge).

Keys follow the registry's ``{source_id}:{native}`` namespace (U3). The native
part for a track/art file is its **root-relative posix path**; the registry
splits on the first ``:`` so any further colons in a filename stay intact.
``resolve_stream`` / ``fetch_art`` realpath the join and confirm it stays under
the root before any open (the containment that U12 hardens + tests).

Capabilities: genres only (when tagged). No native search, sonic similarity,
popular tracks, or Plex "styles" — those degrade to the base-class empties.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

from app.models import Album, Artist, Library, SearchResults, Track
from app.sources.base import Capabilities, MusicSource, StreamTarget

_LOCAL_CAPS = Capabilities(genres=True)

AUDIO_EXTS = {
    ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus",
    ".wav", ".wma", ".aiff", ".aif", ".alac", ".ape",
}
_IMAGE_EXTS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
# Folder-art filename preference (stem, lowercased) then extension preference.
_ART_STEMS = ["cover", "folder", "front", "album", "albumart", "thumb"]
_ART_EXT_ORDER = [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]
# Unit separator joining album-artist + title into an album grouping key. A
# module constant (not an inline literal) so the f-string that builds the id
# carries no backslash escape — that is a SyntaxError under Python 3.11.
_SEP = "\x1f"


@dataclass
class LocalTags:
    """Normalized tags for one audio file — the reader's return shape.

    MusicBrainz ids are split by entity so the provider can scope them onto the
    right model (recording → track; album/release-group → album; artist → artist),
    matching U10's false-merge guard."""

    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    duration_ms: int = 0
    genre: str | None = None
    year: int | None = None
    disc_number: int = 1
    track_number: int | None = None
    has_embedded_art: bool = False
    mb_recording: str | None = None
    mb_album: str | None = None
    mb_release_group: str | None = None
    mb_artist: str | None = None


# ── the mutagen reader (the one I/O boundary; lazy import keeps it injectable) ──

def _year(value) -> int | None:
    if not value:
        return None
    m = re.search(r"\d{4}", str(value))
    return int(m.group()) if m else None


def _num(value) -> int | None:
    """First integer of a ``"n"`` or ``"n/total"`` tag."""
    if value is None:
        return None
    head = str(value).split("/")[0].strip()
    return int(head) if head.isdigit() else None


def read_tags(path: str) -> LocalTags | None:
    """Read one audio file's tags + duration via mutagen. ``None`` when the file
    is not decodable audio (corrupt, or a non-audio file) — the scan skips it."""
    try:
        import mutagen
        audio = mutagen.File(path)
    except Exception:
        return None
    if audio is None:
        return None

    info = getattr(audio, "info", None)
    duration_ms = int(round(float(getattr(info, "length", 0) or 0) * 1000))
    tags = audio.tags
    out = _extract(audio, tags)
    out.duration_ms = duration_ms
    return out


def _extract(audio, tags) -> LocalTags:
    if tags is None:
        return LocalTags()
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4Tags
    if isinstance(tags, ID3):
        return _from_id3(tags)
    if isinstance(tags, MP4Tags):
        return _from_mp4(tags)
    return _from_vorbis(audio, tags)


def _from_id3(tags) -> LocalTags:
    def g(fid):
        fr = tags.get(fid)
        if fr is not None and getattr(fr, "text", None):
            return str(fr.text[0])
        return None

    mb_recording = None
    for fr in tags.getall("UFID"):
        if getattr(fr, "owner", "") == "http://musicbrainz.org":
            try:
                mb_recording = (fr.data.decode("ascii", "ignore").strip() or None)
            except Exception:
                pass

    def txxx(desc):
        for fr in tags.getall("TXXX"):
            if fr.desc.lower() == desc.lower() and fr.text:
                return str(fr.text[0])
        return None

    return LocalTags(
        title=g("TIT2") or "",
        artist=g("TPE1") or "",
        album=g("TALB") or "",
        album_artist=g("TPE2") or "",
        genre=g("TCON"),
        year=_year(g("TDRC") or g("TYER")),
        disc_number=_num(g("TPOS")) or 1,
        track_number=_num(g("TRCK")),
        has_embedded_art=bool(tags.getall("APIC")),
        mb_recording=mb_recording,
        mb_album=txxx("MusicBrainz Album Id"),
        mb_release_group=txxx("MusicBrainz Release Group Id"),
        mb_artist=txxx("MusicBrainz Album Artist Id") or txxx("MusicBrainz Artist Id"),
    )


def _from_vorbis(audio, tags) -> LocalTags:
    def first(k):
        v = tags.get(k)
        return str(v[0]) if v else None

    has_art = bool(getattr(audio, "pictures", None)) or ("metadata_block_picture" in tags)
    return LocalTags(
        title=first("title") or "",
        artist=first("artist") or "",
        album=first("album") or "",
        album_artist=first("albumartist") or first("album artist") or "",
        genre=first("genre"),
        year=_year(first("date") or first("year") or first("originaldate")),
        disc_number=_num(first("discnumber")) or 1,
        track_number=_num(first("tracknumber")),
        has_embedded_art=has_art,
        mb_recording=first("musicbrainz_trackid") or first("musicbrainz_recordingid"),
        mb_album=first("musicbrainz_albumid"),
        mb_release_group=first("musicbrainz_releasegroupid"),
        mb_artist=first("musicbrainz_albumartistid") or first("musicbrainz_artistid"),
    )


def _from_mp4(tags) -> LocalTags:
    def first(k):
        v = tags.get(k)
        return str(v[0]) if v else None

    def freeform(name):
        v = tags.get(f"----:com.apple.iTunes:{name}")
        if v:
            val = v[0]
            return val.decode("utf-8", "ignore") if isinstance(val, bytes) else str(val)
        return None

    trk = tags.get("trkn")
    dsk = tags.get("disk")
    return LocalTags(
        title=first("\xa9nam") or "",
        artist=first("\xa9ART") or "",
        album=first("\xa9alb") or "",
        album_artist=first("aART") or "",
        genre=first("\xa9gen"),
        year=_year(first("\xa9day")),
        disc_number=(dsk[0][0] if dsk and dsk[0] else 1) or 1,
        track_number=(trk[0][0] if trk and trk[0] else None),
        has_embedded_art=bool(tags.get("covr")),
        mb_recording=freeform("MusicBrainz Track Id"),
        mb_album=freeform("MusicBrainz Album Id"),
        mb_release_group=freeform("MusicBrainz Release Group Id"),
        mb_artist=freeform("MusicBrainz Album Artist Id") or freeform("MusicBrainz Artist Id"),
    )


def _short(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


class LocalSource(MusicSource):
    def __init__(
        self,
        root_dir: str,
        source_id: str = "local",
        server_name: str = "",
        reader=None,
    ) -> None:
        self._root = root_dir
        self._root_real = os.path.realpath(root_dir)
        self._source_id = source_id or "local"
        self.server_name = server_name
        self._reader = reader or read_tags
        self._index: dict | None = None
        self._dir_art: dict[str, str | None] = {}

    # ── identity ──────────────────────────────────────────────────────────────

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def source_type(self) -> str:
        return "local"

    @property
    def capabilities(self) -> Capabilities:
        return _LOCAL_CAPS

    # ── key namespace ─────────────────────────────────────────────────────────

    def _make_id(self, native: str) -> str:
        return f"{self._source_id}:{native}"

    def _strip(self, key: str | None) -> str | None:
        prefix = f"{self._source_id}:"
        if key and key.startswith(prefix):
            return key[len(prefix):]
        return key

    def _rel(self, abspath: str) -> str:
        """Root-relative posix key for a file under the root."""
        return os.path.relpath(abspath, self._root).replace(os.sep, "/")

    def _contained(self, relpath: str) -> str | None:
        """Realpath ``relpath`` under the root; ``None`` if it escapes (traversal,
        symlink-out). The single gate every filesystem open passes through."""
        candidate = os.path.realpath(os.path.join(self._root, relpath))
        root = self._root_real
        if candidate == root or candidate.startswith(root + os.sep):
            return candidate
        return None

    # ── art helpers ───────────────────────────────────────────────────────────

    def _folder_art(self, dir_abspath: str) -> str | None:
        """Best folder image in a directory (abspath), cached per directory."""
        if dir_abspath in self._dir_art:
            return self._dir_art[dir_abspath]
        best = None
        try:
            entries = os.listdir(dir_abspath)
        except OSError:
            entries = []
        ranked: list[tuple] = []
        for name in entries:
            stem, ext = os.path.splitext(name)
            ext = ext.lower()
            if ext not in _IMAGE_EXTS:
                continue
            stem_rank = _ART_STEMS.index(stem.lower()) if stem.lower() in _ART_STEMS else len(_ART_STEMS)
            ext_rank = _ART_EXT_ORDER.index(ext) if ext in _ART_EXT_ORDER else len(_ART_EXT_ORDER)
            ranked.append((stem_rank, ext_rank, name))
        if ranked:
            ranked.sort()
            best = os.path.join(dir_abspath, ranked[0][2])
        self._dir_art[dir_abspath] = best
        return best

    def _track_thumb(self, abspath: str, has_embedded: bool) -> str | None:
        if has_embedded:
            return self._make_id(self._rel(abspath))
        folder = self._folder_art(os.path.dirname(abspath))
        if folder:
            return self._make_id(self._rel(folder))
        return None

    # ── match-id scoping (Jellyfin scheme names; recording-only on tracks) ─────

    @staticmethod
    def _track_match_ids(t: LocalTags) -> dict[str, str]:
        if not t.mb_recording:
            return {}
        # emit both scheme names U10's _MB_TRACK accepts so a Jellyfin server that
        # labels the recording either way still merges.
        return {"musicbrainztrack": t.mb_recording, "musicbrainzrecording": t.mb_recording}

    @staticmethod
    def _album_match_ids(t: LocalTags) -> dict[str, str]:
        out: dict[str, str] = {}
        if t.mb_album:
            out["musicbrainzalbum"] = t.mb_album
        if t.mb_release_group:
            out["musicbrainzreleasegroup"] = t.mb_release_group
        return out

    @staticmethod
    def _artist_match_ids(t: LocalTags) -> dict[str, str]:
        return {"musicbrainzartist": t.mb_artist} if t.mb_artist else {}

    # ── scan / index ──────────────────────────────────────────────────────────

    def _ensure_index(self) -> dict:
        if self._index is not None:
            return self._index

        artists: dict[str, Artist] = {}
        albums: dict[str, Album] = {}
        album_tracks: dict[str, list[Track]] = {}
        album_artist_of: dict[str, str] = {}        # album_id → artist_id
        artist_albums: dict[str, list[str]] = {}    # artist_id → [album_id]
        tracks: dict[str, Track] = {}
        genres: set[str] = set()

        for dirpath, _dirs, files in os.walk(self._root):
            for name in sorted(files):
                ext = os.path.splitext(name)[1].lower()
                if ext not in AUDIO_EXTS:
                    continue
                abspath = os.path.join(dirpath, name)
                tags = self._reader(abspath)
                if tags is None:
                    continue  # corrupt/unreadable → skip, scan survives

                rel = self._rel(abspath)
                aa_name = tags.album_artist or tags.artist or "Unknown Artist"
                album_title = tags.album or "Unknown Album"
                # IDENTITIES are content hashes, not the file path, so they are
                # _ID_RE / URL-safe (no '/', '.', spaces, single colon, <128 chars):
                # the catalog identity flows through validate_plex_id (enqueue,
                # ratings, tags) and path segments, where a raw relpath 400s/404s
                # (ce-debug 2026-06-29). The relpath stays on stream_key/thumb
                # (query params, resolved cheaply by resolve_stream/fetch_art).
                # Distinct t/a/r prefixes keep the three id spaces from colliding.
                artist_id = self._make_id(f"r{_short(aa_name.lower())}")
                album_key = (aa_name + _SEP + album_title).lower()
                album_id = self._make_id(f"a{_short(album_key)}")
                thumb = self._track_thumb(abspath, tags.has_embedded_art)

                track = Track(
                    id=self._make_id(f"t{_short(rel)}"),
                    title=tags.title or os.path.splitext(name)[0],
                    artist=tags.artist or aa_name,
                    album=album_title,
                    duration_ms=tags.duration_ms,
                    genre=tags.genre,
                    year=tags.year,
                    thumb=thumb,
                    stream_key=self._make_id(rel),
                    server_name=self.server_name,
                    album_artist=tags.album_artist or None,
                    album_id=album_id,
                    disc_number=tags.disc_number,
                    track_number=tags.track_number,
                    match_ids=self._track_match_ids(tags),
                )
                tracks[track.id] = track
                album_tracks.setdefault(album_id, []).append(track)
                if tags.genre:
                    genres.add(tags.genre)

                if album_id not in albums:
                    albums[album_id] = Album(
                        id=album_id, title=album_title, artist=aa_name, year=tags.year,
                        thumb=None, match_ids=self._album_match_ids(tags),
                    )
                    album_artist_of[album_id] = artist_id
                    artist_albums.setdefault(artist_id, [])
                    if album_id not in artist_albums[artist_id]:
                        artist_albums[artist_id].append(album_id)
                else:
                    # fill album-level mbids from the first file that carries them
                    if not albums[album_id].match_ids:
                        albums[album_id].match_ids = self._album_match_ids(tags)

                if artist_id not in artists:
                    artists[artist_id] = Artist(
                        id=artist_id, title=aa_name, thumb=None,
                        match_ids=self._artist_match_ids(tags),
                    )
                elif not artists[artist_id].match_ids:
                    artists[artist_id].match_ids = self._artist_match_ids(tags)

        # order tracks within each album; derive album/artist track_count + thumb
        for album_id, items in album_tracks.items():
            items.sort(key=lambda t: (t.disc_number, t.track_number or 0, t.id))
            alb = albums[album_id]
            alb.track_count = len(items)
            alb.thumb = next((t.thumb for t in items if t.thumb), None)
        for artist_id, alb_ids in artist_albums.items():
            for aid in alb_ids:
                if albums[aid].thumb:
                    artists[artist_id].thumb = albums[aid].thumb
                    break

        self._index = {
            "artists": artists, "albums": albums, "album_tracks": album_tracks,
            "album_artist_of": album_artist_of, "tracks": tracks,
            "genres": sorted(genres),
        }
        return self._index

    # ── core: enumerate ───────────────────────────────────────────────────────

    async def get_libraries(self) -> list[Library]:
        title = self.server_name or os.path.basename(self._root.rstrip("/\\")) or "Local Music"
        return [Library(key=self._make_id("lib"), title=title, type="artist",
                        server_name=self.server_name)]

    async def get_artists(self, section_key: str) -> list[Artist]:
        idx = self._ensure_index()
        return sorted(idx["artists"].values(), key=lambda a: a.title.lower())

    async def get_albums(
        self,
        section_key: str,
        artist_id: str | None = None,
        year: int | None = None,
        style: str | None = None,
    ) -> list[Album]:
        idx = self._ensure_index()
        out = []
        for album_id, alb in idx["albums"].items():
            if artist_id is not None and idx["album_artist_of"].get(album_id) != artist_id:
                continue
            if year is not None and alb.year != year:
                continue
            out.append(alb)
        return sorted(out, key=lambda a: (a.artist.lower(), a.year or 0, a.title.lower()))

    async def get_tracks(
        self,
        section_key: str,
        album_id: str | None = None,
        genre: str | None = None,
        year: int | None = None,
    ) -> list[Track]:
        idx = self._ensure_index()
        if album_id is not None:
            items = list(idx["album_tracks"].get(album_id, []))
        else:
            items = sorted(
                idx["tracks"].values(),
                key=lambda t: (t.artist.lower(), t.album.lower(), t.disc_number,
                               t.track_number or 0, t.id),
            )
        if genre is not None:
            items = [t for t in items if t.genre == genre]
        if year is not None:
            items = [t for t in items if t.year == year]
        return items

    async def get_track(self, track_id: str) -> Track:
        idx = self._ensure_index()
        t = idx["tracks"].get(track_id)
        if t is None:
            raise KeyError(f"Track {track_id} not found")
        return t

    async def get_album(self, album_id: str) -> Album:
        idx = self._ensure_index()
        a = idx["albums"].get(album_id)
        if a is None:
            raise KeyError(f"Album {album_id} not found")
        return a

    # ── core: search (substring; native_search is False, this is the local floor) ─

    async def search(self, section_key: str, query: str) -> SearchResults:
        idx = self._ensure_index()
        q = (query or "").strip().lower()
        if not q:
            return SearchResults()
        tracks = [t for t in idx["tracks"].values() if q in t.title.lower()]
        albums = [a for a in idx["albums"].values() if q in a.title.lower()]
        artists = [a for a in idx["artists"].values() if q in a.title.lower()]
        tracks.sort(key=lambda t: t.title.lower())
        albums.sort(key=lambda a: a.title.lower())
        artists.sort(key=lambda a: a.title.lower())
        return SearchResults(tracks=tracks, albums=albums, artists=artists)

    # ── core: stream + art (containment-checked) ──────────────────────────────

    def resolve_stream(self, stream_key: str) -> StreamTarget:
        relpath = self._strip(stream_key) or stream_key
        contained = self._contained(relpath)
        return StreamTarget(path=contained) if contained else StreamTarget()

    async def fetch_art(self, thumb_path: str, width: int | None = None) -> tuple[bytes, str]:
        relpath = self._strip(thumb_path) or thumb_path
        abspath = self._contained(relpath)
        if not abspath or not os.path.isfile(abspath):
            raise FileNotFoundError(f"Art not found or outside root: {thumb_path}")
        ext = os.path.splitext(abspath)[1].lower()
        if ext in _IMAGE_EXTS:
            with open(abspath, "rb") as fh:
                return fh.read(), _IMAGE_EXTS[ext]
        pic = _embedded_picture(abspath)
        if pic is None:
            raise FileNotFoundError(f"No embedded art in {thumb_path}")
        return pic

    # ── enrichments (genres only) ─────────────────────────────────────────────

    async def get_genres(self, section_key: str) -> list[str]:
        return list(self._ensure_index()["genres"])

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def invalidate_cache(self) -> None:
        self._index = None
        self._dir_art = {}


def _embedded_picture(abspath: str) -> tuple[bytes, str] | None:
    """Extract the first embedded cover picture from an audio file, or ``None``."""
    try:
        import mutagen
        audio = mutagen.File(abspath)
    except Exception:
        return None
    if audio is None:
        return None
    pics = getattr(audio, "pictures", None)  # FLAC
    if pics:
        return pics[0].data, (pics[0].mime or "image/jpeg")
    tags = audio.tags
    if tags is None:
        return None
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4Cover, MP4Tags
    if isinstance(tags, ID3):
        apics = tags.getall("APIC")
        if apics:
            return apics[0].data, (apics[0].mime or "image/jpeg")
    elif isinstance(tags, MP4Tags):
        covr = tags.get("covr")
        if covr:
            mime = "image/png" if covr[0].imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
            return bytes(covr[0]), mime
    else:
        mbp = tags.get("metadata_block_picture")
        if mbp:
            import base64
            from mutagen.flac import Picture
            pic = Picture(base64.b64decode(mbp[0]))
            return pic.data, (pic.mime or "image/jpeg")
    return None

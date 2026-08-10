"""U6: the dedup/merge predicate (pure). Encodes the origin acceptance examples
AE2 (cross-source ID merge) and AE4 (same title, different artist → no false
merge) plus the disambiguator rules — track_count for albums, duration ±2 s and
disc/track for tracks — and the authoritative-distinct-ID guard.
"""

from app.catalog import merge


def _alb(local_key, title="Rec", artist="Act", tc=10, mbid=None):
    return {"match_ids": ({"mbid": mbid} if mbid else {}), "source_id": local_key.split(":")[0],
            "local_key": local_key, "title_base": title.lower(),
            "artist_base_key": artist.lower(), "track_count": tc}


def _trk(local_key, title="Song", artist="Act", dur=180000, disc=1, num=1, mbid=None):
    return {"match_ids": ({"mbid": mbid} if mbid else {}), "source_id": local_key.split(":")[0],
            "local_key": local_key, "title_base": title.lower(),
            "artist_base_key": artist.lower(), "duration_ms": dur,
            "disc_number": disc, "track_number": num}


def _albums(items):
    return merge.group(items, merge.album_coarse, merge.album_same)


def _tracks(items):
    return merge.group(items, merge.track_coarse, merge.track_same)


# ── external key normalization ───────────────────────────────────────────────

def test_external_keys_normalize_and_drop_empty():
    assert merge.external_keys({"MBID": "ABC", "tvdb": " "}) == {"mbid:abc"}
    assert merge.external_keys(None) == set()
    assert merge.external_keys({"k": None}) == set()


def test_external_keys_excludes_release_group():
    # A MusicBrainz release GROUP spans every edition/release of an album, so it is
    # not an identity and must be excluded from dedup keys; the release id
    # (musicbrainzalbum) is edition-specific and stays (ce-debug 2026-06-29).
    assert merge.external_keys(
        {"musicbrainzalbum": "r1", "musicbrainzreleasegroup": "g1"}) == {"musicbrainzalbum:r1"}


# ── release-group must NOT collapse distinct editions (ce-debug 2026-06-29) ───

def test_albums_sharing_only_release_group_do_not_merge():
    # NIN "Further Down The Spiral" US (13 trk) + Japanese (15 trk): different
    # releases that share only a release-group id. They must stay SEPARATE — the
    # release-group is not an identity and must not override the track_count
    # disambiguation. Same shape as Van She's single vs album.
    us = {"match_ids": {"musicbrainzalbum": "rel-us", "musicbrainzreleasegroup": "rg-1"},
          "local_key": "local:us", "title_base": "further down the spiral",
          "artist_base_key": "nine inch nails", "track_count": 13}
    jp = {"match_ids": {"musicbrainzalbum": "rel-jp", "musicbrainzreleasegroup": "rg-1"},
          "local_key": "local:jp", "title_base": "further down the spiral",
          "artist_base_key": "nine inch nails", "track_count": 15}
    assert len(_albums([us, jp])) == 2


def test_albums_same_title_single_vs_album_unknown_count_cross_source_no_merge():
    # ce-debug 2026-08-10 (Van She "Idea of Happiness"): a Single/EP and an Album
    # sharing a title with UNKNOWN track_count on DIFFERENT sources must not merge.
    # The same-source guard can't separate them (distinct sources) and track_count
    # is None on both, so subtype is the only content signal — album_same keys on
    # it. Without this the album (one source) and single (another) fold into one
    # release and the single vanishes.
    album = {"match_ids": {}, "source_id": "S1", "local_key": "S1:album",
             "title_base": "idea of happiness", "artist_base_key": "van she",
             "track_count": None, "subtype": "album"}
    single = {"match_ids": {}, "source_id": "S2", "local_key": "S2:single",
              "title_base": "idea of happiness", "artist_base_key": "van she",
              "track_count": None, "subtype": "single"}
    assert len(_albums([album, single])) == 2


def test_albums_same_release_subtype_none_and_album_still_merge():
    # No-regression: a genuine shared release whose subtype is None on one source
    # and "album" on the other must still fold (None normalizes to "album"), so an
    # inconsistent subtype tag never splits one release into two rows.
    a = {"match_ids": {}, "source_id": "S1", "local_key": "S1:a",
         "title_base": "ok computer", "artist_base_key": "radiohead",
         "track_count": None, "subtype": None}
    b = {"match_ids": {}, "source_id": "S2", "local_key": "S2:b",
         "title_base": "ok computer", "artist_base_key": "radiohead",
         "track_count": None, "subtype": "album"}
    assert len(_albums([a, b])) == 1


def test_albums_sharing_release_id_still_merge():
    # The release id (edition-specific) stays authoritative: the SAME release in two
    # sources still folds to one, even while sharing the release-group id.
    a = {"match_ids": {"musicbrainzalbum": "rel-1", "musicbrainzreleasegroup": "rg-1"},
         "local_key": "plex:1", "title_base": "x", "artist_base_key": "act", "track_count": 13}
    b = {"match_ids": {"musicbrainzalbum": "rel-1", "musicbrainzreleasegroup": "rg-1"},
         "local_key": "jelly:2", "title_base": "x", "artist_base_key": "act", "track_count": 13}
    assert len(_albums([a, b])) == 1


# ── AE2: cross-source ID merge ───────────────────────────────────────────────

def test_ae2_same_mbid_across_sources_merges():
    # Same album in Plex + Jellyfin with matching MusicBrainz IDs → one entry.
    groups = _albums([_alb("plex:1", mbid="m-1"), _alb("jelly:9", mbid="m-1")])
    assert len(groups) == 1
    assert {it["local_key"] for it in groups[0]} == {"plex:1", "jelly:9"}


def test_shared_id_merges_even_when_names_differ():
    # ID is authoritative: a matching id merges regardless of title text.
    groups = _albums([_alb("plex:1", title="Deluxe", mbid="m-1"),
                      _alb("jelly:9", title="Standard", mbid="m-1")])
    assert len(groups) == 1


def test_transitive_id_chain_merges():
    # A↔B by id X, B↔C by id Y → one cluster.
    a = {"match_ids": {"mb": "X"}, "local_key": "s:a", "title_base": "t", "artist_base_key": "ar", "track_count": 1}
    b = {"match_ids": {"mb": "X", "mb2": "Y"}, "local_key": "s:b", "title_base": "t", "artist_base_key": "ar", "track_count": 1}
    c = {"match_ids": {"mb2": "Y"}, "local_key": "s:c", "title_base": "t", "artist_base_key": "ar", "track_count": 1}
    groups = _albums([a, b, c])
    assert len(groups) == 1


# ── AE4 + authoritative-distinct guard ───────────────────────────────────────

def test_ae4_same_title_different_artist_no_merge():
    groups = _albums([_alb("plex:1", title="Greatest Hits", artist="A"),
                      _alb("jelly:2", title="Greatest Hits", artist="B")])
    assert len(groups) == 2


def test_distinct_ids_same_name_not_merged():
    # Both carry IDs but share none → authoritatively distinct despite same name.
    groups = _albums([_alb("plex:1", mbid="m-1"), _alb("jelly:2", mbid="m-2")])
    assert len(groups) == 2


# ── same-source siblings never merge (ce-debug 2026-06-29) ───────────────────
# Two albums from ONE source are distinct entities the source itself separates
# (different rating-keys / file paths). Native _group_albums folds only ACROSS
# servers and emits same-server siblings as separate rows; the catalog merge must
# match — else US/JP editions (or a single + album) both in one local library
# collapse into one release. The merge's whole premise is CROSS-source dedup.

def _alb_src(source_id, local_suffix, tc=13, mbid=None, title="Further Down The Spiral",
             artist="Nine Inch Nails"):
    return {"match_ids": ({"musicbrainzalbum": mbid} if mbid else {}),
            "source_id": source_id, "local_key": f"{source_id}:{local_suffix}",
            "title_base": title.lower(), "artist_base_key": artist.lower(), "track_count": tc}


def test_same_source_same_title_same_count_albums_do_not_merge():
    # Same source, same title+artist, even the SAME track_count → still two distinct
    # releases (US/JP editions can share a count). Without the source guard, Pass 2
    # name+count merges them.
    groups = _albums([_alb_src("local-x", "us", tc=13), _alb_src("local-x", "jp", tc=13)])
    assert len(groups) == 2


def test_same_source_both_unknown_count_albums_do_not_merge():
    # Both counts unknown in ONE source must NOT fold (Van She single + album when
    # the count is missing). Cross-source both-None still folds (test below).
    groups = _albums([_alb_src("local-x", "a", tc=None), _alb_src("local-x", "b", tc=None)])
    assert len(groups) == 2


def test_same_source_shared_id_albums_do_not_merge():
    # Even a shared external id can't merge two items from one source — a source
    # never reports the same release twice under distinct local keys as "one".
    groups = _albums([_alb_src("local-x", "a", mbid="rel-1"),
                      _alb_src("local-x", "b", mbid="rel-1")])
    assert len(groups) == 2


def test_cross_source_same_release_still_folds_with_source_guard():
    # Guard must NOT block legitimate cross-source folding: the same release on two
    # different sources still merges to one (the multi-source raison d'être).
    groups = _albums([_alb_src("plex", "1", tc=13), _alb_src("local-x", "1", tc=13)])
    assert len(groups) == 1
    assert {it["source_id"] for it in groups[0]} == {"plex", "local-x"}


# ── album disambiguator: track_count ─────────────────────────────────────────

def test_same_title_same_artist_same_count_merges():
    groups = _albums([_alb("plex:1", tc=12), _alb("jelly:2", tc=12)])
    assert len(groups) == 1


def test_same_title_count_mismatch_not_merged():
    groups = _albums([_alb("plex:1", tc=12), _alb("jelly:2", tc=15)])
    assert len(groups) == 2


def test_both_unknown_track_count_merge():
    # Parity U1 (AE3): both counts unknown → fold, mirroring the native fold
    # (_group_albums buckets track_count=None together). Loosened from the prior
    # conservative split so connecting a non-Plex source never duplicates albums
    # the native pipeline previously folded.
    groups = _albums([_alb("plex:1", tc=None), _alb("jelly:2", tc=None)])
    assert len(groups) == 1
    assert {it["local_key"] for it in groups[0]} == {"plex:1", "jelly:2"}


def test_known_and_unknown_track_count_not_merged():
    # Parity U1 (AE7): one count known, the other unknown → stays SEPARATE
    # (can't confirm same release), matching native's distinct track_count buckets.
    groups = _albums([_alb("plex:1", tc=12), _alb("jelly:2", tc=None)])
    assert len(groups) == 2


# ── track disambiguators: duration tolerance + disc/track ────────────────────

def test_tracks_within_tolerance_merge():
    groups = _tracks([_trk("plex:1", dur=180000), _trk("jelly:2", dur=181500)])  # 1.5s
    assert len(groups) == 1


def test_tracks_beyond_tolerance_split():
    groups = _tracks([_trk("plex:1", dur=180000), _trk("jelly:2", dur=200000)])  # 20s
    assert len(groups) == 2


def test_tracks_different_track_number_not_merged():
    groups = _tracks([_trk("plex:1", num=1), _trk("jelly:2", num=2)])
    assert len(groups) == 2


def test_tracks_different_disc_not_merged():
    groups = _tracks([_trk("plex:1", disc=1), _trk("jelly:2", disc=2)])
    assert len(groups) == 2


# ── identity ─────────────────────────────────────────────────────────────────

def test_identity_prefers_smallest_external_key():
    cluster = [_alb("plex:1", mbid="m-zzz"), _alb("jelly:9", mbid="m-aaa")]
    assert merge.cluster_identity(cluster, "album") == "mbid:m-aaa"


def test_identity_falls_back_to_smallest_local_key():
    # No external id → identity is the smallest bare source compound id, so a
    # Plex track's identity equals its existing rating key (migration stays inert).
    cluster = [_alb("plex:9"), _alb("jelly:2")]
    assert merge.cluster_identity(cluster, "album") == "jelly:2"


def test_lookup_keys_external_first_then_local():
    cluster = [_alb("plex:9", mbid="m-2"), _alb("jelly:2", mbid="m-1")]
    assert merge.lookup_keys(cluster) == ["mbid:m-1", "mbid:m-2", "jelly:2", "plex:9"]


def test_identity_stable_regardless_of_member_order():
    c1 = [_trk("plex:1", mbid="r-1"), _trk("jelly:2", mbid="r-1")]
    c2 = list(reversed(c1))
    assert merge.cluster_identity(c1, "track") == merge.cluster_identity(c2, "track")

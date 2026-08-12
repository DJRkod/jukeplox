"""Tests for the pure tag matching / dedup-count helpers (app/tag_utils.py).

These are pure functions over a {track_id: [tags]} map or track dicts — no DB
fixture, no event loop, so they live outside test_database.py (whose aiosqlite
session teardown is slow).
"""

from app import tag_utils


def test_invert_tags_groups_case_insensitively():
    idx = tag_utils.invert_tags({"t1": ["Cool", "idm"], "t2": ["cool"], "t3": ["ambient"]})
    assert set(idx.keys()) == {"cool", "idm", "ambient"}
    assert sorted(idx["cool"]["track_ids"]) == ["t1", "t2"]
    assert idx["cool"]["name"] == "Cool"  # first spelling wins as display


def test_invert_tags_ignores_malformed_rows():
    idx = tag_utils.invert_tags({"t1": ["ok"], "t2": "notalist", "t3": [None, "", "  "], "t4": None})
    assert set(idx.keys()) == {"ok"}
    assert idx["ok"]["track_ids"] == ["t1"]


def test_match_tags_whole_token_not_substring():
    all_tags = {"t1": ["cool"], "t2": ["coolant"], "t3": ["cool covers"], "t4": ["cool-down"]}
    names = {e["name"] for e in tag_utils.match_tags("cool", all_tags)}
    assert names == {"cool", "cool covers", "cool-down"}  # 'coolant' excluded (substring, not token)


def test_match_tags_case_insensitive_and_trimmed():
    all_tags = {"t1": ["Late-Night"], "t2": ["late night"]}
    assert {e["name"] for e in tag_utils.match_tags("  LATE  ", all_tags)} == {"Late-Night", "late night"}


def test_match_tags_empty_query_and_empty_map():
    assert tag_utils.match_tags("", {"t1": ["cool"]}) == []
    assert tag_utils.match_tags("   ", {"t1": ["cool"]}) == []
    assert tag_utils.match_tags("cool", {}) == []


def test_match_tags_returns_track_ids():
    all_tags = {"t1": ["cool", "idm"], "t2": ["cool"]}
    [entry] = [e for e in tag_utils.match_tags("cool", all_tags)]
    assert sorted(entry["track_ids"]) == ["t1", "t2"]


def test_match_tags_unicode_multiword():
    """Whole-token match must keep accented/CJK tokens (Unicode-aware split)."""
    all_tags = {"t1": ["jazz café"], "t2": ["ジャズ mix"]}
    assert {e["name"] for e in tag_utils.match_tags("café", all_tags)} == {"jazz café"}
    assert {e["name"] for e in tag_utils.match_tags("mix", all_tags)} == {"ジャズ mix"}
    assert tag_utils.match_tags("caf", all_tags) == []  # substring, not a whole token


def test_track_dedup_key_mirrors_frontend():
    # title|artist|album|disc|track, strings lowercased, disc defaults to 1
    k = tag_utils.track_dedup_key(
        {"title": "Teardrop", "artist": "Massive Attack", "album": "Mezzanine",
         "disc_number": 1, "track_number": 5})
    assert k == "teardrop|massive attack|mezzanine|1|5"


def test_track_dedup_key_defaults():
    k = tag_utils.track_dedup_key({"title": "X", "artist": "Y", "album": "Z"})
    assert k == "x|y|z|1|"  # missing disc -> 1, missing track -> empty
    # disc 0 -> 1 (falsy), track 0 -> "0" (present)
    k2 = tag_utils.track_dedup_key(
        {"title": "X", "artist": "Y", "album": "Z", "disc_number": 0, "track_number": 0})
    assert k2 == "x|y|z|1|0"


def test_dedup_count_collapses_same_key_keeps_distinct():
    tracks = [
        {"title": "Teardrop", "artist": "MA", "album": "Mezzanine", "disc_number": 1, "track_number": 5},
        {"title": "Teardrop", "artist": "MA", "album": "Mezzanine", "disc_number": 1, "track_number": 5},  # cross-server copy
        {"title": "Teardrop", "artist": "MA", "album": "Singles", "disc_number": 1, "track_number": 1},     # distinct release
    ]
    assert tag_utils.dedup_count(tracks) == 2
    assert tag_utils.dedup_count([]) == 0

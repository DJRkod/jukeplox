import pytest

from app import database
from app.config import Settings


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path, secret_key="test")
    monkeypatch.setattr(database, "settings", s)
    return s


@pytest.fixture
async def db(tmp_settings):
    # close_db() is mandatory, not tidiness: aiosqlite runs its connection on a
    # NON-daemon thread, so leaving it open blocks threading._shutdown and hangs
    # the pytest process AFTER the tests have passed. pytest-timeout cannot
    # catch that — it is an interpreter-exit hang, not an in-test one.
    await database.init_db()
    try:
        yield tmp_settings
    finally:
        await database.close_db()


# ── init ──────────────────────────────────────────────────────────────────────

async def test_init_db_creates_tables(db):
    import aiosqlite
    async with aiosqlite.connect(db.db_path) as conn:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as cur:
            names = {row[0] async for row in cur}
    assert {"settings", "plex_config", "enabled_libraries", "admin_sessions", "queue_state"} <= names


async def test_init_db_idempotent(db):
    await database.init_db()  # second call should not raise


async def test_init_db_creates_catalog_tables(db):
    # U5: the unified catalog ships additively alongside the browse-index, which
    # is retained for the U7 rollback window — both must exist after init.
    import aiosqlite
    async with aiosqlite.connect(db.db_path) as conn:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as cur:
            names = {row[0] async for row in cur}
    assert {"catalog_artist", "catalog_album", "catalog_track",
            "catalog_holds", "catalog_identity_alias"} <= names
    assert {"browse_artist_index", "browse_album_index"} <= names  # coexistence


# ── settings ─────────────────────────────────────────────────────────────────

async def test_settings_round_trip(db):
    await database.set_setting("foo", "bar")
    assert await database.get_setting("foo") == "bar"


async def test_settings_missing_returns_default(db):
    assert await database.get_setting("missing") is None
    assert await database.get_setting("missing", "fallback") == "fallback"


async def test_settings_overwrite(db):
    await database.set_setting("key", "v1")
    await database.set_setting("key", "v2")
    assert await database.get_setting("key") == "v2"


async def test_rail_alpha_mode_default_and_validation(db):
    # Default is the shipped English-friendly rail; unknown values fall back to it.
    assert await database.get_rail_alpha_mode() == "english"
    await database.set_setting("rail_alpha_mode", "international")
    assert await database.get_rail_alpha_mode() == "international"
    await database.set_setting("rail_alpha_mode", "bogus")
    assert await database.get_rail_alpha_mode() == "english"


async def test_rail_thresholds_default_and_coerce(db):
    assert await database.get_rail_artist_threshold() == 2
    assert await database.get_rail_album_threshold() == 2
    await database.set_setting("rail_artist_threshold", "5")
    assert await database.get_rail_artist_threshold() == 5
    await database.set_setting("rail_album_threshold", "0")          # clamps to 1
    assert await database.get_rail_album_threshold() == 1
    await database.set_setting("rail_artist_threshold", "notanint")  # → default 2
    assert await database.get_rail_artist_threshold() == 2


# ── plex config ───────────────────────────────────────────────────────────────

async def test_plex_config_round_trip(db):
    await database.set_plex_config("http://plex.local:32400", "tok123", "client-abc")
    cfg = await database.get_plex_config()
    assert cfg["server_url"] == "http://plex.local:32400"
    assert cfg["token"] == "tok123"
    assert cfg["client_id"] == "client-abc"


async def test_plex_config_missing_returns_none(db):
    assert await database.get_plex_config() is None


async def test_plex_config_upsert(db):
    await database.set_plex_config("http://old:32400", "old_tok", "cid")
    await database.set_plex_config("http://new:32400", "new_tok", "cid")
    cfg = await database.get_plex_config()
    assert cfg["server_url"] == "http://new:32400"


# ── jellyfin sources (U10) ────────────────────────────────────────────────────

async def test_jellyfin_sources_empty_by_default(db):
    assert await database.get_jellyfin_sources() == []


async def test_jellyfin_source_round_trip_and_upsert(db):
    await database.save_jellyfin_source(
        "jelly1", "http://jf.local:8096", "Living Room", "tok", "uid", "dev1")
    rows = await database.get_jellyfin_sources()
    assert len(rows) == 1
    assert rows[0]["server_url"] == "http://jf.local:8096"
    assert rows[0]["token"] == "tok" and rows[0]["user_id"] == "uid"
    # upsert on the same source_id replaces, not duplicates
    await database.save_jellyfin_source(
        "jelly1", "http://jf.local:8096", "Den", "tok2", "uid", "dev1")
    rows = await database.get_jellyfin_sources()
    assert len(rows) == 1 and rows[0]["name"] == "Den" and rows[0]["token"] == "tok2"


async def test_jellyfin_source_stores_token_only_no_password_column(db):
    # Plan R24: the account password is never persisted — there is no column for it.
    import aiosqlite
    async with aiosqlite.connect(db.db_path) as conn:
        async with conn.execute("PRAGMA table_info(jellyfin_sources)") as cur:
            cols = {row[1] async for row in cur}
    assert "token" in cols and "user_id" in cols
    assert not ({"password", "pw", "pass"} & cols)


async def test_jellyfin_source_delete(db):
    await database.save_jellyfin_source(
        "jelly1", "http://jf.local:8096", "X", "tok", "uid", "dev1")
    await database.delete_jellyfin_source("jelly1")
    assert await database.get_jellyfin_sources() == []


# ── local file sources (U11) ──────────────────────────────────────────────────

async def test_local_sources_empty_by_default(db):
    assert await database.get_local_sources() == []


async def test_local_source_round_trip_and_upsert(db):
    await database.save_local_source("local-1", "Vinyl Rips", "/music/flac")
    rows = await database.get_local_sources()
    assert len(rows) == 1
    assert rows[0]["source_id"] == "local-1"
    assert rows[0]["name"] == "Vinyl Rips"
    assert rows[0]["root_dir"] == "/music/flac"
    # upsert on the same source_id replaces, not duplicates
    await database.save_local_source("local-1", "Archive", "/mnt/archive")
    rows = await database.get_local_sources()
    assert len(rows) == 1
    assert rows[0]["name"] == "Archive" and rows[0]["root_dir"] == "/mnt/archive"


async def test_local_source_stores_no_credential_column(db):
    # Plan R6/R24: a local source is read-only and credential-free.
    import aiosqlite
    async with aiosqlite.connect(db.db_path) as conn:
        async with conn.execute("PRAGMA table_info(local_sources)") as cur:
            cols = {row[1] async for row in cur}
    assert cols == {"source_id", "name", "root_dir"}


async def test_local_source_delete(db):
    await database.save_local_source("local-1", "X", "/music")
    await database.delete_local_source("local-1")
    assert await database.get_local_sources() == []


# ── enabled libraries ─────────────────────────────────────────────────────────

async def test_toggle_library_enable(db):
    await database.toggle_library("sec1", "Music", enabled=True)
    libs = await database.get_enabled_libraries()
    assert any(lib["section_key"] == "sec1" for lib in libs)


async def test_toggle_library_disable(db):
    await database.toggle_library("sec1", "Music", enabled=True)
    await database.toggle_library("sec1", "Music", enabled=False)
    libs = await database.get_enabled_libraries()
    assert not any(lib["section_key"] == "sec1" for lib in libs)


async def test_enabled_libraries_empty_by_default(db):
    assert await database.get_enabled_libraries() == []


# ── disabled sources (per-source veto, Libraries-panel U1) ────────────────────

async def test_disabled_sources_empty_by_default(db):
    assert await database.get_disabled_sources() == []


async def test_disabled_sources_round_trip(db):
    await database.set_disabled_sources(["jf-abc", "local-123"])
    assert await database.get_disabled_sources() == ["jf-abc", "local-123"]


async def test_disabled_sources_overwrite_and_clear(db):
    await database.set_disabled_sources(["jf-abc"])
    await database.set_disabled_sources(["local-123"])
    assert await database.get_disabled_sources() == ["local-123"]
    await database.set_disabled_sources([])
    assert await database.get_disabled_sources() == []


async def test_disabled_sources_legacy_empty_source_id_round_trips(db):
    # The legacy single Plex server has machine_id == "" -> source_id "".
    await database.set_disabled_sources([""])
    assert await database.get_disabled_sources() == [""]


def test_source_id_from_section_key():
    assert database.source_id_from_section_key("mach:5") == "mach"
    assert database.source_id_from_section_key("jf-abc:12") == "jf-abc"
    assert database.source_id_from_section_key("local-deadbeef:lib") == "local-deadbeef"
    assert database.source_id_from_section_key("5") == ""       # legacy no-colon Plex key
    assert database.source_id_from_section_key("") == ""


async def test_effective_enabled_libraries_no_veto_returns_all(db):
    await database.toggle_library("plexM:1", "Music", enabled=True)
    await database.toggle_library("jf-a:2", "JF Music", enabled=True)
    eff = await database.get_effective_enabled_libraries()
    assert {r["section_key"] for r in eff} == {"plexM:1", "jf-a:2"}


async def test_effective_enabled_libraries_excludes_vetoed_source(db):
    await database.toggle_library("plexM:1", "Music", enabled=True)
    await database.toggle_library("jf-a:2", "JF Music", enabled=True)
    await database.set_disabled_sources(["jf-a"])
    eff = await database.get_effective_enabled_libraries()
    assert {r["section_key"] for r in eff} == {"plexM:1"}
    # Raw enabled rows are untouched — the per-library selection is remembered.
    assert {r["section_key"] for r in await database.get_enabled_libraries()} == {"plexM:1", "jf-a:2"}


async def test_effective_enabled_libraries_legacy_empty_source_vetoed(db):
    await database.toggle_library("5", "Legacy", enabled=True)   # no colon -> source_id ""
    await database.set_disabled_sources([""])
    assert await database.get_effective_enabled_libraries() == []


async def test_effective_enabled_libraries_fails_open_when_veto_unreadable(db, monkeypatch):
    await database.toggle_library("plexM:1", "Music", enabled=True)

    async def boom():
        raise RuntimeError("settings read failed")

    monkeypatch.setattr(database, "get_disabled_sources", boom)
    eff = await database.get_effective_enabled_libraries()
    assert {r["section_key"] for r in eff} == {"plexM:1"}


# ── sessions ──────────────────────────────────────────────────────────────────

async def test_session_create_and_retrieve(db):
    await database.create_session("tok", "2026-01-01T00:00:00", "2026-01-01T08:00:00")
    session = await database.get_session("tok")
    assert session is not None
    assert session["token"] == "tok"


async def test_session_missing_returns_none(db):
    assert await database.get_session("nonexistent") is None


async def test_session_delete(db):
    await database.create_session("tok", "2026-01-01T00:00:00", "2026-01-01T08:00:00")
    await database.delete_session("tok")
    assert await database.get_session("tok") is None


async def test_delete_expired_sessions(db):
    await database.create_session("expired", "2026-01-01T00:00:00", "2026-01-01T08:00:00")
    await database.create_session("valid", "2026-01-01T00:00:00", "2026-12-31T08:00:00")
    await database.delete_expired_sessions("2026-06-01T00:00:00")
    assert await database.get_session("expired") is None
    assert await database.get_session("valid") is not None


# ── queue state ───────────────────────────────────────────────────────────────

async def test_queue_save_and_load(db):
    items = [
        {"track_id": "t1", "metadata_json": '{"title":"A"}', "added_at": "2026-01-01T00:00:00"},
        {"track_id": "t2", "metadata_json": '{"title":"B"}', "added_at": "2026-01-01T00:01:00"},
    ]
    await database.save_queue(items)
    loaded = await database.load_queue()
    assert len(loaded) == 2
    assert loaded[0]["track_id"] == "t1"
    assert loaded[1]["track_id"] == "t2"


async def test_queue_save_replaces_existing(db):
    await database.save_queue([
        {"track_id": "old", "metadata_json": "{}", "added_at": "2026-01-01T00:00:00"},
    ])
    await database.save_queue([
        {"track_id": "new", "metadata_json": "{}", "added_at": "2026-01-01T00:00:00"},
    ])
    loaded = await database.load_queue()
    assert len(loaded) == 1
    assert loaded[0]["track_id"] == "new"


async def test_queue_load_empty(db):
    assert await database.load_queue() == []


# ── credit cache (2026-06-10 per-track credits plan U2) ──────────────────────

_CREDIT_ROWS = [
    {"name": "13th Floor Elevators", "name_lower": "13th floor elevators",
     "album_id": "al-nuggets", "album_title": "Nuggets", "album_artist": "Various Artists",
     "album_thumb": "/t/1", "album_year": 1972, "server_name": "My Plex"},
    {"name": "The Seeds", "name_lower": "the seeds",
     "album_id": "al-nuggets", "album_title": "Nuggets", "album_artist": "Various Artists",
     "album_thumb": "/t/1", "album_year": 1972, "server_name": "My Plex"},
    {"name": "13th Floor Elevators", "name_lower": "13th floor elevators",
     "album_id": "al-texas", "album_title": "Texas Psych Comp", "album_artist": "Various Artists",
     "album_thumb": None, "album_year": 1985, "server_name": "My Plex"},
]


async def test_credit_cache_round_trip(db):
    await database.set_credit_cache(_CREDIT_ROWS)
    acts = await database.get_credit_acts()
    assert [(a["name"], a["release_count"]) for a in acts] == [
        ("13th Floor Elevators", 2), ("The Seeds", 1)]
    apps = await database.get_credit_appearances("13th floor elevators")
    assert [a["album_id"] for a in apps] == ["al-nuggets", "al-texas"]
    assert apps[0]["album_artist"] == "Various Artists"


async def test_credit_cache_replace_removes_stale(db):
    await database.set_credit_cache(_CREDIT_ROWS)
    await database.set_credit_cache(_CREDIT_ROWS[:1])
    acts = await database.get_credit_acts()
    assert [a["name"] for a in acts] == ["13th Floor Elevators"]
    assert acts[0]["release_count"] == 1


async def test_credit_cache_empty_replace_clears(db):
    await database.set_credit_cache(_CREDIT_ROWS)
    await database.set_credit_cache([])
    assert await database.get_credit_acts() == []


async def test_credit_appearances_unknown_name_empty(db):
    await database.set_credit_cache(_CREDIT_ROWS)
    assert await database.get_credit_appearances("nobody") == []


async def test_credit_cache_duplicate_pairs_ignored(db):
    await database.set_credit_cache(_CREDIT_ROWS + _CREDIT_ROWS[:1])
    acts = await database.get_credit_acts()
    assert [(a["name"], a["release_count"]) for a in acts] == [
        ("13th Floor Elevators", 2), ("The Seeds", 1)]


_NON_VA_ROW = {
    "name": "!!! & Angelica Garcia", "name_lower": "!!! & angelica garcia",
    "album_id": "al-own", "album_title": "Wallop", "album_artist": "!!!",
    "album_thumb": None, "album_year": 2019, "server_name": "My Plex",
}


async def test_credit_acts_va_only_excludes_non_va_variations(db):
    """Browse-VA-gate R1/AE1/AE2: acts whose appearances are all on non-VA
    releases drop out under va_only; VA-compilation acts survive."""
    await database.set_credit_cache(_CREDIT_ROWS + [_NON_VA_ROW])
    gated = await database.get_credit_acts(va_only=True)
    assert [a["name"] for a in gated] == ["13th Floor Elevators", "The Seeds"]
    ungated = await database.get_credit_acts()
    assert "!!! & Angelica Garcia" in [a["name"] for a in ungated]


async def test_credit_acts_va_only_mixed_act_survives_with_full_count(db):
    """An act with BOTH a VA appearance and a non-VA variation row passes
    the gate, and its count covers all appearances (the gate filters acts,
    not rows)."""
    mixed = dict(_NON_VA_ROW, name="13th Floor Elevators",
                 name_lower="13th floor elevators", album_id="al-guest")
    await database.set_credit_cache(_CREDIT_ROWS + [mixed])
    gated = await database.get_credit_acts(va_only=True)
    elevators = next(a for a in gated if a["name"] == "13th Floor Elevators")
    assert elevators["release_count"] == 3  # 2 VA + 1 non-VA appearance


async def test_credit_acts_va_only_case_insensitive_va_match(db):
    row = dict(_NON_VA_ROW, album_artist="VARIOUS ARTISTS")
    await database.set_credit_cache([row])
    gated = await database.get_credit_acts(va_only=True)
    assert [a["name"] for a in gated] == ["!!! & Angelica Garcia"]


# ── pattern rules + exclusions storage (2026-06-10 pattern-rules plan U1) ────

async def test_pattern_rules_round_trip(db):
    rules = [["&", "and"], ["'", "’", "´"]]
    await database.set_pattern_rules(rules)
    assert await database.get_pattern_rules() == rules


async def test_pattern_rules_unset_returns_editable_defaults(db):
    """Never-saved installs get the shipped defaults (data, not behavior);
    exclusions have no defaults."""
    from app.normalize import DEFAULT_PATTERN_RULES
    got = await database.get_pattern_rules()
    assert got == DEFAULT_PATTERN_RULES
    assert got is not DEFAULT_PATTERN_RULES  # caller-safe copy
    assert got[0] is not DEFAULT_PATTERN_RULES[0]
    assert await database.get_artist_exclusions() == []


async def test_pattern_rules_saved_empty_stays_empty(db):
    """Deleting every rule is a choice, not a reset: a SAVED empty list
    must not resurrect the defaults."""
    await database.set_pattern_rules([])
    assert await database.get_pattern_rules() == []


async def test_artist_exclusions_round_trip(db):
    await database.set_artist_exclusions(["[dialogue]", "Various Artists Chorus"])
    assert await database.get_artist_exclusions() == ["[dialogue]", "Various Artists Chorus"]


async def test_pattern_rules_corrupt_json_returns_empty(db):
    await database.set_setting("pattern_rules", "{not json")
    assert await database.get_pattern_rules() == []


# ── play_track_meta + top played (2026-06-10 most-played plan U1) ────────────

_META = {"track_id": "t1", "title": "Song", "artist": "A", "album": "B",
         "thumb": None, "duration_ms": 1000, "server_name": "My Plex"}


async def test_play_track_meta_upsert_fresh_wins(db):
    await database.set_play_track_meta("t1", _META)
    await database.set_play_track_meta("t1", dict(_META, title="Renamed"))
    rows = await database.get_top_played_tracks()
    assert rows == []  # no counts yet — meta alone doesn't surface
    await database.increment_play_count("track", "t1")
    rows = await database.get_top_played_tracks()
    assert rows[0]["metadata"]["title"] == "Renamed"  # one row, fresh meta


async def test_top_played_orders_caps_and_flags_missing_meta(db):
    for tid, plays in [("t1", 3), ("t2", 5), ("t3", 1)]:
        for _ in range(plays):
            await database.increment_play_count("track", tid)
    await database.set_play_track_meta("t1", _META)
    await database.set_play_track_meta("t2", dict(_META, track_id="t2"))
    # t3 has no meta row (pre-feature count) → metadata None
    rows = await database.get_top_played_tracks()
    assert [r["track_id"] for r in rows] == ["t2", "t1", "t3"]
    assert [r["count"] for r in rows] == [5, 3, 1]
    assert rows[2]["metadata"] is None
    capped = await database.get_top_played_tracks(limit=2)
    assert [r["track_id"] for r in capped] == ["t2", "t1"]


async def test_top_played_ignores_album_artist_counts(db):
    await database.increment_play_count("album", "Some Album")
    await database.increment_play_count("artist", "Some Artist")
    assert await database.get_top_played_tracks() == []


async def test_decrement_play_count_floors_at_zero_and_noops(db):
    await database.increment_play_count("track", "t1")
    await database.increment_play_count("track", "t1")          # count = 2
    await database.decrement_play_count("track", "t1")
    assert await database.get_play_count("track", "t1") == 1
    await database.decrement_play_count("track", "t1")
    await database.decrement_play_count("track", "t1")          # already 0 → floor
    assert await database.get_play_count("track", "t1") == 0
    await database.decrement_play_count("track", "absent")      # no row → no-op
    assert await database.get_play_count("track", "absent") == 0


async def test_decrement_play_count_is_name_keyed_and_independent(db):
    await database.increment_play_count("album", "Broken")
    await database.increment_play_count("track", "t1")
    await database.decrement_play_count("album", "Broken")      # name-keyed album row
    assert await database.get_play_count("album", "Broken") == 0
    assert await database.get_play_count("track", "t1") == 1    # track row untouched


async def test_top_played_unbounded_returns_all_with_none_limit(db):
    """limit=None returns every counted track (SQLite LIMIT -1). Popular Random
    relies on this: its pool is gated by the play-count threshold, NOT by the
    Most Played leaderboard's display cap."""
    for i in range(105):
        await database.increment_play_count("track", f"t{i:03d}")
    assert len(await database.get_top_played_tracks(None)) == 105
    assert len(await database.get_top_played_tracks()) == 100  # default still caps


async def test_most_played_display_limit_default_and_clamp(db):
    """Display limit: unset → 100; stored value honored; below 1 clamps to 1;
    unparseable falls back to 100 (mirrors get_popular_random_threshold)."""
    assert await database.get_most_played_display_limit() == 100
    await database.set_setting("most_played_display_limit", "25")
    assert await database.get_most_played_display_limit() == 25
    await database.set_setting("most_played_display_limit", "0")
    assert await database.get_most_played_display_limit() == 1
    await database.set_setting("most_played_display_limit", "notanint")
    assert await database.get_most_played_display_limit() == 100


# ── plex_servers ownership (2026-06-11 collected-library plan U1) ─────────────

def _server_row(machine_id, name, **extra):
    row = {
        "machine_id": machine_id, "server_url": f"http://{machine_id}:32400",
        "name": name, "owner": "admin", "token": "tok", "client_id": "cid",
    }
    row.update(extra)
    return row


async def test_plex_servers_owned_persisted(db):
    await database.save_plex_servers([
        _server_row("m1", "Zeta", owned=True),
        _server_row("m2", "Alpha", owned=False),
        _server_row("m3", "Mu"),  # no owned key (legacy caller) → NULL
    ])
    rows = {r["machine_id"]: r for r in await database.get_plex_servers()}
    assert rows["m1"]["owned"] == 1
    assert rows["m2"]["owned"] == 0
    assert rows["m3"]["owned"] is None


async def test_owned_column_added_to_legacy_db(tmp_settings):
    """A DB created before the owned column existed gains it on init
    without data loss (guarded ALTER; NULL = unknown)."""
    import aiosqlite
    async with aiosqlite.connect(tmp_settings.db_path) as conn:
        await conn.execute(
            "CREATE TABLE plex_servers ("
            "machine_id TEXT PRIMARY KEY, server_url TEXT NOT NULL, "
            "name TEXT NOT NULL, owner TEXT NOT NULL, token TEXT NOT NULL, "
            "client_id TEXT NOT NULL)"
        )
        await conn.execute(
            "INSERT INTO plex_servers VALUES ('mOld', 'http://x', 'Old', 'admin', 't', 'c')"
        )
        await conn.commit()
    await database.init_db()
    rows = await database.get_plex_servers()
    assert len(rows) == 1
    assert rows[0]["name"] == "Old"
    assert rows[0]["owned"] is None
    # idempotent across restarts
    await database.init_db()


# ── browse index (2026-06-21 plan U1) ────────────────────────────────────────

def _artist_row(artist_id, title, base_key, server="A", section="A:1", **kw):
    return {"artist_id": artist_id, "title": title, "base_key": base_key,
            "thumb": kw.get("thumb"), "release_count": kw.get("release_count"),
            "server_name": server, "section_key": section}


def _album_row(album_id, title, title_base, artist, artist_base, server="A",
               section="A:1", **kw):
    return {"album_id": album_id, "title": title, "title_base": title_base,
            "artist": artist, "artist_base_key": artist_base,
            "year": kw.get("year"), "thumb": kw.get("thumb"),
            "subtype": kw.get("subtype"), "added_at": kw.get("added_at"),
            "track_count": kw.get("track_count"),
            "server_name": server, "section_key": section}


async def test_browse_index_round_trip(db):
    artists = [_artist_row("A:1", "Radiohead", "radiohead", release_count=9)]
    albums = [_album_row("A:10", "OK Computer", "ok computer", "Radiohead",
                         "radiohead", year=1997)]
    await database.set_browse_index(artists, albums)
    got_a = await database.get_browse_artists()
    got_b = await database.get_browse_albums()
    assert len(got_a) == 1 and got_a[0]["title"] == "Radiohead"
    assert got_a[0]["release_count"] == 9 and got_a[0]["base_key"] == "radiohead"
    assert len(got_b) == 1 and got_b[0]["year"] == 1997
    assert got_b[0]["artist_base_key"] == "radiohead"


async def test_browse_index_albums_for_artist_cross_server(db):
    # Same artist identity on two servers; a different artist must be excluded.
    albums = [
        _album_row("A:10", "Kid A", "kid a", "Radiohead", "radiohead", server="A"),
        _album_row("B:10", "Amnesiac", "amnesiac", "Radiohead", "radiohead", server="B"),
        _album_row("A:99", "Blue", "blue", "Joni Mitchell", "joni mitchell"),
    ]
    await database.set_browse_index([], albums)
    rows = await database.get_browse_albums_for_artist("radiohead")
    assert {r["album_id"] for r in rows} == {"A:10", "B:10"}
    assert {r["server_name"] for r in rows} == {"A", "B"}
    assert await database.get_browse_albums_for_artist("nobody") == []


async def test_browse_index_by_identity_resolves_per_server_copies(db):
    albums = [
        _album_row("A:10", "Kid A", "kid a", "Radiohead", "radiohead", server="A"),
        _album_row("B:55", "Kid A", "kid a", "Radiohead", "radiohead", server="B"),
        _album_row("A:11", "Kid A", "kid a", "Other Band", "other band"),
    ]
    await database.set_browse_index([], albums)
    rows = await database.get_browse_albums_by_identity("kid a", "radiohead")
    assert {r["album_id"] for r in rows} == {"A:10", "B:55"}


async def test_browse_index_by_id_lookups(db):
    await database.set_browse_index(
        [_artist_row("A:1", "Radiohead", "radiohead")],
        [_album_row("A:10", "Kid A", "kid a", "Radiohead", "radiohead")],
    )
    assert (await database.get_browse_artist_by_id("A:1"))["title"] == "Radiohead"
    assert (await database.get_browse_album_by_id("A:10"))["title"] == "Kid A"
    assert await database.get_browse_artist_by_id("missing") is None
    assert await database.get_browse_album_by_id("missing") is None


async def test_browse_index_atomic_replace(db):
    await database.set_browse_index(
        [_artist_row("A:1", "Old", "old")],
        [_album_row("A:10", "Old Alb", "old alb", "Old", "old")],
    )
    await database.set_browse_index(
        [_artist_row("A:2", "New", "new")],
        [_album_row("A:20", "New Alb", "new alb", "New", "new")],
    )
    arts = await database.get_browse_artists()
    albs = await database.get_browse_albums()
    assert [a["artist_id"] for a in arts] == ["A:2"]
    assert [a["album_id"] for a in albs] == ["A:20"]


async def test_browse_index_empty_clears(db):
    await database.set_browse_index(
        [_artist_row("A:1", "X", "x")], [_album_row("A:10", "Y", "y", "X", "x")]
    )
    await database.set_browse_index([], [])
    assert await database.get_browse_artists() == []
    assert await database.get_browse_albums() == []


async def test_browse_index_compound_ids_survive(db):
    # Machine-prefixed compound ids (with ':') round-trip intact.
    await database.set_browse_index(
        [_artist_row("machineB:42", "Björk", "björk")],
        [_album_row("machineB:43", "Homogenic", "homogenic", "Björk", "björk")],
    )
    assert (await database.get_browse_artist_by_id("machineB:42"))["title"] == "Björk"
    rows = await database.get_browse_albums_for_artist("björk")
    assert rows[0]["album_id"] == "machineB:43"


async def test_browse_index_added_at_round_trips(db):
    """Recently Added plan U2: added_at persists through set/get; None allowed."""
    albums = [
        _album_row("A:10", "Kid A", "kid a", "Radiohead", "radiohead",
                   added_at=1700000000),
        _album_row("A:11", "Untitled", "untitled", "X", "x"),  # no added_at → None
    ]
    await database.set_browse_index([], albums)
    got = {r["album_id"]: r for r in await database.get_browse_albums()}
    assert got["A:10"]["added_at"] == 1700000000
    assert got["A:11"]["added_at"] is None


async def test_browse_album_index_added_at_migration(db):
    """U2: a browse_album_index predating added_at gains the column on migrate
    without dropping rows (mirrors the plex_servers.owned precedent)."""
    conn = database._conn()
    await conn.execute("DROP TABLE browse_album_index")
    await conn.execute(
        "CREATE TABLE browse_album_index (album_id TEXT PRIMARY KEY, title TEXT NOT NULL,"
        " title_base TEXT NOT NULL, artist TEXT NOT NULL, artist_base_key TEXT NOT NULL,"
        " year INTEGER, thumb TEXT, subtype TEXT, server_name TEXT, section_key TEXT)"
    )
    await conn.execute(
        "INSERT INTO browse_album_index (album_id,title,title_base,artist,artist_base_key)"
        " VALUES ('A:1','T','t','Ar','ar')"
    )
    await conn.commit()
    await database._migrate_columns()
    await conn.commit()
    async with conn.execute("PRAGMA table_info(browse_album_index)") as cur:
        cols = {row["name"] async for row in cur}
    assert "added_at" in cols
    rows = await database.get_browse_albums()
    assert len(rows) == 1 and rows[0]["added_at"] is None


async def test_browse_index_track_count_round_trips(db):
    """Same-title plan U2: track_count persists through set/get; None allowed."""
    albums = [
        _album_row("A:10", "Loveless", "loveless", "My Bloody Valentine",
                   "my bloody valentine", track_count=11),
        _album_row("A:11", "Untitled", "untitled", "X", "x"),  # no count → None
    ]
    await database.set_browse_index([], albums)
    got = {r["album_id"]: r for r in await database.get_browse_albums()}
    assert got["A:10"]["track_count"] == 11
    assert got["A:11"]["track_count"] is None


async def test_browse_album_index_track_count_migration(db):
    """U2: an index predating track_count gains the column on migrate without
    dropping rows."""
    conn = database._conn()
    await conn.execute("DROP TABLE browse_album_index")
    await conn.execute(
        "CREATE TABLE browse_album_index (album_id TEXT PRIMARY KEY, title TEXT NOT NULL,"
        " title_base TEXT NOT NULL, artist TEXT NOT NULL, artist_base_key TEXT NOT NULL,"
        " year INTEGER, thumb TEXT, subtype TEXT, added_at INTEGER, server_name TEXT,"
        " section_key TEXT)"
    )
    await conn.execute(
        "INSERT INTO browse_album_index (album_id,title,title_base,artist,artist_base_key)"
        " VALUES ('A:1','T','t','Ar','ar')"
    )
    await conn.commit()
    await database._migrate_columns()
    await conn.commit()
    async with conn.execute("PRAGMA table_info(browse_album_index)") as cur:
        cols = {row["name"] async for row in cur}
    assert "track_count" in cols
    rows = await database.get_browse_albums()
    assert len(rows) == 1 and rows[0]["track_count"] is None


async def test_browse_index_tables_created(db):
    import aiosqlite
    async with aiosqlite.connect(db.db_path) as conn:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as cur:
            names = {row[0] async for row in cur}
    assert {"browse_artist_index", "browse_album_index"} <= names


# ── track ratings + tags (2026-06-26 track-ratings-and-tags plan U1) ─────────

async def test_ratings_tags_tables_created(db):
    import aiosqlite
    async with aiosqlite.connect(db.db_path) as conn:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as cur:
            names = {row[0] async for row in cur}
    assert {"track_ratings", "track_tags"} <= names


async def test_rating_set_get_overwrite(db):
    assert await database.get_rating("t1") is None          # unrated by default
    await database.set_rating("t1", 4)
    assert await database.get_rating("t1") == 4
    await database.set_rating("t1", 2)                       # re-rate overwrites
    assert await database.get_rating("t1") == 2


async def test_rating_zero_clears(db):
    await database.set_rating("t1", 5)
    await database.set_rating("t1", 0)                       # 0 == clear, no zero state
    assert await database.get_rating("t1") is None


async def test_rating_clamped_to_five(db):
    await database.set_rating("t1", 9)
    assert await database.get_rating("t1") == 5


async def test_get_ratings_batch(db):
    await database.set_rating("t1", 3)
    await database.set_rating("t2", 5)
    assert await database.get_ratings(["t1", "t2", "t3"]) == {"t1": 3, "t2": 5}
    assert await database.get_ratings([]) == {}


async def test_tags_normalize_trim_dedupe_dropempty(db):
    stored = await database.set_tags("t1", ["  Fun ", "fun", "", "FUN", "Chill"])
    assert stored == ["Fun", "Chill"]                       # trim + case-insensitive dedupe + drop empty
    assert await database.get_tags("t1") == ["Fun", "Chill"]


async def test_tags_caps(db):
    stored = await database.set_tags("t1", ["x" * 60])
    assert stored == ["x" * database.TAG_MAX_LEN]           # per-tag length cap
    stored = await database.set_tags("t2", [f"tag{i}" for i in range(20)])
    assert len(stored) == database.TAGS_MAX_PER_TRACK       # per-track count cap


async def test_tags_empty_deletes_row(db):
    await database.set_tags("t1", ["a"])
    assert await database.get_tags("t1") == ["a"]
    await database.set_tags("t1", [])
    assert await database.get_tags("t1") == []


async def test_get_tags_bulk(db):
    await database.set_tags("t1", ["a", "b"])
    await database.set_tags("t2", ["c"])
    assert await database.get_tags_bulk(["t1", "t2", "t3"]) == {"t1": ["a", "b"], "t2": ["c"]}
    assert await database.get_tags_bulk([]) == {}


async def test_top_rated_ordering_and_rated_only(db):
    await database.set_rating("t1", 5)
    await database.set_rating("t2", 5)
    await database.set_rating("t3", 3)
    for _ in range(3):
        await database.increment_play_count("track", "t2")
    await database.increment_play_count("track", "t1")
    rows = await database.get_top_rated_tracks()
    # rating DESC, then play_count DESC: t2 (5★/3), t1 (5★/1), t3 (3★/0)
    assert [r["track_id"] for r in rows] == ["t2", "t1", "t3"]
    assert rows[0]["stars"] == 5 and rows[0]["play_count"] == 3


async def test_top_rated_excludes_unrated(db):
    assert await database.get_top_rated_tracks() == []
    await database.increment_play_count("track", "t9")      # played but never rated
    assert await database.get_top_rated_tracks() == []


async def test_top_rated_metadata_backfill_shape(db):
    await database.set_rating("t1", 4)
    rows = await database.get_top_rated_tracks()
    assert rows[0]["metadata"] is None                      # uncaptured → endpoint backfills
    await database.set_play_track_meta("t1", {"track_id": "t1", "title": "Song"})
    rows = await database.get_top_rated_tracks()
    assert rows[0]["metadata"]["title"] == "Song"


async def test_guest_visibility_defaults_off(db):
    assert await database.get_ratings_visible_to_guests() is False
    assert await database.get_tags_visible_to_guests() is False
    await database.set_setting("ratings_visible_to_guests", "1")
    assert await database.get_ratings_visible_to_guests() is True


async def test_browse_facets_default_on(db):
    facets = await database.get_browse_facets()
    assert set(facets) == {"genre", "years", "mostplayed", "recentlyadded",
                           "highestrated", "radio"}
    # The five catalogue facets are on by default; Radio is the exception — it
    # is opt-in, so guests do not see it until an admin turns it on.
    assert all(v for k, v in facets.items() if k != "radio")
    assert facets["radio"] is False
    await database.set_setting("facet_years", "0")
    assert (await database.get_browse_facets())["years"] is False
    await database.set_setting("facet_radio", "1")
    assert (await database.get_browse_facets())["radio"] is True


# ── U17: credential at-rest hardening (R24) ──────────────────────────────────

async def test_plex_servers_token_round_trips_and_sealed_at_rest(db):
    from pathlib import Path
    import aiosqlite
    from app.sources import secrets
    await database.save_plex_servers([{
        "machine_id": "m1", "server_url": "http://x", "name": "Home", "owner": "me",
        "token": "PLEX-SECRET-XYZ", "client_id": "c1", "owned": 1}])
    # get_* opens the token back to plaintext for provider use.
    got = await database.get_plex_servers()
    assert got[0]["token"] == "PLEX-SECRET-XYZ"
    # The raw DB bytes never contain the plaintext token (sealed at rest, R24).
    assert b"PLEX-SECRET-XYZ" not in Path(db.db_path).read_bytes()
    # And the stored column is in sealed form.
    async with aiosqlite.connect(db.db_path) as conn:
        async with conn.execute("SELECT token FROM plex_servers WHERE machine_id='m1'") as cur:
            stored = (await cur.fetchone())[0]
    assert secrets.is_sealed(stored)


async def test_plex_config_token_round_trips_and_sealed(db):
    import aiosqlite
    from app.sources import secrets
    await database.set_plex_config("http://x", "CONFIG-TOKEN-9", "client-9")
    cfg = await database.get_plex_config()
    assert cfg["token"] == "CONFIG-TOKEN-9"
    async with aiosqlite.connect(db.db_path) as conn:
        async with conn.execute("SELECT token FROM plex_config WHERE id=1") as cur:
            stored = (await cur.fetchone())[0]
    assert secrets.is_sealed(stored) and "CONFIG-TOKEN-9" not in stored


async def test_jellyfin_token_round_trips_and_sealed(db):
    import aiosqlite
    from app.sources import secrets
    await database.save_jellyfin_source("jf-1", "http://jf", "Den", "JF-TOKEN-77", "u1", "dev1")
    got = await database.get_jellyfin_sources()
    assert got[0]["token"] == "JF-TOKEN-77"
    async with aiosqlite.connect(db.db_path) as conn:
        async with conn.execute("SELECT token FROM jellyfin_sources WHERE source_id='jf-1'") as cur:
            stored = (await cur.fetchone())[0]
    assert secrets.is_sealed(stored) and "JF-TOKEN-77" not in stored


async def test_plaintext_tokens_migrated_to_sealed_on_upgrade(db):
    from app.sources import secrets
    conn = database._conn()
    # Simulate a pre-U17 row: a plaintext token written directly (bypassing seal).
    await conn.execute(
        "INSERT INTO plex_servers (machine_id, server_url, name, owner, token, client_id, owned) "
        "VALUES ('m9','http://x','Home','me','LEGACY-PLAINTEXT','c1',1)")
    await conn.commit()
    await database._migrate_seal_credentials()
    await conn.commit()
    async with conn.execute("SELECT token FROM plex_servers WHERE machine_id='m9'") as cur:
        stored = (await cur.fetchone())[0]
    assert secrets.is_sealed(stored)  # re-sealed on upgrade
    got = {s["machine_id"]: s for s in await database.get_plex_servers()}
    assert got["m9"]["token"] == "LEGACY-PLAINTEXT"  # still authenticates (opens back)


async def test_seal_migration_is_idempotent(db):
    await database.save_plex_servers([{
        "machine_id": "m1", "server_url": "http://x", "name": "Home", "owner": "me",
        "token": "TOK", "client_id": "c1", "owned": 1}])
    conn = database._conn()
    async with conn.execute("SELECT token FROM plex_servers WHERE machine_id='m1'") as cur:
        first = (await cur.fetchone())[0]
    await database._migrate_seal_credentials()  # already sealed → must not double-seal
    await conn.commit()
    async with conn.execute("SELECT token FROM plex_servers WHERE machine_id='m1'") as cur:
        second = (await cur.fetchone())[0]
    assert first == second  # unchanged
    assert (await database.get_plex_servers())[0]["token"] == "TOK"

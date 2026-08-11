"""Pure tag search + dedup-count helpers (2026-08-11 searchable-clickable-tags).

Tag→tracks matching and count primitives shared by the search Tags section (U2)
and the /api/tag/tracks drill (U3). These are pure functions over a
``{track_id: [tags]}`` map (or track dicts) with no DB I/O — persistence lives in
``app.database`` (``get_all_tags``, ``normalize_tags``, etc.); tag matching and
render-parity counting are search-layer concerns and live here.
"""

import re


def _tag_tokens(name: str) -> set[str]:
    """Whole tokens of a normalized tag name for whole-token search matching so
    'cool' matches 'cool covers' but never 'coolant'. Splits on separators
    (whitespace, punctuation, underscore, hyphen) but is Unicode-aware — ``\\w``
    keeps accented/CJK letters, so 'jazz café' → {'jazz','café'} rather than
    dropping the non-ASCII token."""
    return {t for t in re.split(r"[\W_]+", name.lower()) if t}


def invert_tags(all_tags: dict[str, list[str]]) -> dict[str, dict]:
    """Invert ``{track_id: [tags]}`` into ``{normalized_tag: {"name": display,
    "track_ids": [ids]}}``, grouping case-insensitively (first spelling wins as
    the display name, mirroring ``database.normalize_tags``). Pure; callers pass a
    map from ``database.get_all_tags`` so one read serves both the Tags section and
    the inline fold within a request."""
    index: dict[str, dict] = {}
    for tid, tags in (all_tags or {}).items():
        if not isinstance(tags, list):
            continue
        for tag in tags:
            if not isinstance(tag, str):
                continue
            key = tag.strip().lower()
            if not key:
                continue
            entry = index.get(key)
            if entry is None:
                index[key] = {"name": tag.strip(), "track_ids": [tid]}
            else:
                entry["track_ids"].append(tid)
    return index


def match_tags(query: str, all_tags: dict[str, list[str]]) -> list[dict]:
    """Tags matching ``query`` — whole-token, case-insensitive, under the same
    lowering ``database.normalize_tags`` uses. A query matches a tag when it equals
    the name or appears as a whole token within it ('cool' matches 'cool' and
    'cool covers', never 'coolant'). Returns ``[{"name", "track_ids"}]``; the
    caller attaches the post-dedup count and finalizes display order (R3)."""
    q = (query or "").strip().lower()
    if not q:
        return []
    matches: list[dict] = []
    for key, entry in invert_tags(all_tags).items():
        if q == key or q in _tag_tokens(key):
            matches.append(entry)
    return matches


def track_dedup_key(track) -> str:
    """Frontend-parity dedup key mirroring ``_trackDedupKey`` in
    ``static/browse/index.js`` — ``title|artist|album|disc|track`` (strings
    lowercased, disc defaults to 1, track-number missing → empty). Server-side
    counts key on this so the Tags-section chip count and the drill header match
    the client's ``deduplicateTracks`` rendered row count (count-authority
    decision)."""
    def _get(*keys):
        for k in keys:
            v = track.get(k) if hasattr(track, "get") else getattr(track, k, None)
            if v is not None:
                return v
        return None
    title = str(_get("title") or "").lower()
    artist = str(_get("artist") or "").lower()
    album = str(_get("album") or "").lower()
    disc = _get("disc_number", "disc") or 1
    trackno = _get("track_number", "track")
    trackno = "" if trackno is None else trackno
    return f"{title}|{artist}|{album}|{disc}|{trackno}"


def dedup_count(tracks) -> int:
    """Distinct-track count under :func:`track_dedup_key` — the post-dedup count
    the Tags-section chip (U2) and the drill header (U3) both report."""
    return len({track_dedup_key(t) for t in (tracks or [])})

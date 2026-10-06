"""Characterization of cross-server album folding against real production data.

The fixture in ``tests/fixtures/characterization/album-fold-live-snapshot.jsonl``
is a scrubbed capture of a live two-server album browse taken on 2026-09-21,
while issue #61 was reproducing: one server reported a track count for every one
of its albums, the other for none of its 4,385, because the whole-section track
crawl that derives counts timed out on every refresh.

Why real data rather than synthetic fixtures: the defect was not a wrong branch,
it was a wrong ASSUMPTION about how often a count is unknown. ``_group_albums``
buckets by ``(track_count, subtype)`` on the deliberate premise — stated in its
own docstring — that an unknown count is "a stale index, or a surface like
search that doesn't carry it", i.e. rare and incidental. No synthetic fixture
written by someone holding that premise would have contradicted it. A capture of
the real library does, and it puts a number on it.

Scrubbing: server machine identifiers were replaced with stable synthetic ids
and server names with ``Server A`` / ``Server B``. Titles, artists, subtypes and
track counts are untouched — they are the signal under test. Thumbnails, years
and source arrays were dropped as irrelevant to grouping.

To regenerate, capture ``GET /api/browse/albums`` from a deployment and apply
the same scrub. Do not regenerate to make a failing assertion pass: a diff here
means grouping behaviour changed, which is the entire point of the file.
"""

import collections
import json
import io
import re
from pathlib import Path

from app.api.guest import _group_albums, _norm
from app.models import Album

FIXTURE = (Path(__file__).parent
           / "fixtures/characterization/album-fold-live-snapshot.jsonl")

# Measured against the deployment on 2026-09-21, before any fix.
LIVE_ROWS = 12764
LIVE_DUPLICATE_IDENTITIES = 2640
LIVE_UNAMBIGUOUS_SPLITS = 2520   # one known count on offer — safely foldable
LIVE_AMBIGUOUS_SPLITS = 21       # several known counts compete — never foldable

# After the unknown-count fold, with the counts still missing. This is the FLOOR
# the fold guarantees if the crawl fails again — not the fixed state, which is
# the crawl succeeding and the counts being real.
FOLDED_ROWS = 10228                  # 2,536 duplicate rows removed, 20% of the browse
RESIDUAL_DUPLICATE_IDENTITIES = 136  # entirely accounted for below
SAME_SOURCE_REPEATS = 115            # one source listing a release twice — by design


def _load() -> list[dict]:
    rows = []
    with io.open(FIXTURE, encoding="utf-8") as f:
        cols = json.loads(f.readline())
        for line in f:
            if line.strip():
                rows.append(dict(zip(cols, json.loads(line))))
    return rows


def _tagged(rows):
    return [(Album(id=r["album_id"], title=r["title"], artist=r["artist"],
                   subtype=r["subtype"], track_count=r["track_count"]), r["server"])
            for r in rows]


def _identity(title, artist):
    return (_norm(title, ()), _norm(artist, ()))


def _split_breakdown(rows):
    """(unambiguous, ambiguous) identity+subtype groups holding a known count
    alongside an unknown one."""
    groups = collections.defaultdict(list)
    for r in rows:
        groups[_identity(r["title"], r["artist"])].append(r)
    unambiguous = ambiguous = 0
    for items in groups.values():
        for st in {(i["subtype"] or "album").lower() for i in items}:
            g = [i for i in items if (i["subtype"] or "album").lower() == st]
            known = {i["track_count"] for i in g if i["track_count"] is not None}
            has_unknown = any(i["track_count"] is None for i in g)
            if known and has_unknown:
                if len(known) == 1:
                    unambiguous += 1
                else:
                    ambiguous += 1
    return unambiguous, ambiguous


# ── the fixture itself ────────────────────────────────────────────────────────

def test_fixture_loads_with_the_documented_columns():
    rows = _load()
    assert len(rows) == LIVE_ROWS
    assert set(rows[0]) == {"server", "album_id", "artist", "title",
                            "subtype", "track_count"}


def test_fixture_carries_no_server_identifiers():
    """This file ships in a PUBLIC repo. The scrub is part of the contract.

    Asserted POSITIVELY — every server name and album id must match the
    synthetic shape — rather than against a denylist of the real identifiers.
    A denylist has to spell out the machine ids and hostnames it is protecting,
    which publishes them in the very repo the scrub exists to keep clean; and it
    only ever catches the ones somebody remembered to list. The shape assertions
    below catch any identifier, including ones nobody thought of.
    """
    blob = io.open(FIXTURE, encoding="utf-8").read()
    rows = [json.loads(line) for line in blob.splitlines()[1:] if line.strip()]

    names = {r[0] for r in rows}
    assert names <= {"Server A", "Server B"}, (
        f"unscrubbed server name(s): {sorted(names - {'Server A', 'Server B'})}")

    bad_ids = [r[1] for r in rows if not re.fullmatch(r"server[AB]:\d+", r[1])]
    assert not bad_ids, f"unscrubbed album id(s): {bad_ids[:5]}"

    # Nothing anywhere in the file may look like an address or a machine id,
    # whichever column it hides in.
    addr = re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", blob)
    assert not addr, f"dotted-quad address in fixture: {addr.group()}"

    machine_id = re.search(r"\b[0-9a-f]{16,}\b", blob)
    assert not machine_id, f"machine-id-shaped token in fixture: {machine_id.group()}"


def test_fixture_reproduces_the_reported_condition():
    """One source supplies a count for every album, the other for none — the
    asymmetry that turns a conservative fold rule into 21% duplicate rows."""
    rows = _load()
    by_server = collections.defaultdict(lambda: [0, 0])
    for r in rows:
        by_server[r["server"]][0 if r["track_count"] is not None else 1] += 1
    known_all = [s for s, (k, u) in by_server.items() if u == 0 and k > 0]
    unknown_all = [s for s, (k, u) in by_server.items() if k == 0 and u > 0]
    assert known_all and unknown_all, f"expected an asymmetry, got {dict(by_server)}"


# ── grouping behaviour ────────────────────────────────────────────────────────

def test_folding_removes_the_known_vs_unknown_duplication():
    """The headline: with the counts still missing, the browse goes from 12,764
    rows to 10,228 — 2,536 duplicate rows removed, a fifth of the listing."""
    out = _group_albums(_tagged(_load()))
    assert len(out) == FOLDED_ROWS


def test_nothing_is_hidden_by_the_fold():
    """Folding may only ever REDUCE duplicates. Every identity that rendered
    before must still render — the failure mode this rule exists to avoid is
    collapsing two genuine editions into one and losing a release."""
    rows = _load()
    before = {_identity(r["title"], r["artist"]) for r in rows}
    after = {_identity(a.title, a.artist) for a in _group_albums(_tagged(rows))}
    assert before == after


def test_residual_duplicates_are_fully_accounted_for():
    """136 identities still render more than once, and none of them are the bug:
    115 are one source listing a release twice (which the same-source rule keeps
    separate by design) and 21 are genuinely ambiguous multi-edition cases."""
    rows = _load()
    by_id = {r["album_id"]: r for r in rows}
    grouped = collections.defaultdict(list)
    for a in _group_albums(_tagged(rows)):
        grouped[_identity(a.title, a.artist)].append(a)
    dups = {k: v for k, v in grouped.items() if len(v) > 1}
    assert len(dups) == RESIDUAL_DUPLICATE_IDENTITIES

    same_source = ambiguous = other = 0
    for copies in dups.values():
        servers = {by_id[a.id]["server"] for a in copies}
        known = {a.track_count for a in copies if a.track_count is not None}
        if len(servers) == 1:
            same_source += 1
        elif len(known) > 1:
            ambiguous += 1
        else:
            other += 1
    assert (same_source, ambiguous, other) == (
        SAME_SOURCE_REPEATS, LIVE_AMBIGUOUS_SPLITS, 0)


def test_a_folded_row_reports_the_release_length():
    """A row folded out of an unknown-count copy must state the count it folded
    into, not None — downstream release resolution keys on it."""
    rows = _load()
    out = _group_albums(_tagged(rows))
    unknown_before = sum(1 for r in rows if r["track_count"] is None)
    unknown_after = sum(1 for a in out if a.track_count is None)
    assert unknown_before > 0
    assert unknown_after < unknown_before


def test_split_breakdown_is_overwhelmingly_unambiguous():
    """99.2% of the splits offer exactly ONE known count to fold into. That ratio
    is what makes a strict exactly-one rule worth having: it captures nearly all
    the value while never guessing between editions."""
    unambiguous, ambiguous = _split_breakdown(_load())
    assert (unambiguous, ambiguous) == (LIVE_UNAMBIGUOUS_SPLITS, LIVE_AMBIGUOUS_SPLITS)


def test_the_self_titled_run_is_genuinely_ambiguous():
    """Peter Gabriel's four self-titled albums (8, 9, 10 and 11 tracks) face four
    unknown-count copies. No rule can pair those correctly, so they must stay
    separate both before and after any fold — they are the reason the fold
    refuses to act when several known counts compete."""
    rows = [r for r in _load()
            if _identity(r["title"], r["artist"]) == _identity("Peter Gabriel",
                                                               "Peter Gabriel")]
    known = sorted(r["track_count"] for r in rows if r["track_count"] is not None)
    unknown = [r for r in rows if r["track_count"] is None]
    assert len(set(known)) > 1, "expected competing known counts"
    assert unknown, "expected unknown-count copies facing them"

    out = _group_albums(_tagged(rows))
    assert len(out) == len(rows), "an ambiguous identity must not fold"

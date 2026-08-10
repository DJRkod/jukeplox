"""Catalog dedup/merge (2026-06-27 multi-source plan U6). Pure — no I/O, so the
dedup predicate is unit-tested with plain dicts.

Rule (origin R10): **ID-first, else strict-name**, biased conservative — prefer
a visible duplicate over a false merge.

- Two entries that share any external match ID merge (authoritative).
- Two entries that BOTH carry external IDs but share none are authoritatively
  DISTINCT and are never name-merged, even if their names match.
- Otherwise (at least one lacks IDs) entries merge only when they share a strict
  normalized artist+title AND the entity's disambiguators agree: track_count for
  albums; duration within ±2 s plus disc/track number for tracks.

Grouping is union-find over those edges. The name pass clusters within a coarse
bucket (exact artist+title[+disc+track]) and applies the disambiguator predicate
pairwise; tolerance edges chain transitively (a 3-way within-tolerance run merges
even if the extremes are >2 s apart — acceptable given the tight coarse bucket).
"""

from __future__ import annotations

DURATION_TOL_MS = 2000  # ±2 s: same-title/disc/track tracks within this are one recording


# Scheme names that are NOT identities and must never authoritatively merge.
# A MusicBrainz release GROUP spans every release/edition of an album (US/JP,
# single/album, remaster), so two distinct releases share it by design — keying
# dedup on it collapses editions the track_count disambiguation should keep apart
# (ce-debug 2026-06-29: NIN US/JP, Van She single vs album). The release id
# (musicbrainzalbum) is edition-specific and remains the authoritative album key.
_NON_DEDUP_SCHEMES = {"musicbrainzreleasegroup"}


def external_keys(match_ids: dict | None) -> set[str]:
    """Normalized ``type:value`` external-id keys for an entity; empty when none.

    Excludes :data:`_NON_DEDUP_SCHEMES` (e.g. release-group) — ids that span
    multiple distinct entities and so are not identities."""
    out: set[str] = set()
    for k, v in (match_ids or {}).items():
        kl = str(k).strip().lower()
        if kl in _NON_DEDUP_SCHEMES:
            continue
        if v is not None and str(v).strip():
            out.add(f"{kl}:{str(v).strip().lower()}")
    return out


class _UnionFind:
    def __init__(self, has_ext: list[bool], sources: list | None = None) -> None:
        n = len(has_ext)
        self._p = list(range(n))
        self._rank = [0] * n
        self.has_ext = list(has_ext)  # per-root: does the cluster carry any external id?
        # Per-root set of source_ids in the cluster. A cluster holds AT MOST ONE
        # item per source: two entities from the SAME source are distinct things
        # the source itself separates (different rating-keys / file paths), so they
        # must never merge — mirrors native ``_group_albums``, which folds only
        # ACROSS servers and emits same-server siblings as separate rows. Without
        # this, two same-title editions (US/JP, or a single + album) living in ONE
        # local library collapse into one release (ce-debug 2026-06-29; see
        # docs/solutions/.../same-title-distinct-releases-merge-tripled-tracks.md).
        # Items with no source_id (None) contribute an empty set and never collide.
        srcs = sources or [None] * n
        self._sources = [({s} if s else set()) for s in srcs]

    def find(self, x: int) -> int:
        while self._p[x] != x:
            self._p[x] = self._p[self._p[x]]
            x = self._p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._sources[ra] & self._sources[rb]:
            return  # same-source collision → keep these distinct entities separate
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._p[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1
        self.has_ext[ra] = self.has_ext[ra] or self.has_ext[rb]
        self._sources[ra] |= self._sources[rb]


def group(items: list[dict], coarse_key, same) -> list[list[dict]]:
    """Cluster ``items`` ID-first then strict-name; return clusters in first-seen
    order.

    - ``items`` — dicts each carrying ``match_ids`` (dict | None).
    - ``coarse_key(item)`` → hashable | None — a cheap exact bucket for name
      candidates. ``None`` means the item never name-merges (ID edges still apply).
    - ``same(a, b)`` → bool — the disambiguator predicate applied within a coarse
      bucket.
    """
    n = len(items)
    ext = [external_keys(it.get("match_ids")) for it in items]
    uf = _UnionFind([bool(e) for e in ext], [it.get("source_id") for it in items])

    # Pass 1 — shared external id (authoritative merge).
    seen: dict[str, int] = {}
    for i in range(n):
        for k in ext[i]:
            if k in seen:
                uf.union(i, seen[k])
            else:
                seen[k] = i

    # Pass 2 — strict-name within coarse buckets, guarded against fusing two
    # distinct ID-bearing clusters.
    buckets: dict = {}
    for i in range(n):
        ck = coarse_key(items[i])
        if ck is not None:
            buckets.setdefault(ck, []).append(i)
    for members in buckets.values():
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                i, j = members[x], members[y]
                ri, rj = uf.find(i), uf.find(j)
                if ri == rj:
                    continue
                if uf.has_ext[ri] and uf.has_ext[rj]:
                    continue  # both carry IDs but share none → authoritatively distinct
                if same(items[i], items[j]):
                    uf.union(i, j)

    order: list[int] = []
    clusters: dict[int, list[dict]] = {}
    for i in range(n):
        r = uf.find(i)
        if r not in clusters:
            clusters[r] = []
            order.append(r)
        clusters[r].append(items[i])
    return [clusters[r] for r in order]


# ── coarse keys + disambiguator predicates per entity type ───────────────────

def artist_coarse(it: dict):
    return it.get("base_key") or None


def album_coarse(it: dict):
    ab, tb = it.get("artist_base_key"), it.get("title_base")
    return (ab, tb) if (ab is not None and tb is not None) else None


def album_same(a: dict, b: dict) -> bool:
    """Same release: same subtype AND (track counts both-known-and-equal OR
    both-unknown None).

    Mirrors the native browse-index fold exactly (parity plan U1/R5): native
    ``_group_albums`` buckets albums by ``(track_count, subtype)``, with ``None``
    bucketing together — so two copies whose counts are both unknown FOLD, and a
    known count paired with an unknown one STAYS SEPARATE (can't confirm same
    release), as do two known-but-different counts (distinct editions). Loosening
    the prior both-known-only rule stops a connected non-Plex source from
    splitting albums the native pipeline previously folded (R6).

    The subtype gate keys on the same content signal the native fold uses: a
    Single/EP and an Album sharing a title with an UNKNOWN count on DIFFERENT
    sources are distinct releases the same-source guard can't separate — without
    it they fold and the single vanishes (ce-debug 2026-08-10, Van She "Idea of
    Happiness"). ``None`` subtype normalizes to ``'album'`` (matching
    ``guest._group_albums`` and the frontend) so an inconsistent tag on one source
    never splits one genuine release."""
    sa = (a.get("subtype") or "album").strip().lower()
    sb = (b.get("subtype") or "album").strip().lower()
    if sa != sb:
        return False
    ta, tb = a.get("track_count"), b.get("track_count")
    if ta is None and tb is None:
        return True
    return ta is not None and tb is not None and ta == tb


def track_coarse(it: dict):
    ab, tb = it.get("artist_base_key"), it.get("title_base")
    if ab is None or tb is None:
        return None
    return (ab, tb, it.get("disc_number"), it.get("track_number"))


def track_same(a: dict, b: dict) -> bool:
    """Same recording: durations both known and within ±2 s (disc/track already
    matched by the coarse bucket)."""
    da, db = a.get("duration_ms"), b.get("duration_ms")
    return da is not None and db is not None and abs(da - db) <= DURATION_TOL_MS


# ── identity (deterministic; allocate-once robustness layered in U7) ──────────

def lookup_keys(cluster: list[dict]) -> list[str]:
    """Ordered candidate identity keys for a cluster: external ids first (the
    preferred, cross-source-stable identity), then member ``local_key``s. U7's
    ``find_or_create`` reuses an identity if ANY of these is already known, and
    otherwise mints the first (so a no-external-id track's identity is its bare
    source compound id — equal to its existing rating key, making the U7
    migration inert on a Plex-only install)."""
    ext = sorted(k for it in cluster for k in external_keys(it.get("match_ids")))
    local = sorted(str(it.get("local_key")) for it in cluster if it.get("local_key") is not None)
    out: list[str] = []
    seen: set[str] = set()
    for k in (*ext, *local):
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def cluster_identity(cluster: list[dict], entity_type: str) -> str:
    """A stable, collision-free identity for a merged cluster: the smallest
    external id, else the smallest member ``local_key`` (the bare source compound
    id). ``local_key`` is globally unique so distinct clusters never collide on
    the catalog PK, and a no-external-id Plex track's identity equals its existing
    rating key. Equals ``lookup_keys(cluster)[0]``; U7's ``find_or_create`` layers
    allocate-once reuse (late IDs, rescans) on top of this deterministic mint."""
    keys = lookup_keys(cluster)
    return keys[0] if keys else f"empty:{entity_type}"

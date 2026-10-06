"""Unknown album track-count resolution (#61). Pure — no I/O, no framework.

Cross-server album folding buckets copies by ``(track_count, subtype)``, so a
copy whose count is unknown can never share a bucket with one whose count is
known and the two render as separate albums. That was a deliberate conservative
choice, made on the premise — stated in ``_group_albums``'s own docstring — that
an unknown count is "a stale index, or a surface like search that doesn't carry
it", i.e. rare and incidental.

On a real deployment the premise failed completely: one source reported no count
for **any** of its 4,385 albums, because the whole-section crawl that derives
them timed out on every refresh. 2,541 identities split on known-vs-unknown,
duplicating a fifth of the album browse.

This module resolves that BEFORE any grouping happens, rather than teaching each
consumer of the count-equality rule a new comparison. Three consumers share the
rule — the native browse fold, per-release copy selection, and the catalog
floor's pairwise ``album_same`` — and three near-copies of a subtle predicate is
precisely the drift this design exists to prevent. Resolving first means all
three keep their current semantics and only their *input* changes.

It also dissolves a structural problem on the catalog side: ``merge.group`` is a
pairwise union-find and cannot express "exactly one known count in this whole
identity". A pass over the item list can, before ``group()`` is ever called.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence


def normalized_subtype(subtype: Any) -> str:
    """``None`` reads as ``album``.

    Matches ``guest._group_albums``, ``catalog.merge.album_same`` and the
    frontend's ``a.subtype || 'album'``, so an inconsistent tag on one source
    never splits one genuine release.
    """
    return (str(subtype).strip().lower() if subtype else "album")


def resolved_track_counts(
    items: Sequence[Any],
    *,
    identity: Callable[[Any], Any],
    subtype: Callable[[Any], Any],
    count: Callable[[Any], int | None],
) -> list[int | None]:
    """Effective track count per item, positionally aligned with ``items``.

    Within one ``(identity, normalized subtype)`` group holding **exactly one**
    distinct known count, unknown-count members are treated as carrying that
    count. Every other group is left exactly as it was.

    The strictness is the point. Where several known counts compete — four
    self-titled releases of 8, 9, 10 and 11 tracks facing four unknown copies —
    no rule can pair them correctly, and guessing would hide a release the user
    owns. On the live capture that case is 21 identities against 2,520 with a
    single candidate, so refusing to guess costs almost nothing.

    Resolution is asymmetric by design: unknown folds into known, known never
    folds into known. Two *different* known counts remain the strongest evidence
    available that these are two different releases, and keep their authority.

    Accessor-parameterised because the three callers hand it three different
    shapes — dataclass-backed albums from native browse, browse-index dicts from
    release resolution, and catalog item dicts. Returns a new list and never
    touches the inputs: the album objects reaching the native caller come from
    the Plex client cache, and writing an inferred count onto one would make it
    indistinguishable from a reported one on every later read.
    """
    effective: list[int | None] = [count(it) for it in items]
    groups: dict[Any, list[int]] = {}
    for i, it in enumerate(items):
        groups.setdefault((identity(it), normalized_subtype(subtype(it))), []).append(i)

    for members in groups.values():
        known = {effective[i] for i in members if effective[i] is not None}
        if len(known) != 1:
            continue  # nothing to fold into, or several candidates competing
        only = next(iter(known))
        for i in members:
            if effective[i] is None:
                effective[i] = only
    return effective

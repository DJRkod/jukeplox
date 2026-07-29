"""Unified, track-grained catalog (2026-06-27 multi-source plan, Phase B).

Every connected provider scans into one normalized catalog with stable
allocate-once identities, ID-first/strict-name dedup, and a priority-ordered
per-entity holds list. Browse, search, random, and playback read from here so
the app is source-invisible to guests.

- ``store``    — persistence layer: catalog tables + read/write accessors (U5)
- ``scan``     — per-provider crawl → normalized rows → atomic replace (U6)
- ``merge``    — ID-first / strict-name dedup into one entry + holds list (U6)
- ``identity`` — allocate-once / match-forward stable identity + aliases (U7)
- ``migrate``  — offline re-key of ratings/tags/play-counts onto identity (U7)
"""

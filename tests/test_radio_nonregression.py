"""Integration-level non-regression backstop for Radio Mode (radio plan U11, R10).

The per-unit non-regression assertions live in U3 (``tests/test_radio_session.py``:
radio-never-active leaves ``_should_auto_start`` / ``_do_advance`` intact) and U4
(``tests/test_output_radio_eos.py``: each backend's EOS→advance is byte-identical
with radio mode off). THIS file is the integration-level backstop that the radio
*package as a whole* does not disturb two invariants the plan calls out
(System-Wide Impact → "Unchanged invariants"):

1. The track-queue auto-start / advance gate is a pure no-op when radio was never
   used (``state.radio_active()`` is False → the ``and not radio_active()`` clause
   in ``_should_auto_start`` is True, and ``_do_advance`` never early-returns).
2. Radio is NOT a ``MusicSource``: importing / instantiating the radio modules
   does not add a source to the ``SourceRegistry`` and does not flip the
   catalog-routing gate (``catalog_active()``) or the enabled-library filter.
   Per ``docs/solutions/logic-errors/catalog-routing-gate-source-count-vs-type.md``
   the gate keys on source TYPE (``source_type != "plex"``), not source COUNT, so
   these assertions gate on type, never on ``len(sources)``.

DB-free by construction (mirrors ``tests/test_tag_utils.py`` / the DB-free half of
``tests/test_radio_session.py``): every assertion is a pure import + predicate
check or runs against an in-memory stub registry seeded onto ``state._plex_client``
directly — nothing here opens aiosqlite (which would drag the whole file into the
combined-run teardown hang).
"""

from __future__ import annotations

import asyncio

import pytest


# ── stub media source (type-gated, no DB) ────────────────────────────────────


class _StubSource:
    """The minimal shape ``catalog_active()`` / the enabled-library filter read
    off a registry source: a ``source_type`` and a ``source_id``. A real
    ``PlexSource``/``JellyfinSource`` carries far more, but the gate only ever
    reads the type (per the count-vs-type learning doc)."""

    def __init__(self, source_type: str, source_id: str = "srv"):
        self.source_type = source_type
        self.source_id = source_id


class _StubRegistry:
    """Stand-in for ``app.sources.registry.SourceRegistry`` — the object
    ``get_plex_client()`` returns. ``catalog_active()`` only reads ``.sources``,
    so a plain list is enough to exercise the gate without a DB / real client."""

    def __init__(self, sources):
        self.sources = list(sources)


def _run(coro):
    """Drive one coroutine to completion on a throwaway loop (no pytest-asyncio
    needed for the handful of async predicate reads here; keeps the file's async
    surface tiny and DB-free)."""
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ── Non-regression: radio_active() is a no-op when radio was never used (AE7) ──


def test_radio_active_default_false_leaves_auto_start_gate_true():
    """With radio never activated, ``state.radio_active()`` is False, so the
    ``and not radio_active()`` term added to ``_should_auto_start`` evaluates True
    — i.e. it does NOT change the auto-start decision (SG-06 / AE7). We assert the
    predicate and the shape of the gate clause rather than driving a full queue
    excursion (that lives in the U3 session tests)."""
    from app import state

    # The process-level singleton is inactive on a fresh import (no station started).
    assert state.radio_active() is False
    assert state.radio_session.is_active() is False

    # The gate clause is `and not radio_active()`; with radio_active() False the
    # term is True, so it leaves whatever the OTHER conditions decide untouched.
    radio_term = not state.radio_active()
    assert radio_term is True

    # Model the real _should_auto_start conjunction: for any combination of the
    # non-radio conditions, ANDing in a True term never changes the outcome.
    for other_conditions in (True, False):
        assert (other_conditions and radio_term) == other_conditions


def test_radio_active_default_false_does_not_trip_do_advance_early_return():
    """``_do_advance`` early-returns ``if radio_active()``. With radio inactive
    the guard is False, so advance falls through to its normal path exactly as
    before radio existed (the radio clause is inert)."""
    from app import state

    assert state.radio_active() is False
    # The early-return predicate `if radio_active():` is False → no early return.
    should_early_return = state.radio_active()
    assert should_early_return is False


def test_state_exposes_radio_singleton_and_accessor():
    """The concrete SG-01 change: a module-level ``radio_session`` singleton +
    a ``radio_active()`` accessor, mirroring ``queue_engine`` / ``output_router``.
    (A rename/removal of either would silently un-gate the queue.)"""
    from app import state
    from app.radio.session import RadioSession

    assert hasattr(state, "radio_session")
    assert isinstance(state.radio_session, RadioSession)
    assert callable(state.radio_active)


# ── Radio is NOT a MusicSource: the registry is unchanged by radio ────────────


def _seed_plex_only_registry(state):
    """Install a Plex-only stub registry and return the previous value so the
    test can restore it. Keyed straight onto the ``_plex_client`` module global
    so ``get_plex_client()`` returns it WITHOUT a DB read (it short-circuits on a
    non-None cached client)."""
    prev = state._plex_client
    state._plex_client = _StubRegistry([
        _StubSource("plex", "server-a"),
        _StubSource("plex", "server-b"),  # two Plex servers = still native (type gate)
    ])
    return prev


def test_importing_radio_does_not_add_a_source_or_flip_catalog_gate():
    """Importing the radio package must not register a media source nor flip the
    catalog-routing gate. We seed a Plex-only registry, assert the gate is native
    (False) and its source list is Plex-only, import every radio module, then
    re-assert BOTH are byte-identical — radio never entered the registry."""
    from app import state

    prev = _seed_plex_only_registry(state)
    try:
        # Baseline: all-Plex registry → catalog floor OFF (gate keys on TYPE, and
        # two Plex servers are still all-"plex", so it stays native — the
        # count-vs-type invariant from the learning doc).
        before_sources = list(state._plex_client.sources)
        before_gate = _run(state.catalog_active())
        assert before_gate is False
        assert {s.source_type for s in before_sources} == {"plex"}

        # Import (and instantiate) the full radio surface.
        import app.radio.client as rc
        import app.radio.urlcheck  # noqa: F401
        import app.radio.session as rsession
        import app.radio.icy  # noqa: F401
        import app.radio.stream  # noqa: F401
        import app.output.radio_endless  # noqa: F401
        import app.api.radio  # noqa: F401

        rc.get_radio_client()          # build the singleton client
        rsession.RadioSession()        # instantiate a session

        # The registry is UNTOUCHED: same source objects, same count, still all
        # Plex — radio added nothing (it is not a MusicSource).
        assert state._plex_client.sources == before_sources
        assert len(state._plex_client.sources) == 2
        # And the gate is still native — radio did not flip the catalog floor on.
        assert _run(state.catalog_active()) is False
    finally:
        state._plex_client = prev


def test_no_radio_object_carries_source_type():
    """The catalog gate flips only when a registry source has ``source_type !=
    "plex"``. If any radio object exposed a ``source_type`` attribute it could be
    mistaken for a media source — assert none does (the structural reason radio
    can never flip the gate)."""
    from app.radio.client import Station, RadioBrowserClient, get_radio_client
    from app.radio.session import RadioSession

    station = Station(
        stationuuid="u", name="n", url="http://x/", url_resolved="http://x/",
        favicon="", codec="MP3", bitrate=128, tags=[], countrycode="US",
        lastcheckok=True,
    )
    assert not hasattr(station, "source_type")
    assert not hasattr(RadioBrowserClient, "source_type")
    assert not hasattr(get_radio_client(), "source_type")
    assert not hasattr(RadioSession(), "source_type")


def test_radio_client_not_in_source_registry_type_reads():
    """The two sync registry reads that decide catalog routing /
    Plex-playability (``catalog_active`` via ``.sources`` types, and
    ``_plexplayer_source_ids_sync``) must see ONLY the seeded Plex sources — the
    radio client is not among them even after it is built."""
    from app import state
    import app.radio.client as rc

    prev = _seed_plex_only_registry(state)
    try:
        rc.get_radio_client()  # ensure the radio client exists in the process
        # The sync source-id read (the R9 dispatch filter's type read) sees only
        # the two seeded Plex servers — no radio entry sneaks in.
        ids = state._plexplayer_source_ids_sync()
        assert ids == {"server-a", "server-b"}
        # Every registry source is type "plex" — radio contributed none.
        assert all(getattr(s, "source_type", None) == "plex"
                   for s in state._plex_client.sources)
    finally:
        state._plex_client = prev


# ── Enabled-library filter: radio has no library, isn't swept in ──────────────


def test_radio_has_no_enabled_library_surface():
    """The enabled-library filter (``guest.enabled_libraries`` /
    ``_refresh_enabled_libraries``) reads ``client.get_libraries()`` off the
    registry and filters by ``section_key``. Radio exposes no ``get_libraries``
    and no library keys, so it can never be swept into the enabled-library set."""
    from app.radio.client import RadioBrowserClient, get_radio_client
    from app.radio.session import RadioSession

    # No library-listing surface anywhere in the radio package.
    assert not hasattr(get_radio_client(), "get_libraries")
    assert not hasattr(RadioBrowserClient, "get_libraries")
    assert not hasattr(RadioSession(), "get_libraries")
    # And no station/section key attribute the filter could key on.
    from app.radio.client import Station
    station = Station(
        stationuuid="u", name="n", url="http://x/", url_resolved="http://x/",
        favicon="", codec="MP3", bitrate=128, tags=[], countrycode="US",
        lastcheckok=True,
    )
    assert not hasattr(station, "section_key")
    assert not hasattr(station, "key")


# ── Circular-import / co-import smoke ────────────────────────────────────────


def test_all_radio_modules_import_together_and_state_still_imports():
    """Every radio module imports cleanly alongside the others (circular-import
    guard) and ``app.state`` — which imports ``app.radio.session`` at module load
    to build the ``radio_session`` singleton — still imports. A regression here
    (a new import cycle) would break app startup, not just radio."""
    import importlib

    for name in (
        "app.radio.client",
        "app.radio.urlcheck",
        "app.radio.session",
        "app.radio.icy",
        "app.radio.stream",
        "app.output.radio_endless",
        "app.api.radio",
        "app.state",
    ):
        mod = importlib.import_module(name)
        assert mod is not None

    # The load-bearing cross-module wiring: state.radio_session IS a RadioSession
    # from app.radio.session (proves the singleton import resolved, not a stub).
    from app import state
    from app.radio.session import RadioSession
    assert isinstance(state.radio_session, RadioSession)

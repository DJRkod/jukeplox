"""Rig-side half of the soak harness.

A package rather than a bare directory so sibling tools can ``from rig import
features`` instead of path-inserting this directory and importing a generic
top-level name like ``features``, which would win or lose by import order
against anything else on the path. Scripts here are still run directly on the
rig (``python3 rig/state_sampler.py``); the package marker does not change that.
"""
